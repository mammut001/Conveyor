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

from conveyor.agents import agent_supervisor as ags
from conveyor.agents.agent_supervisor import PiExecutor, RoleConfig
from conveyor.core.config import Settings
from conveyor.agents.supervisor_service import _UI_EXPOSED_EXECUTORS

ANALYST = RoleConfig(name="analyst", executor="pi", tools=["read"], location="worktree")
REVIEWER = RoleConfig(name="reviewer", executor="pi", tools=["read"], location="worktree")
IMPLEMENTER = RoleConfig(name="implementer", executor="pi", tools=["read", "write"], location="worktree")

_ISOLATION_FLAGS = (
    "-p", "--mode", "json", "--no-session", "--no-extensions", "--no-mcp",
    "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files",
    "--offline", "--no-approve",
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

    def test_no_context_files_passed_for_all_roles(self) -> None:
        for role in (ANALYST, REVIEWER, IMPLEMENTER):
            argv = _argv_for(self.settings, role)
            self.assertIn("--no-context-files", argv, role.name)

    def test_argv_fails_when_models_json_write_fails(self) -> None:
        from conveyor.core import provider_config
        env_file = self.tmp / "provider.env"
        payload = {
            "provider_id": "step-fun", "provider_name": "StepFun", "model": "step-3",
            "base_url": "https://api.stepfun.example/v1", "wire_api": "chat",
            "env_key": "STEPFUN_API_KEY", "api_key": "saved-value",
        }
        with patch.dict(os.environ, _clean_env(CONVEYOR_ENV_FILE=str(env_file)), clear=True):
            provider_config.save_provider_config(self.settings, payload)
            with patch("conveyor.core.provider_config._atomic_write", side_effect=OSError("disk full")):
                with self.assertRaises(RuntimeError) as ctx:
                    PiExecutor(self.settings)._argv(REVIEWER, "/tmp")
        self.assertIn("models.json", str(ctx.exception))
        self.assertNotIn("saved-value", str(ctx.exception))

    def test_never_passes_api_key(self) -> None:
        argv = _argv_for(self.settings, IMPLEMENTER, CONVEYOR_PI_PROVIDER="stepfun",
                         CONVEYOR_PI_MODEL="step-x", STEPFUN_API_KEY="test-value",
                         CONVEYOR_PI_BASE_URL="https://api.stepfun.example/v1")
        self.assertNotIn("--api-key", argv)
        self.assertNotIn("test-value", argv)

    def test_model_present_only_when_configured(self) -> None:
        # Nothing configured -> --model omitted, Pi picks its own default.
        argv = _argv_for(self.settings, ANALYST)
        self.assertNotIn("--model", argv)
        self.assertNotIn("--provider", argv)
        # Explicit model -> --model present with that value.
        argv = _argv_for(self.settings, ANALYST, CONVEYOR_PI_PROVIDER="stepfun",
                         CONVEYOR_PI_MODEL="step-x", STEPFUN_API_KEY="sk-test-step",
                         CONVEYOR_PI_BASE_URL="https://api.stepfun.example/v1")
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
                         CONVEYOR_PI_MODEL="step-x", STEPFUN_API_KEY="sk-test-step",
                         CONVEYOR_PI_BASE_URL="https://api.stepfun.example/v1")
        self.assertIn("--provider", argv)
        self.assertIn("--model", argv)

    def test_role_policy_rejects_write_on_readonly_role(self) -> None:
        bad = RoleConfig(name="reviewer", executor="pi", tools=["read", "write"], location="worktree")
        with patch.dict(os.environ, _clean_env(), clear=True):
            with self.assertRaises(ags.SupervisorStateError):
                PiExecutor(self.settings)._argv(bad, "/tmp")

    def test_sanitized_argv_leaks_neither_prompt_nor_secret(self) -> None:
        argv = _argv_for(self.settings, ANALYST, CONVEYOR_PI_PROVIDER="stepfun",
                         CONVEYOR_PI_MODEL="step-x", STEPFUN_API_KEY="sk-test-step",
                         CONVEYOR_PI_BASE_URL="https://api.stepfun.example/v1")
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

    def test_intermediate_text_without_terminal_event_is_not_final(self) -> None:
        sample = json.dumps({"type": "message_end", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "I'll inspect this"}],
            "stopReason": "toolUse"}})
        text, meta = ags._parse_pi_stream(sample)
        self.assertEqual(text, "")
        self.assertTrue(meta["is_error"])

    def test_truncated_or_unfinished_terminal_response_fails_closed(self) -> None:
        for reason in ("length", "aborted", "toolUse", "pending", "deferred"):
            with self.subTest(reason=reason):
                sample = json.dumps({"type": "agent_end", "messages": [{"role": "assistant",
                    "stopReason": reason, "content": [{"type": "text", "text": "partial"}]}]})
                text, meta = ags._parse_pi_stream(sample)
                self.assertEqual(text, "")
                self.assertTrue(meta["is_error"])
                self.assertIn(reason, meta["error"])

    def test_terminal_event_does_not_reuse_earlier_assistant_text(self) -> None:
        sample = json.dumps({"type": "agent_end", "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": "earlier"}]},
            {"role": "assistant", "stopReason": "toolUse", "content": [{"type": "toolCall", "name": "read"}]},
        ]})
        text, meta = ags._parse_pi_stream(sample)
        self.assertEqual(text, "")
        self.assertTrue(meta["is_error"])

    def test_duplicate_terminal_events_do_not_claim_success(self) -> None:
        end = {"type": "agent_end", "messages": [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}]}
        text, meta = ags._parse_pi_stream("\n".join((json.dumps(end), json.dumps(end))))
        self.assertEqual(text, "")
        self.assertTrue(meta["is_error"])

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
        with patch.dict(os.environ, {"CONVEYOR_PI_BIN": str(fake), "DEEPSEEK_API_KEY": "sk-test-deepseek"}, clear=True):
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
            CONVEYOR_PI_BASE_URL="https://api.stepfun.example/v1",
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
        from conveyor.core import provider_config
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
        models = Path(child["PI_CODING_AGENT_DIR"]) / "models.json"
        self.assertTrue(models.is_file())
        self.assertEqual(child.get("PI_CODING_AGENT_DIR"), str(models.parent))
        self.assertNotEqual(models.parent, Path(self.settings.codex_task_root) / "pi-agent")
        # models.json references the key by name, never the raw value.
        self.assertNotIn("saved-value", models.read_text(encoding="utf-8"))
        self.assertIn("$STEPFUN_API_KEY", models.read_text(encoding="utf-8"))

    def test_saved_provider_models_json_oserror_does_not_switch_provider(self) -> None:
        from conveyor.core import provider_config
        env_file = self.tmp / "provider.env"
        payload = {
            "provider_id": "step-fun", "provider_name": "StepFun", "model": "step-3",
            "base_url": "https://api.stepfun.example/v1", "wire_api": "chat",
            "env_key": "STEPFUN_API_KEY", "api_key": "saved-value",
        }
        with patch.dict(
            os.environ,
            _clean_env(CONVEYOR_ENV_FILE=str(env_file), DEEPSEEK_API_KEY="ds-fallback"),
            clear=True,
        ):
            provider_config.save_provider_config(self.settings, payload)
            with patch("conveyor.core.provider_config._atomic_write", side_effect=OSError("read-only fs")):
                with self.assertRaises(RuntimeError) as env_ctx:
                    ags._child_env(self.settings, executors="pi")
                with self.assertRaises(RuntimeError) as argv_ctx:
                    PiExecutor(self.settings)._argv(ANALYST, "/tmp")
        self.assertIn("models.json", str(env_ctx.exception))
        self.assertIn("models.json", str(argv_ctx.exception))
        self.assertNotIn("saved-value", str(env_ctx.exception))
        self.assertNotIn("ds-fallback", str(env_ctx.exception))


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

    def test_zero_exit_with_only_intermediate_message_is_failure(self) -> None:
        fake_pi = _write_fake_pi(self.tmp / "fake-pi-interrupted", r'''#!__PY__
import json, sys
sys.stdin.read()
print(json.dumps({"type": "message_end", "message": {"role": "assistant",
    "stopReason": "toolUse", "content": [{"type": "text", "text": "working on it"}]}}))
''')
        task = ags.create_task(self.settings, goal="analyze", repo_path=str(self.repo),
                               worktree_path=str(self.wt), base_sha=self.head)
        ags.dispatch_task(self.settings, task["id"], worktree_path=str(self.wt))
        with patch.dict(os.environ, _clean_env(CONVEYOR_PI_BIN=str(fake_pi)), clear=True):
            result = asyncio.run(PiExecutor(self.settings).run(
                role=ANALYST, prompt="analyze", worktree_path=str(self.wt),
                timeout_seconds=30, task_id=task["id"]))
        self.assertFalse(result.success)
        self.assertEqual(result.evidence.get("return_code"), 0)
        self.assertIn("final pi result", result.error)

    def test_pi_stdout_line_filter_drops_streaming_updates_and_keeps_diagnostics(self) -> None:
        self.assertFalse(ags._pi_stdout_line_filter(b'{"type":"message_update","delta":"x"}\n'))
        self.assertFalse(ags._pi_stdout_line_filter(b'{"type":"tool_execution_update","partialResult":{}}\n'))
        for kept in (
            b'{"type":"message_end","message":{"role":"assistant"}}\n',
            b'{"type":"agent_end","messages":[]}\n',
            b'{"type":"tool_execution_start","toolName":"read"}\n',
            b'{"type":"tool_execution_end","toolCallId":"t1"}\n',
            b'{"type":"error","message":"boom"}\n',
            b"plain text error from cli\n",
            b"{broken json\n",
        ):
            self.assertTrue(ags._pi_stdout_line_filter(kept), kept)

    def test_six_mib_message_update_lines_succeed_and_parse_result(self) -> None:
        fake_pi = _write_fake_pi(self.tmp / "fake-pi-6mib-updates", r'''#!__PY__
import json, sys
sys.stdin.read()
pad = "tok " * 500
line = json.dumps({
    "type": "message_update",
    "assistantMessageEvent": {"type": "text_delta", "delta": "x", "partial": pad},
})
target = 6 * 1024 * 1024 + 65536
written = 0
while written < target:
    sys.stdout.write(line + "\n")
    written += len(line) + 1
sys.stdout.flush()
text = "readme intro ready"
print(json.dumps({"type": "message_end", "message": {"role": "assistant",
    "usage": {"input": 10, "output": 5, "cacheRead": 0},
    "content": [{"type": "text", "text": text}]}}), flush=True)
print(json.dumps({"type": "agent_end", "messages": [
    {"role": "assistant", "content": [{"type": "text", "text": text}]}]}), flush=True)
''')
        task = ags.create_task(self.settings, goal="analyze", repo_path=str(self.repo),
                               worktree_path=str(self.wt), base_sha=self.head)
        ags.dispatch_task(self.settings, task["id"], worktree_path=str(self.wt))
        with patch.dict(os.environ, _clean_env(CONVEYOR_PI_BIN=str(fake_pi)), clear=True):
            result = asyncio.run(PiExecutor(self.settings).run(
                role=ANALYST, prompt="analyze", worktree_path=str(self.wt),
                timeout_seconds=30, task_id=task["id"]))
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.output, "readme intro ready")
        self.assertEqual(result.error, "")
        self.assertNotIn("message_update", result.evidence.get("stdout_excerpt", ""))
        evidence_data = json.loads(Path(result.evidence["evidence_path"]).read_text(encoding="utf-8"))
        self.assertGreater(evidence_data.get("output_bytes", 0), 6 * 1024 * 1024)
        self.assertEqual(result.evidence.get("meta", {}).get("usage"), {
            "input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5,
        })

    def test_six_mib_non_filtered_lines_fail_with_retained_limit(self) -> None:
        fake_pi = _write_fake_pi(self.tmp / "fake-pi-6mib-retained", r'''#!__PY__
import json, sys
sys.stdin.read()
pad = "z" * 2048
line = json.dumps({"type": "tool_execution_start", "toolName": "read", "args": {"path": pad}})
target = 6 * 1024 * 1024
written = 0
while written < target:
    try:
        sys.stdout.write(line + "\n")
        written += len(line) + 1
    except BrokenPipeError:
        break
''')
        task = ags.create_task(self.settings, goal="analyze", repo_path=str(self.repo),
                               worktree_path=str(self.wt), base_sha=self.head)
        ags.dispatch_task(self.settings, task["id"], worktree_path=str(self.wt))
        with patch.dict(os.environ, _clean_env(CONVEYOR_PI_BIN=str(fake_pi)), clear=True):
            result = asyncio.run(PiExecutor(self.settings).run(
                role=ANALYST, prompt="analyze", worktree_path=str(self.wt),
                timeout_seconds=30, task_id=task["id"]))
        self.assertFalse(result.success)
        self.assertIn("retained output byte limit", result.error)
        self.assertIn(str(ags.OUTPUT_BYTE_LIMIT), result.error)

    def test_single_five_mib_line_fails_with_limit(self) -> None:
        fake_pi = _write_fake_pi(self.tmp / "fake-pi-5mib-line", r'''#!__PY__
import json, sys
sys.stdin.read()
line = json.dumps({"type": "message_update", "big": "a" * (5 * 1024 * 1024)})
try:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
except BrokenPipeError:
    pass
''')
        task = ags.create_task(self.settings, goal="analyze", repo_path=str(self.repo),
                               worktree_path=str(self.wt), base_sha=self.head)
        ags.dispatch_task(self.settings, task["id"], worktree_path=str(self.wt))
        with patch.dict(os.environ, _clean_env(CONVEYOR_PI_BIN=str(fake_pi)), clear=True):
            result = asyncio.run(PiExecutor(self.settings).run(
                role=ANALYST, prompt="analyze", worktree_path=str(self.wt),
                timeout_seconds=30, task_id=task["id"]))
        self.assertFalse(result.success)
        self.assertIn("exceeded output byte limit", result.error)
        self.assertIn(str(ags.OUTPUT_BYTE_LIMIT), result.error)

    def test_raw_cap_kills_flood_of_filtered_lines(self) -> None:
        fake_pi = _write_fake_pi(self.tmp / "fake-pi-raw-flood", r'''#!__PY__
import json, sys
sys.stdin.read()
line = json.dumps({"type": "message_update", "delta": "x" * 1024}) + "\n"
while True:
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except BrokenPipeError:
        break
''')
        task = ags.create_task(self.settings, goal="analyze", repo_path=str(self.repo),
                               worktree_path=str(self.wt), base_sha=self.head)
        ags.dispatch_task(self.settings, task["id"], worktree_path=str(self.wt))
        with patch.object(ags, "PI_RAW_STDOUT_LIMIT", 256 * 1024):
            with patch.dict(os.environ, _clean_env(CONVEYOR_PI_BIN=str(fake_pi)), clear=True):
                result = asyncio.run(PiExecutor(self.settings).run(
                    role=ANALYST, prompt="analyze", worktree_path=str(self.wt),
                    timeout_seconds=30, task_id=task["id"]))
        self.assertFalse(result.success)
        self.assertIn("raw output byte limit", result.error)
        self.assertIn(str(256 * 1024), result.error)

    def test_non_json_error_lines_remain_visible_on_failure(self) -> None:
        fake_pi = _write_fake_pi(self.tmp / "fake-pi-nonjson-err", r'''#!__PY__
import json, sys
sys.stdin.read()
print(json.dumps({"type": "message_update", "delta": "hi"}))
print("fatal: upstream provider rejected request")
sys.exit(1)
''')
        task = ags.create_task(self.settings, goal="analyze", repo_path=str(self.repo),
                               worktree_path=str(self.wt), base_sha=self.head)
        ags.dispatch_task(self.settings, task["id"], worktree_path=str(self.wt))
        with patch.dict(os.environ, _clean_env(CONVEYOR_PI_BIN=str(fake_pi)), clear=True):
            result = asyncio.run(PiExecutor(self.settings).run(
                role=ANALYST, prompt="analyze", worktree_path=str(self.wt),
                timeout_seconds=30, task_id=task["id"]))
        self.assertFalse(result.success)
        self.assertIn("upstream provider rejected request", result.error)

    def test_other_executor_keeps_unfiltered_byte_limit_behaviour(self) -> None:
        fake_claude = _write_fake_pi(self.tmp / "fake-claude-flood", r'''#!__PY__
import json, sys
sys.stdin.read()
line = json.dumps({"type": "message_update", "delta": "x" * 2048}) + "\n"
written = 0
target = 5 * 1024 * 1024
while written < target:
    try:
        sys.stdout.write(line)
        written += len(line)
    except BrokenPipeError:
        sys.exit(0)
print(json.dumps({"type": "result", "result": "done", "is_error": False}))
''')
        claude_role = RoleConfig(name="analyst", executor="claude", tools=["read"], location="worktree")
        task = ags.create_task(self.settings, goal="analyze", repo_path=str(self.repo),
                               worktree_path=str(self.wt), base_sha=self.head)
        ags.dispatch_task(self.settings, task["id"], worktree_path=str(self.wt))
        with patch.dict(os.environ, _clean_env(CONVEYOR_CLAUDE_BIN=str(fake_claude)), clear=True):
            result = asyncio.run(ags.ClaudeExecutor(self.settings).run(
                role=claude_role, prompt="analyze", worktree_path=str(self.wt),
                timeout_seconds=30, task_id=task["id"]))
        self.assertFalse(result.success)
        self.assertIn("stdout exceeded output byte limit", result.error)
        self.assertNotIn("retained", result.error)
        self.assertNotIn("raw", result.error)


class PiRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pi-retry-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo, self.wt, self.head = _make_repo_and_worktree(self.tmp)
        self.settings = _make_settings(self.tmp)

    def test_thinking_level_env_overrides(self) -> None:
        # Analyst default is low.
        with patch.dict(os.environ, _clean_env(), clear=True):
            argv = PiExecutor(self.settings)._argv(ANALYST, "/tmp")
            self.assertIn("--thinking", argv)
            self.assertEqual(argv[argv.index("--thinking") + 1], "low")
        
        # Override
        with patch.dict(os.environ, _clean_env(CONVEYOR_PI_THINKING_ANALYST="high"), clear=True):
            argv = PiExecutor(self.settings)._argv(ANALYST, "/tmp")
            self.assertEqual(argv[argv.index("--thinking") + 1], "high")

    def test_length_retry_succeeds(self) -> None:
        # First call length error, second call success.
        fake_script = r'''#!__PY__
import json, sys, os
cwd = os.getcwd()
with open(os.path.join(cwd, "calls.txt"), "a") as f:
    f.write(" ".join(sys.argv) + "\n")
stdin = sys.stdin.read()
with open(os.path.join(cwd, "stdin.txt"), "a") as f:
    f.write(stdin + "\n---END---\n")
if not os.path.exists(os.path.join(cwd, "retry.marker")):
    with open(os.path.join(cwd, "retry.marker"), "w") as f: f.write("1")
    # First: error "length"
    print(json.dumps({"type": "agent_end", "messages": [
        {"role": "assistant", "stopReason": "length", "content": [{"type": "text", "text": "too long"}]}]}))
else:
    # Second: success
    print(json.dumps({"type": "agent_end", "messages": [
        {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "just right"}]}]}))
'''
        fake_pi = _write_fake_pi(self.tmp / "fake-pi-retry", fake_script)
        task = ags.create_task(self.settings, goal="retry", repo_path=str(self.repo),
                               worktree_path=str(self.wt), base_sha=self.head)
        
        env = _clean_env(
            CONVEYOR_PI_BIN=str(fake_pi),
            CALLS_PATH=str(self.tmp / "calls.txt"),
            STDIN_PATH=str(self.tmp / "stdin.txt"),
            MARKER_PATH=str(self.tmp / "retry.marker")
        )
        with patch.dict(os.environ, env, clear=True):
            ex = PiExecutor(self.settings)
            result = asyncio.run(ex.run(
                role=IMPLEMENTER, prompt="do something",
                worktree_path=str(self.wt), timeout_seconds=30, task_id=task["id"]))
        
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.output, "just right")
        
        # Verify 2 calls
        calls = (self.wt / "calls.txt").read_text().splitlines()
        self.assertEqual(len(calls), 2)
        # Verify 2nd call had minimal thinking and short prompt line
        self.assertIn("--thinking minimal", calls[1])
        
        # Verify 2nd call prompt has extra line
        stains = (self.wt / "stdin.txt").read_text().split("---END---\n")
        self.assertIn("Keep the answer short", stains[1])

    def test_concurrent_runs_keep_their_own_thinking_level(self) -> None:
        # One shared PiExecutor, two concurrent runs: run A hits a length stop
        # and retries with minimal thinking; run B is normal. B builds its argv
        # while A's retry is in flight and must still get the role default.
        argvs: dict[str, list[list[str]]] = {"A": [], "B": []}

        async def scenario() -> tuple[ags.ExecutionResult, ags.ExecutionResult]:
            a_retry_in_flight = asyncio.Event()
            b_argv_built = asyncio.Event()

            async def fake_spawn(settings, *, role, prompt, worktree_path,
                                 timeout_seconds, argv_builder, **_kw):
                run_id = "A" if prompt.startswith("run-a") else "B"
                if run_id == "B":
                    await a_retry_in_flight.wait()
                argv = ags._build_argv_for_spawn(argv_builder, role, worktree_path)
                argvs[run_id].append(argv)
                if run_id == "B":
                    b_argv_built.set()
                    return ags.ExecutionResult(success=True, output="b ok")
                if len(argvs["A"]) == 1:
                    return ags.ExecutionResult(
                        success=False, error="too long",
                        evidence={"meta": {"stop_reason": "length"}})
                a_retry_in_flight.set()
                await b_argv_built.wait()
                return ags.ExecutionResult(success=True, output="a ok")

            ex = PiExecutor(self.settings)
            with patch.object(ags, "_spawn_cli", fake_spawn):
                return await asyncio.wait_for(asyncio.gather(
                    ex.run(role=IMPLEMENTER, prompt="run-a", worktree_path=str(self.wt),
                           timeout_seconds=30),
                    ex.run(role=IMPLEMENTER, prompt="run-b", worktree_path=str(self.wt),
                           timeout_seconds=30),
                ), timeout=10)

        def thinking(argv: list[str]) -> str:
            return argv[argv.index("--thinking") + 1]

        with patch.dict(os.environ, _clean_env(), clear=True):
            res_a, res_b = asyncio.run(scenario())
        self.assertTrue(res_a.success, res_a.error)
        self.assertTrue(res_b.success, res_b.error)
        self.assertEqual([thinking(a) for a in argvs["A"]], ["medium", "minimal"])
        self.assertEqual([thinking(a) for a in argvs["B"]], ["medium"])
        self.assertFalse(hasattr(PiExecutor(self.settings), "thinking_override"))


if __name__ == "__main__":
    unittest.main()
