from __future__ import annotations

import asyncio
import os
import queue
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from channel.types import InboundMessage
from config import Settings
from handlers.chat import is_destructive_request, routes_to_agent
from runner.pi_agent import PiBackend, normalize_pi_model
from runner.providers import create_agent_backend, create_runtime_runner
from runner.types import Job, JobMode, JobState
from web_chat import WebChatPort, run_web_chat


def _make_settings(tmp: Path) -> Settings:
    root = (tmp / "workspace").resolve()
    root.mkdir(parents=True, exist_ok=True)
    state_root = tmp / "state"
    (state_root / "tasks").mkdir(parents=True, exist_ok=True)
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


def _drain_queue(q: queue.Queue) -> list[tuple[str, dict]]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


class PiBackendTests(unittest.TestCase):
    def test_normalize_pi_model(self) -> None:
        self.assertEqual(normalize_pi_model("deepseek", "deepseek-flash"), "deepseek-v4-flash")
        self.assertEqual(normalize_pi_model("deepseek", "deepseek-chat"), "deepseek-v4-flash")
        self.assertEqual(normalize_pi_model("deepseek", "deepseek-reasoner"), "deepseek-v4-pro")
        self.assertEqual(normalize_pi_model("openai", "gpt-4o"), "gpt-4o")

    def test_provider_factory_selects_pi(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            settings = _make_settings(Path(raw))
            backend = create_agent_backend(settings, provider="pi")
            runner = create_runtime_runner(settings, provider="pi")
            self.assertEqual(backend.name, "pi")
            self.assertIsInstance(runner, PiBackend)
            self.assertEqual(runner.pi_model, "deepseek-v4-flash")

    def test_destructive_detection_and_routing(self) -> None:
        self.assertTrue(routes_to_agent("你可以帮我下载一个这个吗"))
        self.assertFalse(is_destructive_request("你可以帮我下载一个这个吗"))
        self.assertFalse(is_destructive_request("帮我查一下 GitHub 有没有登录"))
        self.assertTrue(is_destructive_request("帮我把 /tmp/test.txt 删除掉"))
        self.assertTrue(is_destructive_request("please rm -rf /tmp/build"))
        self.assertTrue(is_destructive_request("delete the old backup file"))

    def test_run_codex_attempt_parses_events_and_passes_env(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            backend = PiBackend(settings)
            fake_pi = tmp / "fake-pi"
            fake_pi.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "prompt = sys.stdin.read()\n"
                "assert os.environ.get(\"DEEPSEEK_API_KEY\") == \"sk-test-deepseek\"\n"
                "assert os.environ.get(\"PI_OFFLINE\") == \"1\"\n"
                "print(json.dumps({\"type\": \"tool_execution_start\", \"toolName\": \"bash\", \"args\": {\"command\": \"gh auth status\"}}), flush=True)\n"
                "print(json.dumps({\"type\": \"message_update\", \"assistantMessageEvent\": {\"type\": \"text_delta\", \"delta\": \"GitHub 已登录: \"}}), flush=True)\n"
                "print(json.dumps({\"type\": \"message_end\", \"message\": {\"role\": \"assistant\", \"content\": [{\"type\": \"text\", \"text\": \"GitHub 已登录: mammut001\"}]}}), flush=True)\n"
                "print(json.dumps({\"type\": \"agent_end\", \"messages\": [{\"role\": \"assistant\", \"content\": [{\"type\": \"text\", \"text\": \"GitHub 已登录: mammut001\"}]}]}), flush=True)\n",
                encoding="utf-8",
            )
            fake_pi.chmod(0o755)
            backend.pi_bin = str(fake_pi)

            job = Job(
                "job-test-1",
                JobMode.RUN,
                "帮我查一下 GitHub 有没有登录",
                "unused",
                state=JobState.RUNNING,
                worktree_path=settings.codex_workspace_root,
                log_path=settings.codex_task_root / "job.log",
                final_message_path=settings.codex_task_root / "final.txt",
            )
            updates: list[str] = []

            async def on_progress(text: str) -> None:
                updates.append(text)

            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test-deepseek"}):
                asyncio.run(backend._run_codex_attempt(job, on_progress))
            self.assertEqual(job.return_code, 0, job.error)
            self.assertEqual(job.final_message_path.read_text(encoding="utf-8"), "GitHub 已登录: mammut001")
            self.assertTrue(any("🔧 bash" in u for u in updates))

    def test_web_chat_auto_executes_non_destructive_and_gates_destructive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            runner = PiBackend(settings)
            executed_prompts: list[str] = []

            async def fake_handle_codex_job(msg, port, _runner, *, mode, prompt, wait=False):
                executed_prompts.append(prompt)
                await port.reply(msg, f"已自动执行: {prompt}")

            q1: queue.Queue = queue.Queue()
            msg1 = InboundMessage(
                channel="web",
                operator_id="web-console",
                chat_id="web-auto",
                message_id="m1",
                text="你可以帮我下载 https://example.com/file.txt 吗",
            )
            port1 = WebChatPort(q1, settings, "web:web-console:web-auto", prompt=msg1.text)
            with patch("handlers.jobs.handle_codex_job", new=fake_handle_codex_job):
                asyncio.run(run_web_chat(msg1, port1, settings, runner, msg1.text))
            events_non_delete = _drain_queue(q1)
            self.assertEqual(len(executed_prompts), 1)
            self.assertTrue(any(e == "message" and "已自动执行" in d.get("text", "") for e, d in events_non_delete))
            self.assertFalse(any(e == "approval" for e, _ in events_non_delete))

            q2: queue.Queue = queue.Queue()
            msg2 = InboundMessage(
                channel="web",
                operator_id="web-console",
                chat_id="web-del",
                message_id="m2",
                text="帮我删除 /tmp/file.txt",
            )
            port2 = WebChatPort(q2, settings, "web:web-console:web-del", prompt=msg2.text)
            with patch("handlers.jobs.handle_codex_job", new=fake_handle_codex_job):
                asyncio.run(run_web_chat(msg2, port2, settings, runner, msg2.text))
            events_delete = _drain_queue(q2)
            # Must NOT have executed automatically without confirmation
            self.assertEqual(len(executed_prompts), 1)
            approvals = [d for e, d in events_delete if e == "approval"]
            self.assertEqual(len(approvals), 1)
            self.assertEqual(approvals[0]["tool_name"], "pi.destructive_task")


    def test_run_mode_uses_direct_workspace_without_worktree_churn(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            backend = PiBackend(settings)
            job = Job("run-direct", JobMode.RUN, "check", "unused")
            wt = asyncio.run(backend._create_worktree(job))
            self.assertEqual(wt, settings.codex_workspace_root.resolve())
            self.assertFalse(job.worktree_created)


if __name__ == "__main__":
    unittest.main()
