from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conveyor.core import provider_config
from conveyor.agents import workers
from config import Settings
from runner import pi_agent
from runner.pi_agent import PiBackend, build_pi_models_json, resolve_pi_bin, sanitize_pi_provider_id
from runner.types import Job, JobMode, JobState

SECRET = "sk-test-secret-1234567890"


def _make_settings(tmp: Path) -> Settings:
    root = (tmp / "workspace").resolve()
    root.mkdir(parents=True, exist_ok=True)
    state_root = tmp / "state"
    (state_root / "tasks").mkdir(parents=True, exist_ok=True)
    (state_root / "memory").mkdir(parents=True, exist_ok=True)
    return Settings(
        telegram_bot_token="unused",
        telegram_allowed_user_id=1,
        codex_workspace_root=root,
        codex_bin="codex",
        codex_task_root=state_root / "tasks",
        codex_model=None,
        codex_timeout_seconds=10,
        telegram_progress_seconds=1,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=state_root / "memory",
        user_timezone="UTC",
        chat_mode="auto",
        chat_api_key="sk-test-deepseek",
        chat_model="deepseek-flash",
        chat_tools_enabled=True,
    )


def _clean_env(tmp: Path, **extra: str) -> dict[str, str]:
    env = {"CONVEYOR_ENV_FILE": str(tmp / ".env")}
    env.update(extra)
    return env


def _save_provider(settings: Settings, **overrides: str) -> None:
    payload = {
        "provider_id": "step-fun",
        "provider_name": "StepFun",
        "model": "step-3",
        "base_url": "https://api.stepfun.example/v1",
        "wire_api": "chat",
        "env_key": "STEPFUN_API_KEY",
        "api_key": SECRET,
    }
    payload.update(overrides)
    provider_config.save_provider_config(settings, payload)


_FAKE_PI = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "sys.stdin.read()\n"
    "dump = {'argv': sys.argv[1:], 'env': {k: v for k, v in os.environ.items()}}\n"
    "open(%r, 'w').write(json.dumps(dump))\n"
    "print(json.dumps({'type': 'agent_end', 'messages': [{'role': 'assistant', "
    "'content': [{'type': 'text', 'text': 'done'}]}]}), flush=True)\n"
)


class PiBinaryResolutionTests(unittest.TestCase):
    def test_resolution_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            vendored = tmp / "vendored-pi"
            vendored.write_text("#!/bin/sh\n")
            settings = type("S", (), {"pi_bin": "/from/settings/pi"})()
            blank = type("S", (), {})()
            with patch.object(pi_agent, "vendored_pi_bin", return_value=vendored):
                with patch.dict(os.environ, {"CONVEYOR_PI_BIN": "/from/env/pi"}):
                    self.assertEqual(resolve_pi_bin(settings), "/from/env/pi")
                env = {k: v for k, v in os.environ.items() if k != "CONVEYOR_PI_BIN"}
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(resolve_pi_bin(settings), "/from/settings/pi")
                    self.assertEqual(resolve_pi_bin(blank), str(vendored))
                    self.assertEqual(resolve_pi_bin(None), str(vendored))
            with patch.object(pi_agent, "vendored_pi_bin", return_value=tmp / "missing"):
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(resolve_pi_bin(blank), "pi")

    def test_validate_message_mentions_executor_page(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            settings = _make_settings(Path(raw))
            backend = PiBackend(settings)
            backend.pi_bin = "definitely-not-a-real-pi-binary"
            with patch("runner.core.CodexRunner.validate", return_value=None):
                with self.assertRaises(RuntimeError) as ctx:
                    asyncio.run(backend.validate())
            self.assertIn("执行者", str(ctx.exception))

    def test_sanitize_provider_id(self) -> None:
        self.assertEqual(sanitize_pi_provider_id("Step_Fun.AI"), "step-fun-ai")
        self.assertEqual(sanitize_pi_provider_id("***"), "conveyor")


class PiProviderBridgeTests(unittest.TestCase):
    def _run(self, tmp: Path, settings: Settings, extra_env: dict[str, str] | None = None):
        fake = tmp / "fake-pi"
        dump = tmp / "dump.json"
        fake.write_text(_FAKE_PI % str(dump), encoding="utf-8")
        fake.chmod(0o755)
        env = _clean_env(tmp)
        env.update(extra_env or {})
        with patch.dict(os.environ, env):
            for key in ("CONVEYOR_PI_PROVIDER", "CONVEYOR_PI_MODEL"):
                if key not in env:
                    os.environ.pop(key, None)
            backend = PiBackend(settings)
            backend.pi_bin = str(fake)
            job = Job(
                "job-bridge", JobMode.RUN, "hello", "unused", state=JobState.RUNNING,
                worktree_path=settings.codex_workspace_root,
                log_path=settings.codex_task_root / "job.log",
                final_message_path=settings.codex_task_root / "final.txt",
            )

            async def on_progress(_text: str) -> None:
                return None

            asyncio.run(backend._run_codex_attempt(job, on_progress))
        self.assertEqual(job.return_code, 0, job.error)
        return json.loads(dump.read_text(encoding="utf-8"))

    def _models_json(self, settings: Settings) -> tuple[Path, dict]:
        path = Path(settings.codex_task_root) / "pi-agent" / "models.json"
        return path, json.loads(path.read_text(encoding="utf-8"))

    def _bridge_case(self, wire_api: str, expected_api: str) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            with patch.dict(os.environ, _clean_env(tmp)):
                _save_provider(settings, wire_api=wire_api)
            seen = self._run(tmp, settings)
            path, data = self._models_json(settings)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn(SECRET, path.read_text(encoding="utf-8"))
            provider = data["providers"]["step-fun"]
            self.assertEqual(provider["api"], expected_api)
            self.assertEqual(provider["apiKey"], "$STEPFUN_API_KEY")
            self.assertEqual(provider["baseUrl"], "https://api.stepfun.example/v1")
            self.assertEqual(provider["models"], [{"id": "step-3", "name": "step-3"}])
            argv = seen["argv"]
            self.assertEqual(argv[argv.index("--provider") + 1], "step-fun")
            self.assertEqual(argv[argv.index("--model") + 1], "step-3")
            self.assertEqual(seen["env"]["STEPFUN_API_KEY"], SECRET)
            self.assertEqual(seen["env"]["PI_CODING_AGENT_DIR"], str(path.parent))
            self.assertEqual(seen["env"]["PI_OFFLINE"], "1")
            self.assertNotIn(SECRET, " ".join(argv))

    def test_bridge_chat_wire_api(self) -> None:
        self._bridge_case("chat", "openai-completions")

    def test_bridge_responses_wire_api(self) -> None:
        self._bridge_case("responses", "openai-responses")

    def test_explicit_env_keeps_deepseek_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            with patch.dict(os.environ, _clean_env(tmp)):
                _save_provider(settings)
            seen = self._run(
                tmp, settings,
                {"CONVEYOR_PI_PROVIDER": "deepseek", "CONVEYOR_PI_MODEL": "deepseek-chat",
                 "DEEPSEEK_API_KEY": "sk-test-deepseek"},
            )
            argv = seen["argv"]
            self.assertEqual(argv[argv.index("--provider") + 1], "deepseek")
            self.assertEqual(argv[argv.index("--model") + 1], "deepseek-v4-flash")
            self.assertEqual(seen["env"]["DEEPSEEK_API_KEY"], "sk-test-deepseek")
            self.assertFalse((Path(settings.codex_task_root) / "pi-agent" / "models.json").exists())

    def test_no_saved_provider_keeps_legacy_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            seen = self._run(tmp, settings, {"DEEPSEEK_API_KEY": "sk-test-deepseek"})
            argv = seen["argv"]
            self.assertEqual(argv[argv.index("--provider") + 1], "deepseek")
            self.assertFalse((Path(settings.codex_task_root) / "pi-agent" / "models.json").exists())

    def test_models_json_builder_never_contains_key(self) -> None:
        data = build_pi_models_json({
            "provider_id": "OpenAI", "base_url": "https://x/v1", "model": "m",
            "wire_api": "responses", "env_key": "OPENAI_API_KEY", "api_key": SECRET,
        })
        self.assertNotIn(SECRET, json.dumps(data))
        self.assertEqual(data["providers"]["openai"]["apiKey"], "$OPENAI_API_KEY")


class PiCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch("conveyor.agents.worker_usage.fetch_usage", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pi(self, settings: Settings) -> dict:
        detection = {"pi": {"installed": True, "version": "1.5.0", "path": "/x/pi", "auth": "unknown"}}
        payload = workers._assemble_workers_payload(settings, detection)
        return next(w for w in payload["workers"] if w["id"] == "pi")

    def test_install_command_is_fixed(self) -> None:
        pi = workers.get_catalog_worker("pi")
        self.assertEqual(pi["install_argv"], ["bash", "scripts/install_pi.sh"])
        with tempfile.TemporaryDirectory() as raw:
            settings = _make_settings(Path(raw))
            with patch("subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(pi["install_argv"], 0, stdout="ok", stderr="")
                result = workers.install_worker(settings, "pi")
            self.assertTrue(result["ok"])
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["bash", "scripts/install_pi.sh"])
            self.assertEqual(kwargs["cwd"], str(Path(workers.__file__).resolve().parent))
            self.assertFalse(kwargs.get("shell", False))
        self.assertTrue((Path(workers.__file__).resolve().parent / "scripts" / "install_pi.sh").is_file())

    def test_auth_needs_login_without_provider(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            with patch.dict(os.environ, _clean_env(tmp)):
                for key in ("CONVEYOR_PI_PROVIDER", "CONVEYOR_PI_MODEL", "DEEPSEEK_API_KEY",
                            "CONVEYOR_CHAT_API_KEY"):
                    os.environ.pop(key, None)
                object.__setattr__(settings, "chat_api_key", None)
                pi = self._pi(settings)
            self.assertEqual(pi["auth"], "needs_login")
            self.assertIsNone(pi["login_command"])
            self.assertEqual(pi["auth_hint"], "先在「设置 → 模型设置」里填服务商")
            self.assertTrue(pi["install_supported"])
            self.assertTrue(pi["models_supported"])
            self.assertTrue(pi["runnable"])
            self.assertEqual(pi["label"], "Pi（内置）")

    def test_auth_ok_with_saved_provider(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            with patch.dict(os.environ, _clean_env(tmp)):
                for key in ("CONVEYOR_PI_PROVIDER", "CONVEYOR_PI_MODEL"):
                    os.environ.pop(key, None)
                _save_provider(settings)
                pi = self._pi(settings)
            self.assertEqual(pi["auth"], "ok")
            self.assertIsNone(pi["auth_hint"])
            self.assertEqual(pi["provider"]["provider_name"], "StepFun")
            self.assertEqual(pi["provider"]["model"], "step-3")
            self.assertNotIn(SECRET, json.dumps(pi))

    def _auth_with(self, env: dict[str, str], chat_key: str | None = None) -> str:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            object.__setattr__(settings, "chat_api_key", chat_key)  # frozen dataclass
            with patch.dict(os.environ, _clean_env(tmp, **env)):
                for key in ("CONVEYOR_PI_PROVIDER", "CONVEYOR_PI_MODEL", "DEEPSEEK_API_KEY",
                            "CONVEYOR_CHAT_API_KEY"):
                    if key not in env:
                        os.environ.pop(key, None)
                return self._pi(settings)["auth"]

    def test_auth_ok_with_deepseek_env_key(self) -> None:
        self.assertEqual(self._auth_with({"DEEPSEEK_API_KEY": "sk-test-deepseek"}), "ok")

    def test_auth_ok_with_chat_env_key(self) -> None:
        self.assertEqual(self._auth_with({"CONVEYOR_CHAT_API_KEY": "sk-test-chat"}), "ok")

    def test_auth_ok_with_settings_chat_key(self) -> None:
        self.assertEqual(self._auth_with({}, chat_key="sk-test-chat"), "ok")

    def test_auth_needs_login_when_no_source(self) -> None:
        self.assertEqual(self._auth_with({}, chat_key=""), "needs_login")

    def test_auth_ok_with_explicit_env(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            with patch.dict(os.environ, _clean_env(tmp, CONVEYOR_PI_PROVIDER="deepseek")):
                pi = self._pi(settings)
            self.assertEqual(pi["auth"], "ok")

    def test_models_use_saved_provider_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            with patch.dict(os.environ, _clean_env(tmp)):
                _save_provider(settings)
                with patch.object(
                    provider_config, "fetch_provider_models",
                    return_value=[{"id": "step-3"}, {"id": "step-2"}],
                ) as fetch:
                    models, _ = workers.get_worker_models("pi", settings=settings)
                fetch.assert_called_once_with(settings)  # saved provider: no draft
            self.assertEqual([m["id"] for m in models], ["step-3", "step-2"])
            with self.assertRaises(workers.ModelsFetchError):
                workers.get_worker_models("pi")

    def test_models_fetch_error_is_mapped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            settings = _make_settings(Path(raw))
            with patch.object(
                provider_config, "fetch_provider_models",
                side_effect=provider_config.ProviderModelsError("未配置模型供应商"),
            ):
                with self.assertRaises(workers.ModelsFetchError):
                    workers.get_worker_models("pi", settings=settings)

    def test_detection_prefers_vendored_binary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            vendored = Path(raw) / "pi"
            vendored.write_text("#!/bin/sh\necho 1.5.0\n")
            vendored.chmod(0o755)
            with patch.object(pi_agent, "vendored_pi_bin", return_value=vendored), \
                 patch.dict(os.environ, {"CONVEYOR_PI_BIN": ""}):
                result = workers.detect_worker(workers.get_catalog_worker("pi"))
            self.assertTrue(result["installed"])
            self.assertEqual(result["path"], str(vendored))
            self.assertEqual(result["version"], "1.5.0")


if __name__ == "__main__":
    unittest.main()
