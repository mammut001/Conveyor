from __future__ import annotations

import asyncio
import dataclasses
import http.client
import http.server
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import provider_config
import workers
import workers_login
from config import Settings
from handlers.job_queue import JobQueue
from runner.pooled import PooledRunner
from runner.types import JobMode, JobState
from web_console import WebConsoleHandler, WebConsoleServer
from web_control import WebControl


def _make_settings(root: Path, workers_enabled: bool = True, workers_install_enabled: bool = False):
    task_root = root / "tasks"
    ws_root = root / "ws"
    mem_root = root / "mem"
    task_root.mkdir(parents=True, exist_ok=True)
    ws_root.mkdir(parents=True, exist_ok=True)
    mem_root.mkdir(parents=True, exist_ok=True)

    base = Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_id=12345,
        codex_workspace_root=ws_root,
        codex_bin="codex",
        codex_task_root=task_root,
        codex_model=None,
        codex_timeout_seconds=30,
        telegram_progress_seconds=5,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=mem_root,
        user_timezone="UTC",
        workers_enabled=workers_enabled,
        workers_install_enabled=workers_install_enabled,
    )
    return SimpleNamespace(**dataclasses.asdict(base))


class FakeBackend:
    def __init__(self, name: str, fail_error: Exception | None = None) -> None:
        self.name = name
        self.fail_error = fail_error
        self.current_job = SimpleNamespace(id=f"job-{name}", state="running")
        self.last_job = self.current_job
        self.calls: list[str] = []

    async def validate(self) -> None:
        self.calls.append("validate")

    async def start(self, mode: JobMode, prompt: str, on_progress: Any, **kwargs: Any) -> Any:
        self.calls.append(f"start:{self.name}:{prompt}")
        if self.fail_error:
            raise self.fail_error
        job = SimpleNamespace(id=f"job-{self.name}", prompt=prompt, mode=mode)
        self.current_job = job
        self.last_job = job
        return job

    async def cancel(self) -> str:
        self.calls.append("cancel")
        return f"cancelled-{self.name}"

    async def diff_job(self, job_id: str, worktree: Any | None = None) -> str:
        self.calls.append(f"diff:{job_id}")
        return f"diff:{self.name}:{job_id}"

    async def apply_job(self, job_id: str, worktree: Any | None = None) -> str:
        self.calls.append(f"apply:{job_id}")
        return f"apply:{self.name}:{job_id}"

    async def discard_job(self, job_id: str, worktree: Any | None = None) -> str:
        self.calls.append(f"discard:{job_id}")
        return f"discard:{self.name}:{job_id}"


class DeferredJob:
    def __init__(self, job_id: str, error: str | None = None, summary: str | None = None, delay: float = 0.02):
        self.id = job_id
        self.state = JobState.RUNNING
        self.error = None
        self.summary = None
        self.worktree_path = None
        self._target_error = error
        self._target_summary = summary
        self._delay = delay
        self._task = asyncio.create_task(self._finish())

    async def _finish(self):
        await asyncio.sleep(self._delay)
        if self._target_error:
            self.error = self._target_error
            self.state = JobState.FAILED
        else:
            self.summary = self._target_summary or "completed successfully"
            self.state = JobState.COMPLETED


class RealShapeFakeBackend:
    def __init__(self, name: str, error: str | None = None, summary: str | None = None, delay: float = 0.02):
        self.name = name
        self.error = error
        self.summary = summary
        self.delay = delay
        self.calls = []
        self.current_job = None
        self.last_job = None

    async def validate(self):
        self.calls.append("validate")

    async def start(self, mode: JobMode, prompt: str, on_progress: Any, **kwargs: Any) -> Any:
        self.calls.append(f"start:{self.name}:{prompt}")
        job = DeferredJob(f"job-{self.name}", error=self.error, summary=self.summary, delay=self.delay)
        self.current_job = job
        self.last_job = job
        return job

    async def diff_job(self, job_id: str, worktree: Any | None = None) -> str:
        return "Job: no git diff."

    async def apply_job(self, job_id: str, worktree: Any | None = None) -> str:
        return "Applied"

    async def discard_job(self, job_id: str, worktree: Any | None = None) -> str:
        return "Discarded"


class FakePort:
    def __init__(self):
        self.replies = []
        self.sent = []

    async def reply(self, msg, text, **kwargs):
        self.replies.append(text)

    async def send_new(self, msg, text):
        self.sent.append(text)


def _make_msg(text: str = "test task"):
    return SimpleNamespace(
        channel="telegram",
        chat_id=12345,
        operator_id=12345,
        text=text,
    )


class WorkerUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        workers.clear_detection_cache()

    def tearDown(self) -> None:
        workers.clear_detection_cache()

    def test_catalog_structure(self) -> None:
        catalog = workers.get_catalog()
        ids = [item["id"] for item in catalog]
        self.assertEqual(ids, ["codex", "claude-code", "pi", "agy", "cmd"])

        codex = workers.get_catalog_worker("codex")
        self.assertIsNotNone(codex)
        self.assertTrue(codex["runnable"])
        self.assertIsNotNone(codex["install_command"])

        agy = workers.get_catalog_worker("agy")
        self.assertIsNotNone(agy)
        self.assertFalse(agy["runnable"])
        self.assertEqual(agy["install_command"], "curl -fsSL https://antigravity.google/cli/install.sh | bash")
        self.assertEqual(agy["models_command"], ["agy", "models"])
        self.assertEqual(agy["auth_type"], "cmd")
        self.assertEqual(agy["auth_command"], ["agy", "models"])
        self.assertNotIn("auth_file", agy)

        pi = workers.get_catalog_worker("pi")
        self.assertIsNotNone(pi)
        self.assertIsNone(pi["install_command"])
        self.assertIsNone(pi["models_command"])

        cmd = workers.get_catalog_worker("cmd")
        self.assertIsNotNone(cmd)
        self.assertFalse(cmd["runnable"])

    @patch("shutil.which", return_value=None)
    def test_detection_uninstalled(self, _mock_which) -> None:
        worker_def = workers.get_catalog_worker("codex")
        result = workers.detect_worker(worker_def)
        self.assertFalse(result["installed"])
        self.assertIsNone(result["version"])
        self.assertIsNone(result["path"])
        self.assertEqual(result["auth"], "unknown")

    @patch("shutil.which", return_value="/usr/local/bin/codex")
    @patch("subprocess.run")
    def test_detection_installed_version_and_auth_ok(self, mock_run, _mock_which) -> None:
        def fake_run(args, **kwargs):
            if "--version" in args:
                return subprocess.CompletedProcess(args, 0, stdout="1.2.3\nextra", stderr="")
            if "login" in args:
                return subprocess.CompletedProcess(args, 0, stdout="Logged in", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        mock_run.side_effect = fake_run
        worker_def = workers.get_catalog_worker("codex")
        result = workers.detect_worker(worker_def)
        self.assertTrue(result["installed"])
        self.assertEqual(result["version"], "1.2.3")
        self.assertEqual(result["auth"], "ok")

    @patch("shutil.which", return_value="/usr/local/bin/codex")
    @patch("subprocess.run")
    def test_detection_codex_auth_fail(self, mock_run, _mock_which) -> None:
        def fake_run(args, **kwargs):
            if "--version" in args:
                return subprocess.CompletedProcess(args, 0, stdout="1.0.0", stderr="")
            if "login" in args:
                return subprocess.CompletedProcess(args, 1, stdout="Not logged in", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        mock_run.side_effect = fake_run
        worker_def = workers.get_catalog_worker("codex")
        result = workers.detect_worker(worker_def)
        self.assertTrue(result["installed"])
        self.assertEqual(result["auth"], "needs_login")

    @patch("shutil.which", return_value="/usr/local/bin/claude")
    @patch("subprocess.run")
    def test_detection_claude_auth_status(self, mock_run, _mock_which) -> None:
        def fake_run(args, **kwargs):
            if "auth" in args:
                return subprocess.CompletedProcess(args, 0, stdout="authenticated", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="2.0.0", stderr="")

        mock_run.side_effect = fake_run
        worker_def = workers.get_catalog_worker("claude-code")
        result = workers.detect_worker(worker_def)
        self.assertEqual(result["auth"], "ok")

    @patch("shutil.which", return_value="/usr/local/bin/pi")
    @patch("subprocess.run")
    def test_detection_pi_auth_unknown(self, mock_run, _mock_which) -> None:
        mock_run.return_value = subprocess.CompletedProcess(["pi", "--version"], 0, stdout="0.5.0", stderr="")
        worker_def = workers.get_catalog_worker("pi")
        result = workers.detect_worker(worker_def)
        self.assertEqual(result["auth"], "unknown")

    @patch("shutil.which", return_value="/usr/local/bin/agy")
    @patch("subprocess.run")
    def test_detection_agy_auth(self, mock_run, _mock_which) -> None:
        worker_def = workers.get_catalog_worker("agy")

        # 1. Models listed -> ok
        def fake_run_ok(args, **kwargs):
            if "--version" in args:
                return subprocess.CompletedProcess(args, 0, stdout="3.0.0", stderr="")
            if "models" in args:
                return subprocess.CompletedProcess(
                    args, 0, stdout="gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n", stderr=""
                )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        mock_run.side_effect = fake_run_ok
        result = workers.detect_worker(worker_def)
        self.assertTrue(result["installed"])
        self.assertEqual(result["version"], "3.0.0")
        self.assertEqual(result["auth"], "ok")

        # 2. "Error: not logged in" rc 1 -> needs_login
        def fake_run_not_logged_in(args, **kwargs):
            if "--version" in args:
                return subprocess.CompletedProcess(args, 0, stdout="3.0.0", stderr="")
            if "models" in args:
                return subprocess.CompletedProcess(args, 1, stdout="Error: not logged in\n", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        mock_run.side_effect = fake_run_not_logged_in
        result = workers.detect_worker(worker_def)
        self.assertEqual(result["auth"], "needs_login")

        # 3. Timeout -> unknown
        def fake_run_timeout(args, **kwargs):
            if "--version" in args:
                return subprocess.CompletedProcess(args, 0, stdout="3.0.0", stderr="")
            if "models" in args:
                raise subprocess.TimeoutExpired(cmd=args, timeout=20)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        mock_run.side_effect = fake_run_timeout
        result = workers.detect_worker(worker_def)
        self.assertEqual(result["auth"], "unknown")

        # 4. Non-zero rc with non-login failure -> unknown
        def fake_run_other_fail(args, **kwargs):
            if "--version" in args:
                return subprocess.CompletedProcess(args, 0, stdout="3.0.0", stderr="")
            if "models" in args:
                return subprocess.CompletedProcess(args, 1, stdout="connection refused\n", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        mock_run.side_effect = fake_run_other_fail
        result = workers.detect_worker(worker_def)
        self.assertEqual(result["auth"], "unknown")

        # 5. Non-zero rc with case-insensitive 401 / unauthorized / authenticate -> needs_login
        for err_msg in ["401 unauthorized", "Please login to proceed", "Failed to authenticate"]:
            def fake_run_auth_keyword(args, err=err_msg, **kwargs):
                if "--version" in args:
                    return subprocess.CompletedProcess(args, 0, stdout="3.0.0", stderr="")
                if "models" in args:
                    return subprocess.CompletedProcess(args, 1, stdout="", stderr=err)
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

            mock_run.side_effect = fake_run_auth_keyword
            result = workers.detect_worker(worker_def)
            self.assertEqual(result["auth"], "needs_login")

    @patch("shutil.which", return_value="/usr/local/bin/cmd")
    @patch("subprocess.run")
    def test_detection_cmd_auth_parsing(self, mock_run, _mock_which) -> None:
        worker_def = workers.get_catalog_worker("cmd")

        # Success case
        mock_run.return_value = subprocess.CompletedProcess(["cmd", "whoami"], 0, stdout="alice", stderr="")
        result = workers.detect_worker(worker_def)
        self.assertEqual(result["auth"], "ok")

        # 401 in text
        mock_run.return_value = subprocess.CompletedProcess(["cmd", "whoami"], 0, stdout="Error: 401 Unauthorized", stderr="")
        result = workers.detect_worker(worker_def)
        self.assertEqual(result["auth"], "needs_login")

        # Authentication failed
        mock_run.return_value = subprocess.CompletedProcess(["cmd", "whoami"], 0, stdout="Authentication failed for token", stderr="")
        result = workers.detect_worker(worker_def)
        self.assertEqual(result["auth"], "needs_login")

        # not logged
        mock_run.return_value = subprocess.CompletedProcess(["cmd", "whoami"], 0, stdout="User is not logged in", stderr="")
        result = workers.detect_worker(worker_def)
        self.assertEqual(result["auth"], "needs_login")

        # Non-zero returncode
        mock_run.return_value = subprocess.CompletedProcess(["cmd", "whoami"], 1, stdout="fatal", stderr="")
        result = workers.detect_worker(worker_def)
        self.assertEqual(result["auth"], "needs_login")

    @patch("shutil.which", return_value="/usr/local/bin/mock")
    @patch("subprocess.run")
    def test_detection_cache_60s(self, mock_run, _mock_which) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="1.0.0", stderr="")
        # First call populates cache
        r1 = workers.detect_all_workers()
        self.assertGreater(mock_run.call_count, 0)
        initial_calls = mock_run.call_count

        # Second call within 60s should use cache
        r2 = workers.detect_all_workers()
        self.assertEqual(mock_run.call_count, initial_calls)
        self.assertEqual(r1, r2)

        # Force bypasses cache
        workers.detect_all_workers(force=True)
        self.assertGreater(mock_run.call_count, initial_calls)

    def test_quota_failure_classification(self) -> None:
        # "Resets in 14h6m27s" -> 14*3600 + 6*60 + 27 = 50787 seconds
        result = workers.classify_failure("Resets in 14h6m27s")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "quota")
        self.assertEqual(result[1], 50787)

        # Compound message with rate limit
        result = workers.classify_failure("OpenAI error 429: rate limit reached. Resets in 2h30m.")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "quota")
        self.assertEqual(result[1], 2 * 3600 + 30 * 60)

        # Match other quota patterns with default 45 min
        for phrase in [
            "RESOURCE_EXHAUSTED: quota exceeded",
            "usage limit reached for today",
            "rate limit exceeded",
            "HTTP 429 Too Many Requests",
            "credit balance is too low",
        ]:
            res = workers.classify_failure(phrase)
            self.assertIsNotNone(res, f"Expected quota match for '{phrase}'")
            self.assertEqual(res[0], "quota")
            self.assertEqual(res[1], 45 * 60)

        # Non-quota error
        self.assertIsNone(workers.classify_failure("SyntaxError: invalid syntax on line 12"))
        self.assertIsNone(workers.classify_failure("Process exited with code 1"))

    def test_state_persistence_and_cooling(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir))
            state_path = workers.get_state_path(settings)
            self.assertFalse(state_path.exists())

            # Load default state
            state = workers.load_workers_state(settings)
            self.assertEqual(state["order"], ["codex", "claude-code", "pi", "agy", "cmd"])
            self.assertEqual(state["cooling"], {})

            # Mark cooling
            workers.mark_cooling(settings, "codex", reset_seconds=3600, reason="test 429")
            self.assertTrue(state_path.exists())

            state = workers.load_workers_state(settings)
            self.assertIn("codex", state["cooling"])
            self.assertEqual(state["cooling"]["codex"]["reason"], "test 429")
            self.assertGreater(state["cooling"]["codex"]["reset_at"], time.time())

            # Reset quota
            workers.reset_quota(settings, "codex")
            state = workers.load_workers_state(settings)
            self.assertNotIn("codex", state["cooling"])

    def test_pick_worker_order_and_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir))

            # Fake detection where codex, claude-code, and pi are installed and authed
            fake_detections = {
                "codex": {"installed": True, "version": "1.0", "path": "/bin/codex", "auth": "ok"},
                "claude-code": {"installed": True, "version": "2.0", "path": "/bin/claude", "auth": "ok"},
                "pi": {"installed": True, "version": "0.5", "path": "/bin/pi", "auth": "unknown"},
                "agy": {"installed": True, "version": "1.0", "path": "/bin/agy", "auth": "ok"},
                "cmd": {"installed": True, "version": "1.0", "path": "/bin/cmd", "auth": "ok"},
            }

            with patch("workers.detect_all_workers", return_value=fake_detections):
                # Default order: codex is picked first
                picked = workers.pick_worker(settings)
                self.assertIsNotNone(picked)
                self.assertEqual(picked["id"], "codex")

                # If codex is cooling, claude-code is picked next
                workers.mark_cooling(settings, "codex", reset_seconds=300)
                picked = workers.pick_worker(settings)
                self.assertIsNotNone(picked)
                self.assertEqual(picked["id"], "claude-code")

                # If claude-code is cooling, pi is picked next (auth 'unknown' is allowed!)
                workers.mark_cooling(settings, "claude-code", reset_seconds=300)
                picked = workers.pick_worker(settings)
                self.assertIsNotNone(picked)
                self.assertEqual(picked["id"], "pi")

                # If pi is cooling, agy and cmd are NOT picked because runnable=False
                workers.mark_cooling(settings, "pi", reset_seconds=300)
                picked = workers.pick_worker(settings)
                self.assertIsNone(picked)


class WorkerModelsUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        workers.clear_models_cache()

    def tearDown(self) -> None:
        workers.clear_models_cache()

    def test_parse_agy_models(self) -> None:
        sample = (
            "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
            "gemini-3.8-pro-high\tGemini 3.8 Pro (High)\n"
            "# Ignored comment\n"
            "claude-3-7-sonnet\tClaude 3.7 Sonnet\n"
        )
        models = workers.parse_agy_models(sample)
        self.assertEqual(len(models), 3)
        self.assertEqual(models[0], {"id": "gemini-3.8-flash-high", "label": "Gemini 3.8 Flash (High)"})
        self.assertEqual(models[1], {"id": "gemini-3.8-pro-high", "label": "Gemini 3.8 Pro (High)"})
        self.assertEqual(models[2], {"id": "claude-3-7-sonnet", "label": "Claude 3.7 Sonnet"})

    def test_parse_cmd_models(self) -> None:
        sample = (
            "Available Models:\n"
            "OpenAI Models:\n"
            "  openai/gpt-4o                 flagship omnichannel model\n"
            "DeepSeek Models:\n"
            "  deepseek/deepseek-v4-pro               hybrid-attention ...\n"
            "  deepseek/deepseek-chat                 conversational engine\n"
        )
        models = workers.parse_cmd_models(sample)
        self.assertEqual(len(models), 3)
        self.assertEqual(models[0], {"id": "openai/gpt-4o", "label": "flagship omnichannel model"})
        self.assertEqual(models[1], {"id": "deepseek/deepseek-v4-pro", "label": "hybrid-attention ..."})
        self.assertEqual(models[2], {"id": "deepseek/deepseek-chat", "label": "conversational engine"})

    def test_claude_code_fixed_models(self) -> None:
        models, _ = workers.get_worker_models("claude-code")
        ids = [m["id"] for m in models]
        self.assertEqual(ids, ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"])

    def test_codex_pi_models_unsupported(self) -> None:
        with self.assertRaises(workers.ModelsUnsupportedError):
            workers.get_worker_models("codex")
        with self.assertRaises(workers.ModelsUnsupportedError):
            workers.get_worker_models("pi")

    def test_models_caching_10min(self) -> None:
        with patch("shutil.which", return_value="/bin/agy"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["/bin/agy", "models"], 0, stdout="gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n", stderr=""
            )
            models1, cached1 = workers.get_worker_models("agy")
            self.assertEqual(len(models1), 1)
            mock_run.assert_called_once()

            # Second call within 10 min uses cache, no extra subprocess call
            models2, cached2 = workers.get_worker_models("agy")
            self.assertEqual(cached1, cached2)
            self.assertEqual(mock_run.call_count, 1)

            # force=True bypasses cache
            models3, cached3 = workers.get_worker_models("agy", force=True)
            self.assertEqual(mock_run.call_count, 2)

    def test_set_worker_model_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir))
            # Setting valid model
            payload = workers.set_worker_model(settings, "claude-code", "claude-sonnet-5-5")
            claude = next(w for w in payload["workers"] if w["id"] == "claude-code")
            self.assertEqual(claude["model"], "claude-sonnet-5-5")
            self.assertTrue(claude["models_supported"])
            self.assertTrue(claude["login_supported"])

            # Verify persisted to workers.json
            state = workers.load_workers_state(settings)
            self.assertEqual(state.get("models", {}).get("claude-code"), "claude-sonnet-5-5")

            # Unknown model raises error
            with self.assertRaises(workers.UnknownModelError):
                workers.set_worker_model(settings, "claude-code", "nonexistent-model")

            # Setting to None clears chosen model
            payload = workers.set_worker_model(settings, "claude-code", None)
            claude = next(w for w in payload["workers"] if w["id"] == "claude-code")
            self.assertIsNone(claude["model"])

            # Unsupported worker raises error
            with self.assertRaises(workers.ModelsUnsupportedError):
                workers.set_worker_model(settings, "codex", "any-model")


class WorkersLoginUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        workers_login.clear_login_sessions()

    def tearDown(self) -> None:
        workers_login.clear_login_sessions()

    def test_strip_ansi(self) -> None:
        raw = "\x1b[32mhttps://antigravity.google/oauth\x1b[0m\x1b[2K\n\x1b[1mPaste code here:\x1b[0m "
        clean = workers_login.strip_ansi(raw)
        self.assertEqual(clean, "https://antigravity.google/oauth\nPaste code here: ")

    def test_extract_url(self) -> None:
        # 1. agy SSH flow text
        agy_text = (
            "To authenticate Antigravity, please visit the following URL in your browser:\n"
            "https://accounts.google.com/o/oauth2/auth?client_id=12345.apps.googleusercontent.com&redirect_uri=urn:ietf:wg:oauth:2.0:oob\n"
            "Enter verification code: "
        )
        self.assertEqual(
            workers_login.extract_url(agy_text),
            "https://accounts.google.com/o/oauth2/auth?client_id=12345.apps.googleusercontent.com&redirect_uri=urn:ietf:wg:oauth:2.0:oob",
        )

        # 2. claude "Paste code here"
        claude_text = (
            "Please open (https://claude.ai/login?code=abc-123).\n"
            "Paste code here: "
        )
        self.assertEqual(workers_login.extract_url(claude_text), "https://claude.ai/login?code=abc-123")

        # 3. cmd / codex URLs
        cmd_text = "Please authenticate at 'https://command-code.com/auth/cli' to proceed.\nEnter code: "
        self.assertEqual(workers_login.extract_url(cmd_text), "https://command-code.com/auth/cli")

        codex_text = "Open https://platform.openai.com/device?user_code=ABCD-EFGH and enter the code."
        self.assertEqual(workers_login.extract_url(codex_text), "https://platform.openai.com/device?user_code=ABCD-EFGH")

    def test_detect_code_prompt(self) -> None:
        # agy SSH flow
        self.assertTrue(workers_login.detect_code_prompt("Enter verification code: "))
        self.assertTrue(workers_login.detect_code_prompt("To authenticate, visit:\nhttps://...\nEnter code: "))
        # claude
        self.assertTrue(workers_login.detect_code_prompt("Paste code here: "))
        self.assertTrue(workers_login.detect_code_prompt("Paste code here"))
        # cmd
        self.assertTrue(workers_login.detect_code_prompt("Enter authorization code? "))
        # Chinese
        self.assertTrue(workers_login.detect_code_prompt("请输入验证码："))
        # Negative cases
        self.assertFalse(workers_login.detect_code_prompt("Please wait, loading..."))
        self.assertFalse(workers_login.detect_code_prompt("Code generation finished."))
        self.assertFalse(workers_login.detect_code_prompt("https://antigravity.google/oauth?code=xyz"))

    def test_code_validation(self) -> None:
        # Valid codes
        self.assertEqual(workers_login.validate_code("123456"), "123456")
        self.assertEqual(workers_login.validate_code("  4/1AY0e-g7...  "), "4/1AY0e-g7...")
        self.assertEqual(workers_login.validate_code("A" * 200), "A" * 200)

        # Invalid codes
        with self.assertRaises(workers_login.BadCodeError):
            workers_login.validate_code("")
        with self.assertRaises(workers_login.BadCodeError):
            workers_login.validate_code("   ")
        with self.assertRaises(workers_login.BadCodeError):
            workers_login.validate_code("code\nwith\nnewlines")
        with self.assertRaises(workers_login.BadCodeError):
            workers_login.validate_code("code\rwith\rcarriage")
        with self.assertRaises(workers_login.BadCodeError):
            workers_login.validate_code("A" * 201)
        with self.assertRaises(workers_login.BadCodeError):
            workers_login.validate_code("code\x00null")

    def test_session_lifecycle_with_fake_pty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir))

            s_master, s_slave = socket.socketpair()
            master_fd = s_master.detach()
            slave_fd = s_slave.detach()
            test_pipe = os.dup(slave_fd)

            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.wait.return_value = 0

            with patch("pty.openpty", return_value=(master_fd, slave_fd)), \
                 patch("subprocess.Popen", return_value=mock_proc), \
                 patch("workers.detect_worker", return_value={"installed": True, "version": "1.0", "path": "/bin/agy", "auth": "ok"}):

                start_res = workers_login.start_login_session(settings, "agy")
                session_id = start_res["session"]
                self.assertEqual(start_res["state"], "waiting_url")

                # Step 1: CLI outputs URL -> open_url
                os.write(test_pipe, b"Visit https://antigravity.google/oauth?code=123 to authenticate\n")
                time.sleep(0.3)
                curr = workers_login.get_login_session(settings, "agy", session_id)
                self.assertEqual(curr["state"], "open_url")
                self.assertEqual(curr["url"], "https://antigravity.google/oauth?code=123")

                # Step 2: CLI asks for code -> needs_code
                os.write(test_pipe, b"Enter verification code: ")
                time.sleep(0.3)
                curr = workers_login.get_login_session(settings, "agy", session_id)
                self.assertEqual(curr["state"], "needs_code")

                # Step 3: Send code
                workers_login.send_login_code(settings, "agy", session_id, "AUTH_CODE_XYZ")
                received = os.read(test_pipe, 1024)
                self.assertEqual(received, b"AUTH_CODE_XYZ\n")

                # Step 4: Process finishes successfully
                mock_proc.poll.return_value = 0
                os.close(test_pipe)
                time.sleep(0.3)

                curr = workers_login.get_login_session(settings, "agy", session_id)
                self.assertEqual(curr["state"], "done")
                self.assertEqual(curr["message"], "登录成功")

    def test_session_lifecycle_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir))

            s_master, s_slave = socket.socketpair()
            master_fd = s_master.detach()
            slave_fd = s_slave.detach()
            test_pipe = os.dup(slave_fd)

            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.wait.return_value = 1

            with patch("pty.openpty", return_value=(master_fd, slave_fd)), \
                 patch("subprocess.Popen", return_value=mock_proc), \
                 patch("workers.detect_worker", return_value={"installed": True, "version": "1.0", "path": "/bin/agy", "auth": "needs_login"}):

                start_res = workers_login.start_login_session(settings, "agy")
                session_id = start_res["session"]

                # Process exits with error
                mock_proc.poll.return_value = 1
                os.close(test_pipe)
                time.sleep(0.3)

                curr = workers_login.get_login_session(settings, "agy", session_id)
                self.assertEqual(curr["state"], "failed")
                self.assertTrue(len(curr["message"]) > 0)

    def test_session_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir))

            s_master, s_slave = socket.socketpair()
            master_fd = s_master.detach()
            slave_fd = s_slave.detach()
            test_pipe = os.dup(slave_fd)

            mock_proc = MagicMock()
            mock_proc.poll.return_value = None

            with patch("pty.openpty", return_value=(master_fd, slave_fd)), \
                 patch("subprocess.Popen", return_value=mock_proc):

                start_res = workers_login.start_login_session(settings, "agy")
                session_id = start_res["session"]

                cancel_res = workers_login.cancel_login_session(settings, "agy", session_id)
                self.assertEqual(cancel_res["state"], "failed")
                self.assertEqual(cancel_res["message"], "已取消")
                mock_proc.terminate.assert_called()
                os.close(test_pipe)

    def test_session_new_start_kills_old(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir))

            s1_master, s1_slave = socket.socketpair()
            master1_fd = s1_master.detach()
            slave1_fd = s1_slave.detach()
            p1 = os.dup(slave1_fd)
            mock_proc1 = MagicMock()
            mock_proc1.poll.return_value = None

            with patch("pty.openpty", return_value=(master1_fd, slave1_fd)), \
                 patch("subprocess.Popen", return_value=mock_proc1):
                s1 = workers_login.start_login_session(settings, "agy")

            s2_master, s2_slave = socket.socketpair()
            master2_fd = s2_master.detach()
            slave2_fd = s2_slave.detach()
            p2 = os.dup(slave2_fd)
            mock_proc2 = MagicMock()
            mock_proc2.poll.return_value = None

            with patch("pty.openpty", return_value=(master2_fd, slave2_fd)), \
                 patch("subprocess.Popen", return_value=mock_proc2):
                s2 = workers_login.start_login_session(settings, "agy")

            self.assertNotEqual(s1["session"], s2["session"])
            mock_proc1.terminate.assert_called()
            os.close(p1)
            os.close(p2)

    def test_session_expiry_10min(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir))
            session = workers_login.WorkerLoginSession(
                worker_id="agy",
                session_id="test-exp",
                settings=settings,
                worker_def={"id": "agy"},
            )
            session.started_at = time.time() - 601  # >10 min ago
            self.assertTrue(session.is_expired())

            workers_login._active_sessions["agy"] = session
            curr = workers_login.get_login_session(settings, "agy", "test-exp")
            self.assertEqual(curr["state"], "failed")
            self.assertEqual(curr["message"], "登录超时")


class PooledRunnerTests(unittest.TestCase):
    def test_pooled_runner_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir))
            fake_detections = {
                "codex": {"installed": True, "version": "1.0", "path": "/bin/codex", "auth": "ok"},
                "claude-code": {"installed": True, "version": "2.0", "path": "/bin/claude", "auth": "ok"},
                "pi": {"installed": True, "version": "0.5", "path": "/bin/pi", "auth": "unknown"},
                "agy": {"installed": False, "version": None, "path": None, "auth": "unknown"},
                "cmd": {"installed": False, "version": None, "path": None, "auth": "unknown"},
            }

            backends = {
                "codex": FakeBackend("codex"),
                "claude-code": FakeBackend("claude-code"),
            }

            def fake_factory(wid, _s):
                return backends[wid]

            with patch("workers.detect_all_workers", return_value=fake_detections):
                runner = PooledRunner(settings, backend_factory=fake_factory)

                async def run():
                    async def progress(_msg): pass
                    job = await runner.start(JobMode.RUN, "do task", progress)
                    self.assertEqual(job.id, "job-codex")
                    self.assertIn("start:codex:do task", backends["codex"].calls)

                    # Delegation of cancel / diff / apply / discard
                    self.assertEqual(await runner.cancel(), "cancelled-codex")
                    self.assertEqual(await runner.diff_job(job.id), "diff:codex:job-codex")
                    self.assertEqual(await runner.apply_job(job.id), "apply:codex:job-codex")
                    self.assertEqual(await runner.discard_job(job.id), "discard:codex:job-codex")

                asyncio.run(run())

    def test_pooled_runner_retry_once_on_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir))
            fake_detections = {
                "codex": {"installed": True, "version": "1.0", "path": "/bin/codex", "auth": "ok"},
                "claude-code": {"installed": True, "version": "2.0", "path": "/bin/claude", "auth": "ok"},
                "pi": {"installed": True, "version": "0.5", "path": "/bin/pi", "auth": "unknown"},
                "agy": {"installed": False, "version": None, "path": None, "auth": "unknown"},
                "cmd": {"installed": False, "version": None, "path": None, "auth": "unknown"},
            }

            backends = {
                "codex": FakeBackend("codex", fail_error=RuntimeError("rate limit exceeded: Resets in 14h6m27s")),
                "claude-code": FakeBackend("claude-code"),
            }

            def fake_factory(wid, _s):
                return backends[wid]

            with patch("workers.detect_all_workers", return_value=fake_detections):
                runner = PooledRunner(settings, backend_factory=fake_factory)

                async def run():
                    async def progress(_msg): pass
                    job = await runner.start(JobMode.RUN, "test retry", progress)
                    # Succeeded on claude-code after codex failed with quota!
                    self.assertEqual(job.id, "job-claude-code")
                    self.assertIn("start:codex:test retry", backends["codex"].calls)
                    self.assertIn("start:claude-code:test retry", backends["claude-code"].calls)

                    # Codex was marked cooling in state
                    state = workers.load_workers_state(settings)
                    self.assertIn("codex", state["cooling"])

                asyncio.run(run())

    def test_pooled_runner_no_retry_on_non_quota_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir))
            fake_detections = {
                "codex": {"installed": True, "version": "1.0", "path": "/bin/codex", "auth": "ok"},
                "claude-code": {"installed": True, "version": "2.0", "path": "/bin/claude", "auth": "ok"},
                "pi": {"installed": False, "version": None, "path": None, "auth": "unknown"},
                "agy": {"installed": False, "version": None, "path": None, "auth": "unknown"},
                "cmd": {"installed": False, "version": None, "path": None, "auth": "unknown"},
            }

            backends = {
                "codex": FakeBackend("codex", fail_error=ValueError("Syntax error in project files")),
                "claude-code": FakeBackend("claude-code"),
            }

            def fake_factory(wid, _s):
                return backends[wid]

            with patch("workers.detect_all_workers", return_value=fake_detections):
                runner = PooledRunner(settings, backend_factory=fake_factory)

                async def run():
                    async def progress(_msg): pass
                    with self.assertRaises(ValueError) as ctx:
                        await runner.start(JobMode.RUN, "test non-quota", progress)
                    self.assertIn("Syntax error", str(ctx.exception))
                    # claude-code was NOT called
                    self.assertEqual(len(backends["claude-code"].calls), 0)
                    # codex was NOT marked cooling
                    state = workers.load_workers_state(settings)
                    self.assertNotIn("codex", state["cooling"])

                asyncio.run(run())

    def test_detection_runs_via_to_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir))
            runner = PooledRunner(settings)

            async def run():
                with patch("asyncio.to_thread", wraps=asyncio.to_thread) as mock_to_thread:
                    with patch("shutil.which", return_value=None):
                        workers.clear_detection_cache()
                        try:
                            await runner.validate()
                        except RuntimeError:
                            pass
                        mock_to_thread.assert_called()
                        targets = [c.args[0] for c in mock_to_thread.call_args_list]
                        self.assertIn(workers.detect_all_workers, targets)

            asyncio.run(run())

    def test_real_shape_quota_retry_on_next_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir))
            queue = JobQueue()
            queue.configure(settings, runner=None, recover=False)

            fake_detections = {
                "codex": {"installed": True, "version": "1.0", "path": "/bin/codex", "auth": "ok"},
                "claude-code": {"installed": True, "version": "2.0", "path": "/bin/claude", "auth": "ok"},
                "pi": {"installed": False, "version": None, "path": None, "auth": "unknown"},
                "agy": {"installed": False, "version": None, "path": None, "auth": "unknown"},
                "cmd": {"installed": False, "version": None, "path": None, "auth": "unknown"},
            }

            backends = {
                "codex": RealShapeFakeBackend("codex", error="RESOURCE_EXHAUSTED: quota exceeded. Resets in 2h"),
                "claude-code": RealShapeFakeBackend("claude-code", summary="Result from Claude Code"),
            }

            def fake_factory(wid, _s):
                return backends[wid]

            with patch("workers.detect_all_workers", return_value=fake_detections):
                runner = PooledRunner(settings, backend_factory=fake_factory)
                port = FakePort()
                msg = _make_msg("fix bug")

                async def run():
                    from handlers.jobs import _execute_codex_job
                    await _execute_codex_job(msg, port, runner, JobMode.RUN, "fix bug")

                asyncio.run(run())

                # Both backends were called once
                self.assertEqual(len(backends["codex"].calls), 1)
                self.assertEqual(len(backends["claude-code"].calls), 1)

                # Codex was marked cooling in state (reset_at ~ 7200s)
                state = workers.load_workers_state(settings)
                self.assertIn("codex", state["cooling"])
                self.assertGreater(state["cooling"]["codex"]["reset_at"], time.time() + 7000)

                # Exactly one retry notification was sent with Chinese text and label
                retry_messages = [t for t in port.sent if "当前执行者额度用完" in t]
                self.assertEqual(len(retry_messages), 1)
                self.assertIn("Claude Code", retry_messages[0])

                # Final result from Claude Code was delivered
                self.assertTrue(any("Result from Claude Code" in t for t in port.sent))

    def test_real_shape_non_quota_failure_no_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _make_settings(Path(tmpdir))
            queue = JobQueue()
            queue.configure(settings, runner=None, recover=False)

            fake_detections = {
                "codex": {"installed": True, "version": "1.0", "path": "/bin/codex", "auth": "ok"},
                "claude-code": {"installed": True, "version": "2.0", "path": "/bin/claude", "auth": "ok"},
                "pi": {"installed": False, "version": None, "path": None, "auth": "unknown"},
                "agy": {"installed": False, "version": None, "path": None, "auth": "unknown"},
                "cmd": {"installed": False, "version": None, "path": None, "auth": "unknown"},
            }

            backends = {
                "codex": RealShapeFakeBackend("codex", error="SyntaxError: invalid syntax on line 10"),
                "claude-code": RealShapeFakeBackend("claude-code", summary="Result"),
            }

            def fake_factory(wid, _s):
                return backends[wid]

            with patch("workers.detect_all_workers", return_value=fake_detections):
                runner = PooledRunner(settings, backend_factory=fake_factory)
                port = FakePort()
                msg = _make_msg("build app")

                async def run():
                    from handlers.jobs import _execute_codex_job
                    await _execute_codex_job(msg, port, runner, JobMode.RUN, "build app")

                asyncio.run(run())

                # Only codex was called; claude-code was NOT called
                self.assertEqual(len(backends["codex"].calls), 1)
                self.assertEqual(len(backends["claude-code"].calls), 0)

                # Codex was not marked cooling
                state = workers.load_workers_state(settings)
                self.assertNotIn("codex", state["cooling"])

                # No retry message
                self.assertFalse(any("当前执行者额度用完" in t for t in port.sent))

    def test_real_shape_pool_off_unchanged_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Feature flag off
            settings = _make_settings(Path(tmpdir), workers_enabled=False)
            queue = JobQueue()
            queue.configure(settings, runner=None, recover=False)

            backend = RealShapeFakeBackend("codex", error="RESOURCE_EXHAUSTED: quota exceeded. Resets in 2h")
            backend.settings = settings
            port = FakePort()
            msg = _make_msg("run command")

            async def run():
                from handlers.jobs import _execute_codex_job
                await _execute_codex_job(msg, port, backend, JobMode.RUN, "run command")

            asyncio.run(run())

            self.assertEqual(len(backend.calls), 1)
            # No retry notice
            self.assertFalse(any("当前执行者额度用完" in t for t in port.sent))
            # State file was not modified with cooling
            state = workers.load_workers_state(settings)
            self.assertNotIn("codex", state["cooling"])


TOKEN = "test-token-0123456789-abcdefghijklmnopqrstuvwxyz"


def _mock_api_detect_worker(worker_def: dict[str, Any]) -> dict[str, Any]:
    wid = worker_def["id"]
    return {
        "installed": True,
        "version": f"{wid} 1.0.0",
        "path": f"/usr/local/bin/{worker_def.get('bin', wid)}",
        "auth": "ok" if wid != "pi" else "unknown",
    }


class WebConsoleApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._detect_patcher = patch("workers.detect_worker", side_effect=_mock_api_detect_worker)
        cls._detect_patcher.start()
        workers.clear_detection_cache()

        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.settings = _make_settings(Path(cls.tmpdir.name), workers_enabled=True, workers_install_enabled=False)

        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()

        cls.queue = JobQueue()
        cls.queue.configure(cls.settings, runner=None, recover=False)
        control = WebControl(cls.settings, runner=None, queue=cls.queue)
        cls.server = WebConsoleServer(
            ("127.0.0.1", 0),
            WebConsoleHandler,
            control=control,
            loop=cls.loop,
            token=TOKEN,
        )
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.loop_thread.join(timeout=2)
        cls.server_thread.join(timeout=2)
        cls.tmpdir.cleanup()
        cls._detect_patcher.stop()
        workers.clear_detection_cache()

    def request(self, method: str, path: str, body: Any = None, authorized: bool = True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = f"Bearer {TOKEN}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        payload = json.loads(raw or b"{}")
        connection.close()
        return response.status, payload

    def test_feature_status_includes_workers(self) -> None:
        status, body = self.request("GET", "/api/system/status")
        self.assertEqual(status, 200)
        self.assertIn("workers", body.get("features", {}))
        self.assertTrue(body["features"]["workers"])

    def test_flag_off_returns_404(self) -> None:
        # Toggle flag off temporarily
        self.settings.workers_enabled = False
        try:
            status, body = self.request("GET", "/api/workers")
            self.assertEqual(status, 404)
            self.assertEqual(body.get("error"), "not found")

            status, body = self.request("POST", "/api/workers/order", {"order": [], "enabled": []})
            self.assertEqual(status, 404)
        finally:
            self.settings.workers_enabled = True

    def test_get_workers_and_detect(self) -> None:
        status, body = self.request("GET", "/api/workers")
        self.assertEqual(status, 200)
        self.assertTrue(body["enabled"])
        self.assertFalse(body["install_enabled"])
        self.assertIn("workers", body)
        self.assertEqual(len(body["workers"]), 5)

        status, body = self.request("POST", "/api/workers/detect")
        self.assertEqual(status, 200)
        self.assertIn("workers", body)

    def test_order_validation(self) -> None:
        # Unknown worker in order
        status, body = self.request("POST", "/api/workers/order", {"order": ["codex", "bad-worker"], "enabled": []})
        self.assertEqual(status, 400)
        self.assertEqual(body.get("code"), "unknown_worker")

        # Unknown worker in enabled
        status, body = self.request("POST", "/api/workers/order", {"order": ["codex"], "enabled": ["unknown-worker"]})
        self.assertEqual(status, 400)
        self.assertEqual(body.get("code"), "unknown_worker")

        # Valid order update
        new_order = ["claude-code", "codex", "pi", "agy", "cmd"]
        status, body = self.request("POST", "/api/workers/order", {"order": new_order, "enabled": ["claude-code", "codex"]})
        self.assertEqual(status, 200)
        worker_ids = [w["id"] for w in body["workers"]]
        self.assertEqual(worker_ids, new_order)
        self.assertTrue(body["workers"][0]["enabled"])

    def test_install_safety_and_validation(self) -> None:
        # 1. Install disabled -> 403
        self.settings.workers_install_enabled = False
        status, body = self.request("POST", "/api/workers/codex/install", {"confirm": True})
        self.assertEqual(status, 403)
        self.assertEqual(body.get("code"), "install_disabled")

        # Enable install for remaining tests
        self.settings.workers_install_enabled = True
        try:
            # 2. Confirm required -> 400
            status, body = self.request("POST", "/api/workers/codex/install", {"confirm": False})
            self.assertEqual(status, 400)
            self.assertEqual(body.get("code"), "confirm_required")

            status, body = self.request("POST", "/api/workers/codex/install", {})
            self.assertEqual(status, 400)
            self.assertEqual(body.get("code"), "confirm_required")

            # 3. Install unsupported (pi has install_command=None) -> 400
            status, body = self.request("POST", "/api/workers/pi/install", {"confirm": True})
            self.assertEqual(status, 400)
            self.assertEqual(body.get("code"), "install_unsupported")

            # 4. Successful install with mocked subprocess
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    ["npm", "install", "-g", "@openai/codex"],
                    0,
                    stdout="added 1 package in 1s\n",
                    stderr="",
                )
                status, body = self.request("POST", "/api/workers/codex/install", {"confirm": True})
                self.assertEqual(status, 200)
                self.assertTrue(body.get("ok"))
                self.assertIn("added 1 package", body.get("output", ""))
                # Verify fixed catalog command was executed (no shell=True)
                mock_run.assert_called_once()
                args, kwargs = mock_run.call_args
                self.assertEqual(args[0], ["npm", "install", "-g", "@openai/codex"])
                self.assertFalse(kwargs.get("shell", False))
                self.assertEqual(kwargs.get("timeout"), 300)
        finally:
            self.settings.workers_install_enabled = False

    def test_reset_quota_api(self) -> None:
        workers.mark_cooling(self.settings, "claude-code", reset_seconds=1800)
        status, body = self.request("GET", "/api/workers")
        self.assertEqual(status, 200)
        claude = next(w for w in body["workers"] if w["id"] == "claude-code")
        self.assertEqual(claude["quota"]["state"], "cooling")

        status, body = self.request("POST", "/api/workers/claude-code/reset-quota")
        self.assertEqual(status, 200)
        claude = next(w for w in body["workers"] if w["id"] == "claude-code")
        self.assertEqual(claude["quota"]["state"], "available")

    def test_install_agy_bash_c(self) -> None:
        self.settings.workers_install_enabled = True
        try:
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    ["bash", "-c", "curl -fsSL https://antigravity.google/cli/install.sh | bash"],
                    0,
                    stdout="Installed Antigravity CLI\n",
                    stderr="",
                )
                status, body = self.request("POST", "/api/workers/agy/install", {"confirm": True})
                self.assertEqual(status, 200)
                self.assertTrue(body.get("ok"))
                mock_run.assert_called_once()
                args, kwargs = mock_run.call_args
                self.assertEqual(args[0], ["bash", "-c", "curl -fsSL https://antigravity.google/cli/install.sh | bash"])
                self.assertFalse(kwargs.get("shell", False))
                self.assertEqual(kwargs.get("timeout"), 300)
        finally:
            self.settings.workers_install_enabled = False

    def test_models_api_flow(self) -> None:
        # 1. GET models for claude-code (fixed list)
        status, body = self.request("GET", "/api/workers/claude-code/models")
        self.assertEqual(status, 200)
        self.assertIn("models", body)
        self.assertIn("cached_at", body)
        ids = [m["id"] for m in body["models"]]
        self.assertEqual(ids, ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"])

        # 2. GET models for codex (unsupported) -> 400
        status, body = self.request("GET", "/api/workers/codex/models")
        self.assertEqual(status, 400)
        self.assertEqual(body.get("code"), "models_unsupported")

        # 3. GET models for unknown worker -> 400
        status, body = self.request("GET", "/api/workers/unknown-worker/models")
        self.assertEqual(status, 400)
        self.assertEqual(body.get("code"), "unknown_worker")

        # 4. POST choose model for claude-code
        status, body = self.request("POST", "/api/workers/claude-code/model", {"model": "claude-sonnet-5-5"})
        self.assertEqual(status, 200)
        claude = next(w for w in body["workers"] if w["id"] == "claude-code")
        self.assertEqual(claude["model"], "claude-sonnet-5-5")

        # 5. POST choose invalid model -> 400 unknown_model
        status, body = self.request("POST", "/api/workers/claude-code/model", {"model": "nonexistent-model"})
        self.assertEqual(status, 400)
        self.assertEqual(body.get("code"), "unknown_model")

        # 6. POST clear chosen model (set to null) -> 200
        status, body = self.request("POST", "/api/workers/claude-code/model", {"model": None})
        self.assertEqual(status, 200)
        claude = next(w for w in body["workers"] if w["id"] == "claude-code")
        self.assertIsNone(claude["model"])

        # 7. POST model for unsupported worker -> 400
        status, body = self.request("POST", "/api/workers/codex/model", {"model": "any"})
        self.assertEqual(status, 400)
        self.assertEqual(body.get("code"), "models_unsupported")

    def test_login_api_flow(self) -> None:
        s_master, s_slave = socket.socketpair()
        master_fd = s_master.detach()
        slave_fd = s_slave.detach()
        test_pipe = os.dup(slave_fd)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = 0

        with patch("pty.openpty", return_value=(master_fd, slave_fd)), \
             patch("subprocess.Popen", return_value=mock_proc):

            # 1. pi has login_command=None -> 400 login_unsupported
            status, body = self.request("POST", "/api/workers/pi/login/start")
            self.assertEqual(status, 400)
            self.assertEqual(body.get("code"), "login_unsupported")

            # 2. unknown worker -> 400 unknown_worker
            status, body = self.request("POST", "/api/workers/unknown-worker/login/start")
            self.assertEqual(status, 400)
            self.assertEqual(body.get("code"), "unknown_worker")

            # 3. Start login session for agy
            status, body = self.request("POST", "/api/workers/agy/login/start")
            self.assertEqual(status, 200)
            session_id = body.get("session")
            self.assertIsNotNone(session_id)
            self.assertEqual(body.get("state"), "waiting_url")

            # 4. GET status
            status, body = self.request("GET", f"/api/workers/agy/login/{session_id}")
            self.assertEqual(status, 200)
            self.assertEqual(body.get("session"), session_id)

            # 5. GET unknown session -> 404
            status, body = self.request("GET", "/api/workers/agy/login/nonexistent-session")
            self.assertEqual(status, 404)
            self.assertEqual(body.get("code"), "session_not_found")

            # 6. POST code: bad code (multiline) -> 400 bad_code
            status, body = self.request("POST", f"/api/workers/agy/login/{session_id}/code", {"code": "bad\ncode"})
            self.assertEqual(status, 400)
            self.assertEqual(body.get("code"), "bad_code")

            # 7. POST valid code -> 200
            status, body = self.request("POST", f"/api/workers/agy/login/{session_id}/code", {"code": "AUTH_VAL_99"})
            self.assertEqual(status, 200)
            received = os.read(test_pipe, 1024)
            self.assertEqual(received, b"AUTH_VAL_99\n")

            # 8. Cancel session -> 200, state failed, message "已取消"
            status, body = self.request("POST", f"/api/workers/agy/login/{session_id}/cancel")
            self.assertEqual(status, 200)
            self.assertEqual(body.get("state"), "failed")
            self.assertEqual(body.get("message"), "已取消")

            os.close(test_pipe)


    def test_provider_models_endpoint(self) -> None:
        # Create a mock upstream HTTP server
        class MockProviderHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                auth = self.headers.get("Authorization")
                if auth != "Bearer secret-token-xyz":
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error":"unauthorized"}')
                    return
                if self.path == "/models":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"data": [{"id": "custom-deepseek-chat"}, {"id": "custom-deepseek-coder"}]}).encode("utf-8"))
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), MockProviderHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        fake_port = server.server_address[1]

        try:
            # Write config.toml in memory root
            config_toml_path = Path(self.settings.codex_memory_root) / "config.toml"
            config_content = (
                f'model_provider = "testprov"\n'
                f'[model_providers.testprov]\n'
                f'name = "Test Provider"\n'
                f'base_url = "http://127.0.0.1:{fake_port}"\n'
                f'env_key = "TESTPROV_API_KEY"\n'
            )
            config_toml_path.write_text(config_content, encoding="utf-8")
            os.environ["TESTPROV_API_KEY"] = "secret-token-xyz"

            status, body = self.request("GET", "/api/config/provider/models")
            self.assertEqual(status, 200)
            self.assertIn("models", body)
            self.assertEqual(
                body["models"],
                [{"id": "custom-deepseek-chat"}, {"id": "custom-deepseek-coder"}],
            )
            # Verify the secret token is never returned
            self.assertNotIn("secret-token-xyz", json.dumps(body))

            # Test insecure remote URL rejection -> 502
            config_toml_path.write_text(
                'model_provider = "remote"\n[model_providers.remote]\nbase_url = "http://api.remote-insecure.com/v1"\nenv_key = "KEY"\n',
                encoding="utf-8",
            )
            status, body = self.request("GET", "/api/config/provider/models")
            self.assertEqual(status, 502)
            self.assertEqual(body.get("code"), "models_fetch_failed")

        finally:
            server.shutdown()
            server.server_close()
            os.environ.pop("TESTPROV_API_KEY", None)

    def test_flag_off_returns_404_all_routes(self) -> None:
        self.settings.workers_enabled = False
        try:
            routes = [
                ("GET", "/api/workers"),
                ("POST", "/api/workers/detect", {}),
                ("POST", "/api/workers/order", {"order": [], "enabled": []}),
                ("GET", "/api/workers/claude-code/models"),
                ("POST", "/api/workers/claude-code/model", {"model": None}),
                ("POST", "/api/workers/agy/login/start", {}),
                ("GET", "/api/workers/agy/login/session-123"),
                ("POST", "/api/workers/agy/login/session-123/code", {"code": "123"}),
                ("POST", "/api/workers/agy/login/session-123/cancel", {}),
            ]
            for method, path, *rest in routes:
                body = rest[0] if rest else None
                status, res = self.request(method, path, body)
                self.assertEqual(status, 404, f"Route {method} {path} should be 404 when flag off, got {status}")
                self.assertEqual(res.get("error"), "not found")
        finally:
            self.settings.workers_enabled = True


if __name__ == "__main__":
    unittest.main()
