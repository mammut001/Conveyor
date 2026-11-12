"""Explicit custom Pi providers get one fresh $ENV models.json.

Covers the live failure: CONVEYOR_PI_PROVIDER=stepfun skipped the bridge, so
Pi 1.5.0 kept a stale models.json whose apiKey was the plain env name, and the
Supervisor never pointed at a generated config (Unknown provider).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conveyor.agents import agent_supervisor as ags
from conveyor.agents import workers
from runner.pi_agent import (
    PiProviderConfigError,
    describe_pi_provider,
    pi_coding_agent_dir,
    prepare_pi_child_env,
    resolve_pi_provider,
)
from tests.test_pi_builtin import SECRET, _clean_env, _make_settings, _save_provider
from tests.test_supervisor_pi_executor import ANALYST, PiExecutor, _clean_env as _sup_env

STEP_URL = "https://api.stepfun.com/step_plan/v1"
OTHER_URL = "https://api.other.example/v1"
OTHER_SECRET = "sk-other-provider-secret"


class ExplicitStepfunBridgeTests(unittest.TestCase):
    def test_explicit_stepfun_uses_matching_saved_endpoint_and_dollar_env(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            with patch.dict(os.environ, _clean_env(tmp), clear=True):
                _save_provider(
                    settings,
                    provider_id="stepfun",
                    provider_name="StepFun",
                    model="step-3.7-flash",
                    base_url=STEP_URL,
                    wire_api="responses",
                    env_key="STEPFUN_API_KEY",
                    api_key=SECRET,
                )
                os.environ["CONVEYOR_PI_PROVIDER"] = "stepfun"
                os.environ["CONVEYOR_PI_MODEL"] = "step-5-preview"
                selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
                child: dict[str, str] = {}
                prepare_pi_child_env(child, settings, selection)
            self.assertEqual(selection.provider, "stepfun")
            self.assertEqual(selection.model, "step-5-preview")
            self.assertEqual(selection.api_key_env, "STEPFUN_API_KEY")
            self.assertEqual(child.get("STEPFUN_API_KEY"), SECRET)
            self.assertNotIn("DEEPSEEK_API_KEY", child)
            data = json.loads((Path(child["PI_CODING_AGENT_DIR"]) / "models.json").read_text(encoding="utf-8"))
            provider = data["providers"]["stepfun"]
            self.assertEqual(provider["baseUrl"], STEP_URL)
            self.assertEqual(provider["api"], "openai-responses")
            self.assertEqual(provider["apiKey"], "$STEPFUN_API_KEY")
            self.assertEqual(provider["models"], [{"id": "step-5-preview", "name": "step-5-preview"}])
            text = json.dumps(data)
            self.assertNotIn(SECRET, text)
            self.assertNotIn(SECRET, child["PI_CODING_AGENT_DIR"])

    def test_wire_override_and_explicit_base_url_beat_saved_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            override_url = "https://api.stepfun.example/v1"
            with patch.dict(os.environ, _clean_env(tmp), clear=True):
                _save_provider(
                    settings,
                    provider_id="stepfun",
                    base_url=STEP_URL,
                    wire_api="responses",
                    model="saved-model",
                    env_key="STEPFUN_API_KEY",
                    api_key=SECRET,
                )
                os.environ["CONVEYOR_PI_PROVIDER"] = "stepfun"
                os.environ["CONVEYOR_PI_MODEL"] = "step-5-preview"
                os.environ["CONVEYOR_PI_BASE_URL"] = override_url
                os.environ["CONVEYOR_PI_WIRE_API"] = "chat"
                selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
            data = json.loads(selection.models_json or "")
            provider = data["providers"]["stepfun"]
            self.assertEqual(provider["baseUrl"], override_url)
            self.assertEqual(provider["api"], "openai-completions")
            self.assertEqual(provider["apiKey"], "$STEPFUN_API_KEY")
            self.assertNotIn(SECRET, selection.models_json or "")

    def test_mismatched_saved_provider_is_not_used(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            with patch.dict(os.environ, _clean_env(tmp, OPENAI_API_KEY=OTHER_SECRET), clear=True):
                _save_provider(
                    settings,
                    provider_id="other-ai",
                    provider_name="Other",
                    model="other-model",
                    base_url=OTHER_URL,
                    wire_api="chat",
                    env_key="OPENAI_API_KEY",
                    api_key=OTHER_SECRET,
                )
                os.environ["CONVEYOR_PI_PROVIDER"] = "stepfun"
                os.environ["CONVEYOR_PI_MODEL"] = "step-5-preview"
                os.environ["STEPFUN_API_KEY"] = SECRET
                selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
                self.assertTrue(selection.config_error)
                self.assertIn("stepfun", selection.config_error)
                self.assertIsNone(selection.models_json)
                with self.assertRaises(PiProviderConfigError):
                    prepare_pi_child_env({}, settings, selection)
            blob = ""
            root = Path(settings.codex_task_root) / "pi-agent"
            if root.exists():
                blob = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("models.json"))
            self.assertNotIn(OTHER_URL, blob)
            self.assertNotIn(OTHER_SECRET, blob)
            self.assertNotIn(SECRET, blob)

    def test_chat_endpoint_matches_host_and_uses_completions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            object.__setattr__(settings, "chat_model", "chat-model-should-not-leak")
            with patch.dict(
                os.environ,
                _clean_env(
                    tmp,
                    CONVEYOR_PI_PROVIDER="stepfun",
                    CONVEYOR_PI_MODEL="step-5-preview",
                    CONVEYOR_CHAT_BASE_URL=STEP_URL,
                    CONVEYOR_CHAT_API_KEY=SECRET,
                ),
                clear=True,
            ):
                selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
            self.assertFalse(selection.config_error, selection.config_error)
            data = json.loads(selection.models_json or "")
            provider = data["providers"]["stepfun"]
            self.assertEqual(provider["baseUrl"], STEP_URL)
            self.assertEqual(provider["api"], "openai-completions")
            self.assertEqual(provider["apiKey"], "$STEPFUN_API_KEY")
            self.assertEqual(selection.model, "step-5-preview")
            self.assertNotIn("chat-model-should-not-leak", selection.models_json or "")
            self.assertNotIn(SECRET, selection.models_json or "")

    def test_supervisor_does_not_adopt_chat_model_when_model_blank(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            object.__setattr__(settings, "chat_model", "chat-model-should-not-leak")
            with patch.dict(
                os.environ,
                _clean_env(
                    tmp,
                    CONVEYOR_PI_PROVIDER="stepfun",
                    CONVEYOR_PI_BASE_URL=STEP_URL,
                    STEPFUN_API_KEY=SECRET,
                ),
                clear=True,
            ):
                selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
            self.assertIn("CONVEYOR_PI_MODEL", selection.config_error)
            self.assertNotIn("chat-model-should-not-leak", selection.config_error)
            self.assertEqual(selection.model, "")

    def test_stale_plain_env_name_is_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            with patch.dict(
                os.environ,
                _clean_env(
                    tmp,
                    CONVEYOR_PI_PROVIDER="stepfun",
                    CONVEYOR_PI_MODEL="step-5-preview",
                    CONVEYOR_PI_BASE_URL=STEP_URL,
                    STEPFUN_API_KEY=SECRET,
                ),
                clear=True,
            ):
                selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
                directory = pi_coding_agent_dir(settings, selection.models_json or "")
                directory.mkdir(parents=True)
                (directory / "models.json").write_text(
                    json.dumps({"providers": {"stepfun": {"apiKey": "STEPFUN_API_KEY", "baseUrl": "https://stale.example"}}}),
                    encoding="utf-8",
                )
                legacy = Path(settings.codex_task_root) / "pi-agent" / "models.json"
                legacy.write_text('{"apiKey": "STEPFUN_API_KEY"}', encoding="utf-8")
                child: dict[str, str] = {}
                prepare_pi_child_env(child, settings, selection)
            fresh = Path(child["PI_CODING_AGENT_DIR"]) / "models.json"
            text = fresh.read_text(encoding="utf-8")
            self.assertIn('"$STEPFUN_API_KEY"', text)
            self.assertNotIn('"apiKey": "STEPFUN_API_KEY"', text)
            self.assertNotEqual(fresh.resolve(), legacy.resolve())
            self.assertNotIn(SECRET, text)

    def test_distinct_selections_use_distinct_directories(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            dirs = []
            for url in (STEP_URL, "https://api.stepfun.example/other"):
                with patch.dict(
                    os.environ,
                    _clean_env(
                        tmp,
                        CONVEYOR_PI_PROVIDER="stepfun",
                        CONVEYOR_PI_MODEL="step-5-preview",
                        CONVEYOR_PI_BASE_URL=url,
                        STEPFUN_API_KEY=SECRET,
                    ),
                    clear=True,
                ):
                    selection = resolve_pi_provider(settings, model_fallback="", chat_model=False)
                    child: dict[str, str] = {}
                    prepare_pi_child_env(child, settings, selection)
                    dirs.append(child["PI_CODING_AGENT_DIR"])
                    text = (Path(child["PI_CODING_AGENT_DIR"]) / "models.json").read_text(encoding="utf-8")
                    self.assertIn(url, text)
                    self.assertNotIn(SECRET, child["PI_CODING_AGENT_DIR"])
            self.assertNotEqual(dirs[0], dirs[1])


class PiReadinessTests(unittest.TestCase):
    def _pi(self, settings, env: dict[str, str]):
        with patch.dict(os.environ, env, clear=True), \
                patch("conveyor.agents.worker_usage.fetch_usage", return_value=None):
            detection = {"pi": {"installed": True, "version": "1.5.0", "path": "/x/pi", "auth": "unknown"}}
            payload = workers._assemble_workers_payload(settings, detection)
        return next(worker for worker in payload["workers"] if worker["id"] == "pi")

    def test_missing_endpoint_or_key_is_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            object.__setattr__(settings, "chat_api_key", None)
            env = _clean_env(tmp, CONVEYOR_PI_PROVIDER="stepfun", CONVEYOR_PI_MODEL="step-5-preview")
            pi = self._pi(settings, env)
            self.assertEqual(pi["auth"], "needs_login")
            self.assertFalse(pi["provider"]["configured"])
            self.assertIn("接口", pi["auth_hint"])
            self.assertNotIn("验证远程", pi["auth_hint"])
            self.assertNotIn(SECRET, json.dumps(pi))

            env["CONVEYOR_PI_BASE_URL"] = STEP_URL
            pi = self._pi(settings, env)
            self.assertEqual(pi["auth"], "needs_login")
            self.assertIn("STEPFUN_API_KEY", pi["auth_hint"])
            self.assertIn("没有验证远程登录", pi["auth_hint"])

            env["STEPFUN_API_KEY"] = SECRET
            pi = self._pi(settings, env)
            self.assertEqual(pi["auth"], "ok")
            self.assertNotIn(SECRET, json.dumps(pi))

    def test_saved_provider_still_counts_as_configured(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            with patch.dict(os.environ, _clean_env(tmp), clear=True):
                for key in ("CONVEYOR_PI_PROVIDER", "CONVEYOR_PI_MODEL"):
                    os.environ.pop(key, None)
                _save_provider(settings)
                status = describe_pi_provider(settings)
                pi = self._pi(settings, dict(os.environ))
            self.assertTrue(status["configured"])
            self.assertEqual(pi["auth"], "ok")
            self.assertEqual(pi["provider"]["provider_name"], "StepFun")
            self.assertEqual(pi["provider"]["model"], "step-3")

    def test_executor_available_requires_local_config_not_remote_auth(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            fake = tmp / "pi"
            fake.write_text("#!/bin/sh\n", encoding="utf-8")
            fake.chmod(0o755)
            object.__setattr__(settings, "chat_api_key", None)
            with patch.dict(
                os.environ,
                _sup_env(CONVEYOR_PI_BIN=str(fake), CONVEYOR_PI_PROVIDER="stepfun", CONVEYOR_PI_MODEL="step-5-preview"),
                clear=True,
            ):
                ok, reason = PiExecutor(settings).available()
            self.assertFalse(ok)
            self.assertIn("接口", reason)
            self.assertNotIn("登录成功", reason)


class SupervisorPiAgreementTests(unittest.TestCase):
    def test_argv_and_env_share_one_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            env = _sup_env(
                CONVEYOR_PI_BIN="/bin/echo",
                CONVEYOR_PI_PROVIDER="stepfun",
                CONVEYOR_PI_MODEL="step-5-preview",
                CONVEYOR_PI_BASE_URL=STEP_URL,
                STEPFUN_API_KEY=SECRET,
                OPENAI_API_KEY=OTHER_SECRET,
                DEEPSEEK_API_KEY="sk-deepseek-other",
            )
            with patch.dict(os.environ, env, clear=True):
                argv = PiExecutor(settings)._argv(ANALYST, "/tmp")
                child = ags._child_env(settings, executors="pi")
            self.assertEqual(argv[argv.index("--provider") + 1], "stepfun")
            self.assertEqual(argv[argv.index("--model") + 1], "step-5-preview")
            self.assertNotIn(SECRET, argv)
            self.assertNotIn("--api-key", argv)
            models = Path(child["PI_CODING_AGENT_DIR"]) / "models.json"
            data = json.loads(models.read_text(encoding="utf-8"))
            self.assertEqual(data["providers"]["stepfun"]["models"][0]["id"], "step-5-preview")
            self.assertEqual(data["providers"]["stepfun"]["baseUrl"], STEP_URL)
            self.assertEqual(data["providers"]["stepfun"]["apiKey"], "$STEPFUN_API_KEY")
            self.assertEqual(child.get("STEPFUN_API_KEY"), SECRET)
            self.assertNotIn("OPENAI_API_KEY", child)
            self.assertNotIn("DEEPSEEK_API_KEY", child)
            self.assertNotIn(SECRET, models.read_text(encoding="utf-8"))

    def test_bridge_write_failure_does_not_launch_another_provider(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            env_file = tmp / "provider.env"
            with patch.dict(
                os.environ,
                _clean_env(tmp, CONVEYOR_ENV_FILE=str(env_file), DEEPSEEK_API_KEY="sk-fallback"),
                clear=True,
            ):
                _save_provider(settings, provider_id="stepfun", base_url=STEP_URL, model="step-5-preview")
                os.environ["CONVEYOR_PI_PROVIDER"] = "stepfun"
                os.environ["CONVEYOR_PI_MODEL"] = "step-5-preview"
                with patch("conveyor.core.provider_config._atomic_write", side_effect=OSError("read-only fs")):
                    with self.assertRaises(PiProviderConfigError) as ctx:
                        ags._child_env(settings, executors="pi")
            self.assertIn("models.json", str(ctx.exception))
            self.assertNotIn(SECRET, str(ctx.exception))
            self.assertNotIn("sk-fallback", str(ctx.exception))
            # save_provider_config itself writes files; the failed bridge must not
            # leave a Pi config that names a different provider.
            root = Path(settings.codex_task_root) / "pi-agent"
            if root.exists():
                for path in root.rglob("models.json"):
                    self.assertNotIn("deepseek", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
