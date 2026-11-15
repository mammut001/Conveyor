"""tests/test_worker_check.py — 执行者一键测试（「这个执行者现在能回答吗？」）。

Covers `classify_check_error`, `check_worker` against a fake `pi` executable
(the same approach as tests/test_pi_backend.py), the `POST /api/workers/<id>/check`
route, and the goal-start preflight that reads the saved result.
"""
from __future__ import annotations

import asyncio
import dataclasses
import http.client
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from conveyor.agents import agent_supervisor as ags
from conveyor.agents import supervisor_goals as goals
from conveyor.agents import supervisor_service as svc
from conveyor.agents import worker_check
from conveyor.agents import workers
from conveyor.core.config import Settings
from conveyor.entrypoints.web_console import WebConsoleHandler, WebConsoleServer
from conveyor.web.web_control import WebControl
from handlers.job_queue import JobQueue

TOKEN = "test-token-0123456789-abcdefghijklmnopqrstuvwxyz"


def _clean_pi_env(fake: Path) -> dict[str, str]:
    """Minimal environment with the Pi bin pointed at the fake executable."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(fake.parent),
        "LANG": "C.UTF-8",
        "CONVEYOR_PI_BIN": str(fake),
    }


def _write_fake_pi(tmp: Path, name: str, events: list) -> Path:
    """Fake pi CLI: a dict item is printed as one JSONL event, a float is a sleep."""
    lines = [
        "#!/usr/bin/env python3",
        "import json, os, sys, time",
        "if '--version' in sys.argv:",
        "    print('pi 1.5.0 (fake)')",
        "    raise SystemExit(0)",
        "sys.stdin.read()",
        "def emit(obj):",
        "    print(json.dumps(obj), flush=True)",
    ]
    for item in events:
        if isinstance(item, dict):
            lines.append(f"emit({item!r})")
        else:
            lines.append(f"time.sleep({item!r})")
    fake = tmp / name
    fake.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fake.chmod(0o755)
    return fake


def _write_sleeping_pi(tmp: Path, name: str, pid_file: Path, seconds: float = 30.0) -> Path:
    """Fake pi CLI that records its pid and then never answers."""
    fake = tmp / name
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time\n"
        "sys.stdin.read()\n"
        f"with open({str(pid_file)!r}, 'w', encoding='utf-8') as fh:\n"
        "    fh.write(str(os.getpid()))\n"
        f"time.sleep({seconds!r})\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _assistant_event(text: str) -> dict[str, Any]:
    return {
        "type": "agent_end",
        "messages": [{"role": "assistant", "content": [{"type": "text", "text": text}]}],
    }


def _make_settings(root: Path, *, workers_enabled: bool = True) -> Any:
    task_root = root / "tasks"
    ws_root = root / "ws"
    mem_root = root / "mem"
    for directory in (task_root, ws_root, mem_root):
        directory.mkdir(parents=True, exist_ok=True)
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
        workers_install_enabled=True,
    )
    return SimpleNamespace(**dataclasses.asdict(base))


class ClassifyCheckErrorTests(unittest.TestCase):
    def test_auth_failures(self) -> None:
        for text in (
            '401: {"message":"Authentication Fails"}',
            "403 Forbidden",
            "Invalid API key provided",
            "unauthorized",
        ):
            self.assertEqual(worker_check.classify_check_error(text), "auth", text)

    def test_quota_failures(self) -> None:
        for text in ("402 Payment Required", "insufficient balance", "余额不足，请充值"):
            self.assertEqual(worker_check.classify_check_error(text), "quota", text)

    def test_model_missing(self) -> None:
        for text in (
            "404 model not found",
            'The model "deepseek-v4-flash" does not exist',
        ):
            self.assertEqual(worker_check.classify_check_error(text), "model_missing", text)

    def test_rate_limit_and_timeout_and_network(self) -> None:
        self.assertEqual(worker_check.classify_check_error("429 Too Many Requests"), "rate_limit")
        self.assertEqual(worker_check.classify_check_error("rate limit exceeded"), "rate_limit")
        self.assertEqual(worker_check.classify_check_error("request timed out"), "timeout")
        self.assertEqual(worker_check.classify_check_error("connection refused"), "network")
        self.assertEqual(worker_check.classify_check_error("getaddrinfo ENOTFOUND api.example.com"), "network")

    def test_unknown_text_is_other(self) -> None:
        self.assertEqual(worker_check.classify_check_error(""), "other")
        self.assertEqual(worker_check.classify_check_error("something odd happened"), "other")


class CheckWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        workers.clear_detection_cache()
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-worker-check-")
        self.root = Path(self._tmp.name).resolve()
        self.settings = _make_settings(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        workers.clear_detection_cache()

    def test_unsupported_worker_runs_nothing(self) -> None:
        result = asyncio.run(worker_check.check_worker(self.settings, "codex"))
        self.assertIsNone(result["ok"])
        self.assertEqual(result["worker_id"], "codex")
        self.assertEqual(result["kind"], "unsupported")
        self.assertEqual(result["message"], "这个执行者暂不支持一键测试")
        self.assertIn("checked_at", result)
        # Nothing was persisted for an unsupported worker.
        state = workers.load_workers_state(self.settings)
        self.assertEqual(state.get("last_check"), {})

    def test_success_saves_result_and_shows_in_payload(self) -> None:
        fake = _write_fake_pi(self.root, "pi", [_assistant_event("好的")])
        with patch.dict(os.environ, _clean_pi_env(fake), clear=True):
            result = asyncio.run(worker_check.check_worker(self.settings, "pi"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["kind"], "ok")
        self.assertEqual(result["worker_id"], "pi")
        self.assertEqual(result["provider"], "deepseek")
        self.assertTrue(result["model"])
        self.assertGreaterEqual(result["latency_ms"], 0)
        self.assertIn("可以用", result["message"])
        self.assertIn("毫秒", result["message"])
        self.assertNotIn("api_key", result["detail"].lower())

        saved = workers.load_workers_state(self.settings)["last_check"]["pi"]
        self.assertEqual(saved["ok"], True)
        self.assertEqual(saved["kind"], "ok")

        payload = workers.get_workers_payload(self.settings)
        pi_entry = next(w for w in payload["workers"] if w["id"] == "pi")
        self.assertEqual(pi_entry["last_check"]["kind"], "ok")

    def test_auth_error_event_is_reported_in_chinese(self) -> None:
        fake = _write_fake_pi(
            self.root,
            "pi",
            [{"type": "error", "message": '401: {"message":"Authentication Fails"}'}],
        )
        with patch.dict(os.environ, _clean_pi_env(fake), clear=True):
            result = asyncio.run(worker_check.check_worker(self.settings, "pi"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "auth")
        self.assertIn("设置 › 模型设置", result["message"])
        self.assertNotIn("DEEPSEEK_API_KEY", result["message"])
        self.assertIn("401", result["detail"])
        self.assertLessEqual(len(result["detail"]), 600)

    def test_timeout_kills_the_process(self) -> None:
        pid_file = self.root / "pi.pid"
        fake = _write_sleeping_pi(self.root, "pi", pid_file, seconds=30.0)
        with patch.dict(os.environ, _clean_pi_env(fake), clear=True):
            result = asyncio.run(worker_check.check_worker(self.settings, "pi", timeout=1.0))
        self.assertFalse(result["ok"])
        self.assertEqual(result["kind"], "timeout")
        self.assertIn("1 秒内没有回应", result["message"])
        self.assertLess(result["latency_ms"], 8000)
        self.assertTrue(pid_file.is_file(), "the fake pi must have started")
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_clear_worker_checks_forgets_the_saved_pi_result(self) -> None:
        fake = _write_fake_pi(self.root, "pi", [_assistant_event("好的")])
        with patch.dict(os.environ, _clean_pi_env(fake), clear=True):
            asyncio.run(worker_check.check_worker(self.settings, "pi"))
        workers.clear_worker_checks(self.settings)
        payload = workers.get_workers_payload(self.settings)
        pi_entry = next(w for w in payload["workers"] if w["id"] == "pi")
        self.assertIsNone(pi_entry["last_check"])


def _mock_api_detect_worker(worker_def: dict[str, Any]) -> dict[str, Any]:
    wid = worker_def["id"]
    return {
        "installed": True,
        "version": f"{wid} 1.0.0",
        "path": f"/usr/local/bin/{worker_def.get('bin', wid)}",
        "auth": "ok",
    }


class WorkerCheckRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._detect_patcher = patch(
            "conveyor.agents.workers.detect_worker", side_effect=_mock_api_detect_worker
        )
        cls._detect_patcher.start()
        workers.clear_detection_cache()

        cls._usage_patcher = patch("conveyor.agents.worker_usage.fetch_usage", return_value=None)
        cls._usage_patcher.start()

        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.settings = _make_settings(Path(cls.tmpdir.name))

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
        cls._usage_patcher.stop()
        cls._detect_patcher.stop()
        workers.clear_detection_cache()

    def request(self, method: str, path: str, body: Any = None, authorized: bool = True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
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

    def test_check_pi_returns_the_result_and_the_workers_payload(self) -> None:
        fake = _write_fake_pi(Path(self.tmpdir.name), "pi", [_assistant_event("好的")])
        with patch.dict(os.environ, _clean_pi_env(fake), clear=True):
            status, body = self.request("POST", "/api/workers/pi/check")
        self.assertEqual(status, 200)
        self.assertTrue(body["check"]["ok"], body)
        self.assertEqual(body["check"]["kind"], "ok")
        self.assertIn("workers", body)
        pi_entry = next(w for w in body["workers"] if w["id"] == "pi")
        self.assertEqual(pi_entry["last_check"]["kind"], "ok")

    def test_check_unknown_worker_is_rejected(self) -> None:
        status, body = self.request("POST", "/api/workers/not-a-worker/check")
        self.assertEqual(status, 400)
        self.assertEqual(body.get("code"), "unknown_worker")


# ---------------------------------------------------------------------------
# Goal preflight: a saved failed Pi check refuses to start a goal
# ---------------------------------------------------------------------------

GOAL_OUTCOME = "在托管工作目录里新增 feature.txt，内容为 hello-goal，并干净提交"


def _init_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Worker Check Test"], check=True)
    (repo_dir / "README.md").write_text("# Disposable Goal Repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "initial commit"], check=True, capture_output=True)


def _goal_settings(root: Path) -> Settings:
    workspace = root / "workspace"
    task_root = root / "tasks"
    mem_root = root / "memory"
    for directory in (workspace, task_root, mem_root):
        directory.mkdir(parents=True, exist_ok=True)
    return Settings(
        telegram_bot_token="",
        telegram_allowed_user_id=1,
        codex_workspace_root=workspace,
        codex_bin="/bin/echo",
        codex_task_root=task_root,
        codex_model=None,
        codex_timeout_seconds=30,
        telegram_progress_seconds=5,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=mem_root,
        user_timezone="UTC",
        agent_supervisor_enabled=True,
        agent_supervisor_max_repair_rounds=1,
        agent_supervisor_task_timeout_seconds=25,
        agent_supervisor_validation_commands=(),
    )


class GoalPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-goal-check-")
        self.root = Path(self._tmp.name).resolve()
        self.settings = _goal_settings(self.root)
        _init_repo(self.root / "repo")
        repo, _created = svc.register_repository(self.settings, path=str(self.root / "repo"))
        wt = svc.create_managed_worktree(self.settings, repo["id"], branch="feat/goal-check")
        self.repo_id = str(repo["id"])
        self.worktree_id = str(wt["id"])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _body(self, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "outcome": GOAL_OUTCOME,
            "repo_id": self.repo_id,
            "worktree_id": self.worktree_id,
        }
        body.update(overrides)
        return body

    def _save_check(self, result: dict[str, Any]) -> None:
        state = workers.load_workers_state(self.settings)
        state.setdefault("last_check", {})["pi"] = result
        workers.save_workers_state(self.settings, state)

    def test_failed_check_refuses_to_start_a_pi_goal(self) -> None:
        self._save_check({
            "ok": False,
            "worker_id": "pi",
            "kind": "auth",
            "message": "模型服务拒绝了密钥（deepseek）。请到 设置 › 模型设置 重新填写 API Key。",
        })
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            goals.create_goal(None, self.settings, self._body(
                role_overrides={"implementer": {"executor": "pi"}}
            ))
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "executor_check_failed")
        self.assertIn("执行者上次测试没通过", ctx.exception.message)
        self.assertIn("设置 › 模型设置", ctx.exception.message)

    def test_ok_check_allows_start(self) -> None:
        self._save_check({"ok": True, "worker_id": "pi", "kind": "ok", "message": "可以用"})
        goal = goals.create_goal(
            None, self.settings, self._body(role_overrides={"implementer": {"executor": "pi"}})
        )
        self.assertEqual(goal["state"], "running")

    def test_no_saved_check_allows_start(self) -> None:
        goal = goals.create_goal(
            None, self.settings, self._body(role_overrides={"implementer": {"executor": "pi"}})
        )
        self.assertEqual(goal["state"], "running")

    def test_failed_check_ignored_when_no_role_uses_pi(self) -> None:
        self._save_check({"ok": False, "worker_id": "pi", "kind": "auth", "message": "模型服务拒绝了密钥"})

        class _FakeExecutor:
            name = "fake"

            def available(self):
                return True, "ok"

        with patch.object(ags, "get_executor", lambda _settings, _name: _FakeExecutor()):
            goal = goals.create_goal(
                None,
                self.settings,
                self._body(
                    role_overrides={
                        "analyst": {"executor": "claude"},
                        "implementer": {"executor": "claude"},
                        "reviewer": {"executor": "claude"},
                    }
                ),
            )
        self.assertEqual(goal["state"], "running")


if __name__ == "__main__":
    unittest.main()
