"""tests/test_supervisor_pi_executor.py — Supervisor "pi" executor.

Uses the built-in Pi coding agent as a Supervisor executor for all three roles.
No real model CLI is run: argv/parser/env are exercised directly, and the one
end-to-end test drives the real ``_spawn_cli`` subprocess path with a fake
``pi`` binary pointed at via ``CONVEYOR_PI_BIN`` (mirrors
scripts/supervisor_pool_smoke.py and the round2 executor tests).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("CONVEYOR_AGENT_SUPERVISOR_ENABLED", "true")

import agent_supervisor as ags
from agent_supervisor import PiExecutor, RoleConfig
from config import Settings
from supervisor_service import _UI_EXPOSED_EXECUTORS

ANALYST = RoleConfig(name="analyst", executor="pi", tools=["read"], location="worktree")
REVIEWER = RoleConfig(name="reviewer", executor="pi", tools=["read"], location="worktree")
IMPLEMENTER = RoleConfig(name="implementer", executor="pi", tools=["read", "write"], location="worktree")

_ISOLATION_FLAGS = (
    "-p", "--mode", "json", "--no-session", "--no-extensions", "--no-mcp",
    "--no-skills", "--no-prompt-templates", "--no-themes", "--offline", "--no-approve",
)

_FAKE_PI = r'''#!__PY__
import json, os, sys
sys.stdin.read()
argv = sys.argv[1:]
tools = argv[argv.index("--tools") + 1] if "--tools" in argv else ""
if "write" in tools.split(","):
    with open("pi_feature.txt", "w", encoding="utf-8") as fh:
        fh.write("pi implementer wrote this\n")
text = "implemented the feature"
print(json.dumps({"type": "message_end", "message": {"role": "assistant",
    "usage": {"input": 12, "output": 4, "cacheRead": 0},
    "content": [{"type": "text", "text": text}]}}), flush=True)
print(json.dumps({"type": "agent_end", "messages": [
    {"role": "assistant", "content": [{"type": "text", "text": text}]}]}), flush=True)
'''


def _clean_env(**extra: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp"}
    env.update(extra)
    return env


def _make_settings(tmp: Path) -> Settings:
    return Settings(
        telegram_bot_token="",
        telegram_allowed_user_id=0,
        codex_workspace_root=tmp,
        codex_bin="codex",
        codex_task_root=tmp / "tasks",
        codex_model=None,
        codex_timeout_seconds=60,
        telegram_progress_seconds=3,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=tmp / "memory",
        user_timezone="UTC",
        agent_supervisor_enabled=True,
        agent_supervisor_task_timeout_seconds=30,
    )


def _git(*argv: str, cwd: Path) -> str:
    r = subprocess.run(["git", *argv], cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {argv} failed: {r.stderr[:300]}")
    return (r.stdout or "").strip()


def _make_repo_and_worktree(tmp: Path) -> tuple[Path, Path, str]:
    repo = tmp / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "pi-exec@test.local", cwd=repo)
    _git("config", "user.name", "PiExec", cwd=repo)
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git("add", "app.py", cwd=repo)
    _git("commit", "-qm", "initial", cwd=repo)
    head = _git("rev-parse", "HEAD", cwd=repo)
    wt = tmp / "wt"
    _git("worktree", "add", "--detach", str(wt), head, cwd=repo)
    return repo, wt, head


def _write_fake_pi(path: Path, template: str = _FAKE_PI) -> Path:
    path.write_text(template.replace("__PY__", sys.executable), encoding="utf-8")
    path.chmod(0o755)
    return path


def _argv_for(settings: Settings, role: RoleConfig, **env: str) -> list[str]:
    with patch.dict(os.environ, _clean_env(**env), clear=True):
        return PiExecutor(settings)._argv(role, "/tmp")


class PiExecutorArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pi-argv-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.settings = _make_settings(self.tmp)

    def test_readonly_roles_use_read_only_tool_allowlist(self) -> None:
        for role in (ANALYST, REVIEWER):
            argv = _argv_for(self.settings, role)
            tools = argv[argv.index("--tools") + 1]
            self.assertEqual(tools, "read,grep,find,ls", role.name)
            for forbidden in ("bash", "edit", "write"):
                self.assertNotIn(forbidden, tools.split(","), role.name)

    def test_implementer_uses_write_tool_allowlist(self) -> None:
        argv = _argv_for(self.settings, IMPLEMENTER)
        tools = argv[argv.index("--tools") + 1]
        self.assertEqual(tools, "read,bash,edit,write,grep,find,ls")
        for needed in ("read", "bash", "edit", "write", "grep", "find", "ls"):
            self.assertIn(needed, tools.split(","))

    def test_isolation_flags_and_mode_present(self) -> None:
        argv = _argv_for(self.settings, ANALYST)
        for flag in _ISOLATION_FLAGS:
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "json")

    def test_never_passes_api_key(self) -> None:
        argv = _argv_for(self.settings, IMPLEMENTER, CONVEYOR_PI_PROVIDER="stepfun",
                         CONVEYOR_PI_MODEL="step-x", STEPFUN_API_KEY="test-value")
        self.assertNotIn("--api-key", argv)
        self.assertNotIn("test-value", argv)

    def test_model_present_only_when_configured(self) -> None:
        # Nothing configured -> --model omitted, Pi picks its own default.
        argv = _argv_for(self.settings, ANALYST)
        self.assertNotIn("--model", argv)
        self.assertNotIn("--provider", argv)
        # Explicit model -> --model present with that value.
        argv = _argv_for(self.settings, ANALYST, CONVEYOR_PI_PROVIDER="stepfun",
                         CONVEYOR_PI_MODEL="step-x")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "step-x")
        self.assertEqual(argv[argv.index("--provider") + 1], "stepfun")

    def test_provider_never_passed_without_model(self) -> None:
        # pi 1.5.0 errors out when --provider is given without --model.
        argv = _argv_for(self.settings, ANALYST)
        has_provider = "--provider" in argv
        has_model = "--model" in argv
        self.assertEqual(has_provider, has_model)
        self.assertFalse(has_provider and not has_model)
        # Both present once a model is configured.
        argv = _argv_for(self.settings, IMPLEMENTER, CONVEYOR_PI_PROVIDER="stepfun",
                         CONVEYOR_PI_MODEL="step-x")
        self.assertIn("--provider", argv)
        self.assertIn("--model", argv)

    def test_role_policy_rejects_write_on_readonly_role(self) -> None:
        bad = RoleConfig(name="reviewer", executor="pi", tools=["read", "write"], location="worktree")
        with patch.dict(os.environ, _clean_env(), clear=True):
            with self.assertRaises(ags.SupervisorStateError):
                PiExecutor(self.settings)._argv(bad, "/tmp")

    def test_sanitized_argv_leaks_neither_prompt_nor_secret(self) -> None:
        argv = _argv_for(self.settings, ANALYST, CONVEYOR_PI_PROVIDER="stepfun",
                         CONVEYOR_PI_MODEL="step-x")
        sanitized = ags._sanitize_argv_for_evidence(argv, prompt="SUPER-SECRET-PROMPT")
        self.assertNotIn("--api-key", sanitized)
        self.assertNotIn("SUPER-SECRET-PROMPT", sanitized)
        self.assertIn("--tools", sanitized)
        self.assertIn("--provider", sanitized)


class PiParserTests(unittest.TestCase):
    def test_parse_pi_stream_returns_text_and_usage(self) -> None:
        sample = "\n".join([
            json.dumps({"type": "message_update",
                        "assistantMessageEvent": {"type": "text_delta", "delta": "Hel"}}),
            json.dumps({"type": "message_end", "message": {
                "role": "assistant",
                "usage": {"input": 100, "output": 10, "cacheRead": 20},
                "content": [{"type": "text", "text": "Hello world"}]}}),
            json.dumps({"type": "agent_end", "messages": [
                {"role": "assistant", "content": [{"type": "text", "text": "Hello world"}]}]}),
        ])
        text, meta = ags._parse_pi_stream(sample)
        self.assertEqual(text, "Hello world")
        self.assertFalse(meta["is_error"])
        # cacheRead folds into input_tokens (Codex-compatible naming).
        self.assertEqual(meta["usage"], {
            "input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 10,
        })

    def test_parse_pi_stream_error_fails_closed_even_on_zero_exit(self) -> None:
        sample = json.dumps({"type": "message_end", "message": {
            "role": "assistant", "content": [], "stopReason": "error",
            "errorMessage": '404 model "x" does not exist'}})
        text, meta = ags._parse_pi_stream(sample)
        self.assertEqual(text, "")
        self.assertTrue(meta["is_error"])
        self.assertIn("does not exist", meta["error"])

    def test_parse_pi_stream_missing_result(self) -> None:
        text, meta = ags._parse_pi_stream(json.dumps({"type": "turn_start"}))
        self.assertEqual(text, "")
        self.assertTrue(meta["is_error"])
        self.assertEqual(meta["subtype"], "missing_result")

    def test_reviewer_verdict_is_parseable_like_command_code(self) -> None:
        sha, fp = "a" * 40, "b" * 64
        verdict = {
            "passed": True, "target_sha": sha, "target_fingerprint": fp, "issues": [],
            "checks": [{"name": "c", "command": "cmd", "passed": True, "output": "ok"}],
            "summary": "looks good",
        }
        sample = json.dumps({"type": "agent_end", "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": json.dumps(verdict)}]}]})
        text, meta = ags._parse_pi_stream(sample)
        self.assertFalse(meta["is_error"])
        parsed = ags.parse_review_verdict(text, expected_sha=sha, expected_fingerprint=fp)
        self.assertTrue(parsed["passed"])


class PiExecutorWiringTests(unittest.TestCase):
    def test_get_executor_returns_pi_executor(self) -> None:
        self.assertIsInstance(ags.get_executor(SimpleNamespace(), "pi"), PiExecutor)

    def test_ui_exposed_executors_contains_pi(self) -> None:
        names = [name for name, _label in _UI_EXPOSED_EXECUTORS]
        self.assertIn("pi", names)
        self.assertEqual(dict(_UI_EXPOSED_EXECUTORS)["pi"], "Pi（内置）")
        # Pool stays first (recommended); pi is listed right after it.
        self.assertEqual(_UI_EXPOSED_EXECUTORS[0][0], "pool")
        self.assertEqual(names[1], "pi")


class PiExecutorAvailableTests(unittest.TestCase):
    def test_available_false_with_missing_binary(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="pi-missing-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        settings = _make_settings(tmp)
        with patch.dict(os.environ, {"CONVEYOR_PI_BIN": "/no/such/pi-binary-xyz"}, clear=True):
            ex = PiExecutor(settings)
        ok, reason = ex.available()
        self.assertFalse(ok)
        self.assertIn("没有安装", reason)
        self.assertNotIn(tmp.name, reason)  # no paths for a non-technical owner

    def test_available_true_with_present_binary(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="pi-present-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        fake = _write_fake_pi(tmp / "pi")
        settings = _make_settings(tmp)
        with patch.dict(os.environ, {"CONVEYOR_PI_BIN": str(fake)}, clear=True):
            ex = PiExecutor(settings)
        ok, reason = ex.available()
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "ok")


class PiChildEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pi-env-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.settings = _make_settings(self.tmp)

    def test_explicit_provider_grants_only_its_key(self) -> None:
        env = _clean_env(
            CONVEYOR_PI_PROVIDER="stepfun",
            CONVEYOR_PI_MODEL="step-x",
            STEPFUN_API_KEY="test-value",
            DEEPSEEK_API_KEY="other-deepseek",
            OPENAI_API_KEY="other-openai",
            ANTHROPIC_API_KEY="other-anthropic",
            MINIMAX_API_KEY="other-minimax",
        )
        with patch.dict(os.environ, env, clear=True):
            child = ags._child_env(self.settings, executors="pi")
        self.assertEqual(child.get("STEPFUN_API_KEY"), "test-value")
        for leaked in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                       "ANTHROPIC_AUTH_TOKEN", "MINIMAX_API_KEY"):
            self.assertNotIn(leaked, child)

    def test_saved_provider_grants_only_its_key_and_bridges_models_json(self) -> None:
        import provider_config
        env_file = self.tmp / "provider.env"
        payload = {
            "provider_id": "step-fun", "provider_name": "StepFun", "model": "step-3",
            "base_url": "https://api.stepfun.example/v1", "wire_api": "chat",
            "env_key": "STEPFUN_API_KEY", "api_key": "saved-value",
        }
        with patch.dict(os.environ, _clean_env(CONVEYOR_ENV_FILE=str(env_file)), clear=True):
            provider_config.save_provider_config(self.settings, payload)
            child = ags._child_env(self.settings, executors="pi")
        self.assertEqual(child.get("STEPFUN_API_KEY"), "saved-value")
        self.assertNotIn("DEEPSEEK_API_KEY", child)
        self.assertNotIn("OPENAI_API_KEY", child)
        models = Path(self.settings.codex_task_root) / "pi-agent" / "models.json"
        self.assertTrue(models.is_file())
        self.assertEqual(child.get("PI_CODING_AGENT_DIR"), str(models.parent))
        # models.json references the key by name, never the raw value.
        self.assertNotIn("saved-value", models.read_text(encoding="utf-8"))
        self.assertIn("$STEPFUN_API_KEY", models.read_text(encoding="utf-8"))


class PiExecutorEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pi-e2e-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo, self.wt, self.head = _make_repo_and_worktree(self.tmp)
        self.settings = _make_settings(self.tmp)

    def test_implementer_run_writes_file_and_returns_output(self) -> None:
        fake_pi = _write_fake_pi(self.tmp / "fake-pi")
        task = ags.create_task(self.settings, goal="add pi feature",
                               repo_path=str(self.repo), worktree_path=str(self.wt),
                               base_sha=self.head)
        ags.dispatch_task(self.settings, task["id"], worktree_path=str(self.wt))
        with patch.dict(os.environ, _clean_env(CONVEYOR_PI_BIN=str(fake_pi)), clear=True):
            ex = PiExecutor(self.settings)
            result = asyncio.run(ex.run(
                role=IMPLEMENTER, prompt="implement the feature",
                worktree_path=str(self.wt), timeout_seconds=30, task_id=task["id"]))
        self.assertTrue(result.success, result.error)
        self.assertIn("implemented the feature", result.output)
        self.assertEqual(result.evidence.get("return_code"), 0)
        self.assertTrue((self.wt.resolve() / "pi_feature.txt").is_file())

    def test_readonly_role_run_returns_output_without_writing(self) -> None:
        fake_pi = _write_fake_pi(self.tmp / "fake-pi-ro")
        task = ags.create_task(self.settings, goal="analyze",
                               repo_path=str(self.repo), worktree_path=str(self.wt),
                               base_sha=self.head)
        ags.dispatch_task(self.settings, task["id"], worktree_path=str(self.wt))
        with patch.dict(os.environ, _clean_env(CONVEYOR_PI_BIN=str(fake_pi)), clear=True):
            ex = PiExecutor(self.settings)
            result = asyncio.run(ex.run(
                role=ANALYST, prompt="analyze the code",
                worktree_path=str(self.wt), timeout_seconds=30, task_id=task["id"]))
        self.assertTrue(result.success, result.error)
        self.assertIn("implemented the feature", result.output)
        # Read-only allowlist means the fake never enters the write branch.
        self.assertFalse((self.wt.resolve() / "pi_feature.txt").exists())


if __name__ == "__main__":
    unittest.main()
