"""tests/test_pi_chat_bridge.py — 内置 Pi 复用它自己配好的对话模型。

The first-run promise is 「内置 Pi 只要配好模型就能用」. When 模型设置 holds a
chat model on an OpenAI-compatible endpoint (``CONVEYOR_CHAT_BASE_URL`` / 模型
设置 › 自定义) and Pi has no configuration of its own, Pi must run THAT model
as a whole: endpoint, model name, wire api and key from one source. Before this
bridge Pi adopted the chat model NAME while staying on DeepSeek's endpoint, so
the executor check came back ``provider: deepseek`` with a 401.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conveyor.agents import agent_supervisor as ags
from runner.pi_agent import (
    PI_CHAT_BRIDGE_KEY_ENV,
    PI_CHAT_BRIDGE_PROVIDER,
    PiBackend,
    PiProviderConfigError,
    chat_bridge_provider_name,
    describe_pi_provider,
    pi_api_key_env,
    pi_chat_bridge,
    prepare_pi_child_env,
    resolve_pi_provider,
)
from runner.types import Job, JobMode, JobState
from tests.test_pi_builtin import (
    SECRET,
    _FAKE_PI,
    _clean_env,
    _make_settings,
    _save_provider,
)
from tests.test_supervisor_pi_executor import ANALYST, PiExecutor, _clean_env as _sup_env

CHAT_URL = "http://127.0.0.1:18795/v1"
CHAT_MODEL = "fake-model"
CHAT_SECRET = "sk-chat-bridge-secret-987654321"
OTHER_URL = "https://api.other.example/v1"


def _chat_settings(
    tmp: Path,
    *,
    base_url: str | None = CHAT_URL,
    model: str | None = CHAT_MODEL,
    api_key: str | None = CHAT_SECRET,
):
    """模型设置 chat tier: a custom OpenAI-compatible endpoint + model + key."""
    return dataclasses.replace(
        _make_settings(tmp),
        chat_base_url=base_url,
        chat_model=model,
        chat_api_key=api_key,
    )


class ChatBridgeProviderTests(unittest.TestCase):
    def test_custom_chat_endpoint_becomes_one_bridged_provider(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _chat_settings(tmp)
            with patch.dict(os.environ, _clean_env(tmp), clear=True):
                for key in (
                    "CONVEYOR_PI_PROVIDER", "CONVEYOR_PI_MODEL", "CONVEYOR_PI_BASE_URL",
                    "DEEPSEEK_API_KEY", "CONVEYOR_CHAT_API_KEY", "CONVEYOR_CHAT_MODEL",
                    "CONVEYOR_CHAT_BASE_URL", "STEPFUN_API_KEY",
                ):
                    os.environ.pop(key, None)
                selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
                child: dict[str, str] = {"DEEPSEEK_API_KEY": "sk-other-provider"}
                prepare_pi_child_env(child, settings, selection)
                # The chat tier values are read live, so a second resolution
                # with different chat values must not repeat the first one.
                object.__setattr__(settings, "chat_model", "second-model")
                again = resolve_pi_provider(settings, model_fallback="", chat_model=False)
                # Read the generated file while the temp dir still exists.
                models_path = Path(child["PI_CODING_AGENT_DIR"]) / "models.json"
                models_text = models_path.read_text(encoding="utf-8")
                models_mode = stat.S_IMODE(models_path.stat().st_mode)
        self.assertFalse(selection.config_error, selection.config_error)
        self.assertTrue(selection.chat_bridge)
        self.assertTrue(selection.bridge_required)
        self.assertEqual(selection.provider, PI_CHAT_BRIDGE_PROVIDER)
        self.assertEqual(selection.model, CHAT_MODEL)
        self.assertEqual(selection.api_key_env, PI_CHAT_BRIDGE_KEY_ENV)
        self.assertEqual(selection.api_key, CHAT_SECRET)
        self.assertEqual(again.model, "second-model")
        self.assertEqual(again.provider, PI_CHAT_BRIDGE_PROVIDER)

        self.assertEqual(child.get(PI_CHAT_BRIDGE_KEY_ENV), CHAT_SECRET)
        self.assertNotIn("DEEPSEEK_API_KEY", child)
        self.assertNotIn(SECRET, json.dumps(child))
        self.assertEqual(models_mode, 0o600)
        self.assertNotIn(CHAT_SECRET, models_text)
        self.assertNotIn(CHAT_SECRET, child["PI_CODING_AGENT_DIR"])
        provider = json.loads(models_text)["providers"][PI_CHAT_BRIDGE_PROVIDER]
        self.assertEqual(provider["baseUrl"], CHAT_URL)
        self.assertEqual(provider["api"], "openai-completions")
        self.assertEqual(provider["apiKey"], "$" + PI_CHAT_BRIDGE_KEY_ENV)
        self.assertEqual(provider["models"], [{"id": CHAT_MODEL, "name": CHAT_MODEL}])

    def test_bridge_key_env_is_the_chat_key_variable(self) -> None:
        # The name Pi resolves for the bridged provider is the very variable the
        # chat tier already reads, so no new credential name is introduced.
        with tempfile.TemporaryDirectory() as raw:
            with patch.dict(os.environ, _clean_env(Path(raw)), clear=True):
                os.environ.pop("CONVEYOR_PI_API_KEY_ENV", None)
                self.assertEqual(pi_api_key_env(PI_CHAT_BRIDGE_PROVIDER), PI_CHAT_BRIDGE_KEY_ENV)
        self.assertEqual(PI_CHAT_BRIDGE_KEY_ENV, "CONVEYOR_CHAT_API_KEY")

    def test_chat_values_from_the_environment_alone_are_enough(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _chat_settings(tmp, base_url=None, model=None, api_key=None)
            with patch.dict(
                os.environ,
                _clean_env(
                    tmp,
                    CONVEYOR_CHAT_BASE_URL=CHAT_URL,
                    CONVEYOR_CHAT_MODEL=CHAT_MODEL,
                    CONVEYOR_CHAT_API_KEY=CHAT_SECRET,
                ),
                clear=True,
            ):
                selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
                child: dict[str, str] = {}
                prepare_pi_child_env(child, settings, selection)
                # The generated models.json string carries the same provider.
                models = json.loads(selection.models_json or "")
        self.assertFalse(selection.config_error, selection.config_error)
        self.assertEqual(selection.provider, PI_CHAT_BRIDGE_PROVIDER)
        self.assertEqual(selection.model, CHAT_MODEL)
        self.assertEqual(child.get(PI_CHAT_BRIDGE_KEY_ENV), CHAT_SECRET)
        self.assertEqual(models["providers"][PI_CHAT_BRIDGE_PROVIDER]["baseUrl"], CHAT_URL)

    def test_missing_chat_key_is_a_local_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _chat_settings(tmp, api_key=None)
            with patch.dict(os.environ, _clean_env(tmp), clear=True):
                selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
                status = describe_pi_provider(settings, model_fallback="", chat_model=False)
                with self.assertRaises(PiProviderConfigError):
                    prepare_pi_child_env({}, settings, selection)
        self.assertTrue(selection.config_error)
        self.assertIn("模型设置", selection.config_error)
        self.assertIn("API Key", selection.config_error)
        self.assertNotIn(CHAT_SECRET, selection.config_error)
        self.assertFalse(status["configured"])
        self.assertIn("模型设置", status["hint"])

    def test_no_chat_endpoint_keeps_pi_own_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _chat_settings(tmp, base_url=None)
            with patch.dict(os.environ, _clean_env(tmp, DEEPSEEK_API_KEY="sk-ds"), clear=True):
                self.assertIsNone(pi_chat_bridge(settings))
                worker = resolve_pi_provider(settings)
                supervisor = resolve_pi_provider(settings, model_fallback="", chat_model=False)
                child: dict[str, str] = {}
                prepare_pi_child_env(child, settings, supervisor)
                isolated_text = (
                    Path(child["PI_CODING_AGENT_DIR"]) / "models.json"
                ).read_text(encoding="utf-8")
        for selection in (worker, supervisor):
            self.assertFalse(selection.chat_bridge)
            self.assertEqual(selection.provider, "deepseek")
            self.assertIsNone(selection.models_json)
            self.assertFalse(selection.bridge_required)
        # chat_model=True still falls back to the chat model NAME on DeepSeek,
        # which is the legacy behaviour when no endpoint is bridged.
        self.assertEqual(worker.model, CHAT_MODEL)
        self.assertEqual(supervisor.model, "")
        self.assertEqual(json.loads(isolated_text), {"providers": {}})

    def test_unusable_chat_endpoint_is_not_bridged(self) -> None:
        for url in ("", "not-a-url", "ftp://example.com/v1", "https://user:pw@example.com/v1"):
            with self.subTest(url=url):
                with tempfile.TemporaryDirectory() as raw:
                    tmp = Path(raw)
                    settings = _chat_settings(tmp, base_url=url or None)
                    with patch.dict(os.environ, _clean_env(tmp), clear=True):
                        self.assertIsNone(pi_chat_bridge(settings))
                        selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
                self.assertFalse(selection.chat_bridge)
                self.assertEqual(selection.provider, "deepseek")

    def test_no_chat_model_name_is_not_bridged(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _chat_settings(tmp, model=None)
            with patch.dict(os.environ, _clean_env(tmp), clear=True):
                self.assertIsNone(pi_chat_bridge(settings))
                selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
            self.assertFalse(selection.chat_bridge)
            self.assertEqual(selection.provider, "deepseek")

    def test_explicit_pi_provider_beats_the_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _chat_settings(tmp)
            with patch.dict(
                os.environ,
                _clean_env(
                    tmp,
                    CONVEYOR_PI_PROVIDER="stepfun",
                    CONVEYOR_PI_MODEL="step-5-preview",
                    CONVEYOR_PI_BASE_URL="https://api.stepfun.example/v1",
                    STEPFUN_API_KEY=SECRET,
                ),
                clear=True,
            ):
                selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
        self.assertFalse(selection.chat_bridge)
        self.assertEqual(selection.provider, "stepfun")
        self.assertEqual(selection.model, "step-5-preview")
        data = json.loads(selection.models_json or "")
        self.assertEqual(data["providers"]["stepfun"]["baseUrl"], "https://api.stepfun.example/v1")
        self.assertNotIn(CHAT_URL, selection.models_json or "")
        self.assertNotIn(CHAT_SECRET, selection.models_json or "")

    def test_explicit_pi_base_url_beats_the_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _chat_settings(tmp)
            with patch.dict(
                os.environ,
                _clean_env(
                    tmp,
                    CONVEYOR_PI_PROVIDER="my-llm",
                    CONVEYOR_PI_MODEL="my-model",
                    CONVEYOR_PI_BASE_URL=OTHER_URL,
                    MY_LLM_API_KEY=SECRET,
                ),
                clear=True,
            ):
                selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
        self.assertFalse(selection.chat_bridge)
        self.assertEqual(selection.provider, "my-llm")
        self.assertEqual(selection.model, "my-model")
        data = json.loads(selection.models_json or "")
        self.assertEqual(data["providers"]["my-llm"]["baseUrl"], OTHER_URL)
        self.assertNotIn(CHAT_URL, selection.models_json or "")

    def test_saved_provider_beats_the_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _chat_settings(tmp)
            with patch.dict(os.environ, _clean_env(tmp), clear=True):
                _save_provider(settings)
                selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
        self.assertFalse(selection.chat_bridge)
        self.assertEqual(selection.provider, "step-fun")
        self.assertEqual(selection.model, "step-3")
        data = json.loads(selection.models_json or "")
        self.assertEqual(data["providers"]["step-fun"]["baseUrl"], "https://api.stepfun.example/v1")
        self.assertNotIn(CHAT_URL, selection.models_json or "")
        self.assertNotIn(CHAT_MODEL, selection.models_json or "")

    def test_explicit_pi_model_travels_with_the_chat_endpoint(self) -> None:
        # Documented decision: CONVEYOR_PI_MODEL only overrides the model NAME.
        # With no CONVEYOR_PI_PROVIDER and no CONVEYOR_PI_BASE_URL the endpoint
        # comes from the chat bridge, so the custom name is never sent to
        # DeepSeek (the deepseek + custom-name mix that returned 401).
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _chat_settings(tmp)
            with patch.dict(
                os.environ,
                _clean_env(tmp, CONVEYOR_PI_MODEL="pi-only-model", DEEPSEEK_API_KEY="sk-ds"),
                clear=True,
            ):
                selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
                child: dict[str, str] = {}
                prepare_pi_child_env(child, settings, selection)
        self.assertFalse(selection.config_error, selection.config_error)
        self.assertTrue(selection.chat_bridge)
        self.assertEqual(selection.provider, PI_CHAT_BRIDGE_PROVIDER)
        self.assertEqual(selection.model, "pi-only-model")
        data = json.loads(selection.models_json or "")
        self.assertEqual(data["providers"][PI_CHAT_BRIDGE_PROVIDER]["baseUrl"], CHAT_URL)
        self.assertEqual(
            data["providers"][PI_CHAT_BRIDGE_PROVIDER]["models"], [{"id": "pi-only-model", "name": "pi-only-model"}]
        )
        self.assertNotIn("deepseek", json.dumps(data))
        self.assertNotIn("DEEPSEEK_API_KEY", child)
        self.assertNotIn(CHAT_SECRET, json.dumps(data))

    def test_explicit_model_without_chat_endpoint_keeps_deepseek(self) -> None:
        # No chat endpoint to bridge: the explicit model stays on Pi's own
        # DeepSeek default, exactly as before this bridge existed.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _chat_settings(tmp, base_url=None)
            with patch.dict(
                os.environ,
                _clean_env(tmp, CONVEYOR_PI_MODEL="pi-only-model", DEEPSEEK_API_KEY="sk-ds"),
                clear=True,
            ):
                selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
        self.assertFalse(selection.chat_bridge)
        self.assertEqual(selection.provider, "deepseek")
        self.assertEqual(selection.model, "pi-only-model")
        self.assertIsNone(selection.models_json)
        self.assertFalse(selection.config_error)

    def test_wire_override_applies_to_the_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _chat_settings(tmp)
            with patch.dict(
                os.environ, _clean_env(tmp, CONVEYOR_PI_WIRE_API="responses"), clear=True
            ):
                ok = resolve_pi_provider(settings, model_fallback="", chat_model=False)
            with patch.dict(
                os.environ, _clean_env(tmp, CONVEYOR_PI_WIRE_API="openai-bogus"), clear=True
            ):
                bad = resolve_pi_provider(settings, model_fallback="", chat_model=False)
        self.assertFalse(ok.config_error, ok.config_error)
        self.assertEqual(
            json.loads(ok.models_json or "")["providers"][PI_CHAT_BRIDGE_PROVIDER]["api"],
            "openai-responses",
        )
        self.assertTrue(bad.config_error)
        self.assertIn("无法识别", bad.config_error)

    def test_bridge_values_are_read_fresh_every_time(self) -> None:
        # 模型设置 saves clear Pi's last check; the bridge itself keeps no
        # cache, so the next resolution already sees the new values.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _chat_settings(tmp)
            seen = []
            for url, model in ((CHAT_URL, CHAT_MODEL), (OTHER_URL, "renamed-model")):
                object.__setattr__(settings, "chat_base_url", url)
                object.__setattr__(settings, "chat_model", model)
                with patch.dict(os.environ, _clean_env(tmp), clear=True):
                    selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
                    status = describe_pi_provider(settings, model_fallback="", chat_model=False)
                    base_url = json.loads(selection.models_json or "")["providers"][PI_CHAT_BRIDGE_PROVIDER]["baseUrl"]
                seen.append((selection.model, base_url, status["provider_name"]))
            self.assertEqual(
                seen[0],
                (CHAT_MODEL, CHAT_URL, chat_bridge_provider_name(CHAT_MODEL)),
            )
            self.assertEqual(
                seen[1],
                ("renamed-model", OTHER_URL, chat_bridge_provider_name("renamed-model")),
            )

    def test_describe_pi_provider_names_the_bridge_in_chinese(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _chat_settings(tmp)
            with patch.dict(os.environ, _clean_env(tmp), clear=True):
                status = describe_pi_provider(settings)
        self.assertTrue(status["configured"], status)
        self.assertFalse(status["explicit"])
        self.assertEqual(status["provider"], PI_CHAT_BRIDGE_PROVIDER)
        self.assertEqual(status["provider_name"], f"和对话用同一个模型（{CHAT_MODEL}）")
        self.assertEqual(status["model"], CHAT_MODEL)
        self.assertNotIn(CHAT_SECRET, json.dumps(status))


class ChatBridgeBackendRunTests(unittest.TestCase):
    """The worker path: one real PiBackend attempt against a fake pi binary."""

    def test_run_codex_attempt_uses_the_bridged_provider(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _chat_settings(tmp)
            dump = tmp / "dump.json"
            fake = tmp / "fake-pi"
            fake.write_text(_FAKE_PI % str(dump), encoding="utf-8")
            fake.chmod(0o755)
            with patch.dict(
                os.environ,
                _clean_env(tmp, CONVEYOR_PI_BIN=str(fake), DEEPSEEK_API_KEY="sk-deepseek-other"),
                clear=True,
            ):
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
                # Read the fake pi's dump and the generated models.json while
                # the temp dir still exists.
                seen = json.loads(dump.read_text(encoding="utf-8"))
                models_text = (
                    Path(seen["env"]["PI_CODING_AGENT_DIR"]) / "models.json"
                ).read_text(encoding="utf-8")
            self.assertEqual(job.return_code, 0, job.error)
            argv = seen["argv"]
            self.assertEqual(argv[argv.index("--provider") + 1], PI_CHAT_BRIDGE_PROVIDER)
            self.assertEqual(argv[argv.index("--model") + 1], CHAT_MODEL)
            self.assertNotIn(CHAT_SECRET, json.dumps(argv))
            child_env = seen["env"]
            self.assertEqual(child_env.get(PI_CHAT_BRIDGE_KEY_ENV), CHAT_SECRET)
            self.assertNotIn("DEEPSEEK_API_KEY", child_env)
            models = json.loads(models_text)
            provider = models["providers"][PI_CHAT_BRIDGE_PROVIDER]
            self.assertEqual(provider["baseUrl"], CHAT_URL)
            self.assertEqual(provider["apiKey"], "$" + PI_CHAT_BRIDGE_KEY_ENV)
            self.assertNotIn(CHAT_SECRET, json.dumps(models))


class ChatBridgeSupervisorTests(unittest.TestCase):
    """The 项目 roles default to Pi, so 「配好模型就能用」 must hold there too."""

    def test_supervisor_argv_and_child_env_use_the_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _chat_settings(tmp)
            with patch.dict(
                os.environ,
                _sup_env(
                    CONVEYOR_PI_BIN="/bin/echo",
                    DEEPSEEK_API_KEY="sk-deepseek-other",
                    OPENAI_API_KEY=SECRET,
                ),
                clear=True,
            ):
                argv = PiExecutor(settings)._argv(ANALYST, "/tmp")
                child = ags._child_env(settings, executors="pi")
                ok, reason = PiExecutor(settings).available()
                # Read the bridged models.json while the temp dir still exists.
                models_text = (
                    Path(child["PI_CODING_AGENT_DIR"]) / "models.json"
                ).read_text(encoding="utf-8")
        self.assertTrue(ok, reason)
        self.assertEqual(argv[argv.index("--provider") + 1], PI_CHAT_BRIDGE_PROVIDER)
        self.assertEqual(argv[argv.index("--model") + 1], CHAT_MODEL)
        self.assertNotIn(CHAT_SECRET, json.dumps(argv))
        data = json.loads(models_text)
        provider = data["providers"][PI_CHAT_BRIDGE_PROVIDER]
        self.assertEqual(provider["baseUrl"], CHAT_URL)
        self.assertEqual(provider["api"], "openai-completions")
        self.assertEqual(provider["apiKey"], "$" + PI_CHAT_BRIDGE_KEY_ENV)
        self.assertEqual(provider["models"], [{"id": CHAT_MODEL, "name": CHAT_MODEL}])
        self.assertEqual(child.get(PI_CHAT_BRIDGE_KEY_ENV), CHAT_SECRET)
        self.assertNotIn("DEEPSEEK_API_KEY", child)
        self.assertNotIn("OPENAI_API_KEY", child)
        self.assertNotIn(CHAT_SECRET, json.dumps(data))

    def test_supervisor_not_ready_without_the_chat_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _chat_settings(tmp, api_key=None)
            with patch.dict(os.environ, _sup_env(CONVEYOR_PI_BIN="/bin/echo"), clear=True):
                ok, reason = PiExecutor(settings).available()
        self.assertFalse(ok)
        self.assertIn("模型设置", reason)
        self.assertNotIn(CHAT_SECRET, reason)


if __name__ == "__main__":
    unittest.main()
