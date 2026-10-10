"""Telegram navigation: legacy buttons, private scoped cards, and safe callbacks."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telegram_navigation import (
    callback_action, legacy_action, navigation_screen, sessions_screen, workers_screen,
)
from transcript_store import get_transcript_store, session_identity


class FakeQueue:
    def __init__(self, jobs=None):
        self.jobs = list(jobs or [])
        self.calls = []

    def list_jobs(self, limit=100, *, session_id=None, channel=None, operator_id=None):
        self.calls.append((limit, session_id, channel, operator_id))
        return [
            item for item in self.jobs
            if (session_id is None or item["chat_id"] == session_id)
            and (channel is None or item["channel"] == channel)
            and (operator_id is None or item["operator_id"] == operator_id)
        ][:limit]


def _job(job_id, chat, operator="42", *, channel="telegram", state="completed"):
    return {
        "id": job_id, "chat_id": chat, "operator_id": operator,
        "channel": channel, "state": state, "mode": "fix",
        "prompt_preview": f"task {job_id}",
    }


class TelegramNavigationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = SimpleNamespace(
            codex_memory_root=self.root, agents_enabled=False,
        )

    def test_exact_legacy_keyboard_labels_are_not_sent_to_llm(self):
        self.assertEqual(legacy_action("👷 我的 Workers"), "workers")
        self.assertEqual(legacy_action("👷 我的 Workers\ufe0f"), "workers")
        self.assertEqual(legacy_action("🔄 切换会话"), "sessions")
        self.assertEqual(legacy_action("切换会话"), "sessions")
        for normal_prompt in (
            "请创建 3 个 Workers", "切换会话以后修复代码",
            "/workers", "我想知道 Workers 怎么工作", "切换会话？",
        ):
            self.assertIsNone(legacy_action(normal_prompt))

    def test_workers_read_only_card_restricts_jobs_to_physical_chat_and_operator(self):
        queue = FakeQueue([
            _job("mine-running", "100", state="running"),
            _job("mine-queued", "100", state="queued"),
            _job("different-user", "100", operator="43", state="running"),
            _job("other-chat", "200", state="running"),
            _job("different-channel", "100", channel="web", state="running"),
        ])
        screen = workers_screen(self.settings, queue, "42", "100")
        self.assertIn("运行中 1", screen.text)
        self.assertIn("排队中 1", screen.text)
        self.assertIn("默认 Worker", screen.text)
        self.assertNotIn("different-user", screen.text)
        self.assertTrue(all(call[1:] == ("100", "telegram", "42") for call in queue.calls))
        self.assertIn(("🔄 查看会话和任务", "tgn:sessions"), screen.buttons[-1])

    def test_workers_counts_are_explicitly_scoped_to_recent_jobs(self):
        # Older running jobs must not be misrepresented as global zero.
        recent = [_job(f"old-{i}", "100") for i in range(40)]
        older_running = _job("older-running", "100", state="running")
        screen = workers_screen(self.settings, FakeQueue(recent + [older_running]), "42", "100")
        self.assertIn("最近 40 条任务中", screen.text)
        self.assertIn("运行中 0", screen.text)

    def test_sessions_list_and_job_detail_revalidate_identity(self):
        queue = FakeQueue([
            _job("mine-1", "100", state="completed"),
            _job("not-mine", "100", operator="43"),
            _job("other-channel", "100", channel="feishu"),
        ])
        sess_id = session_identity("telegram", "100", "42")
        get_transcript_store(self.settings).append_turn(
            sess_id, "hello", "hi", channel="telegram",
            operator_id="42", source_chat_id="100",
        )
        screen = sessions_screen(self.settings, queue, "42", "100")
        self.assertIn("当前会话", screen.text)
        self.assertIn("最近任务：1 条", screen.text)
        self.assertIn("不会改变任务执行会话", screen.text)
        self.assertNotIn("not-mine", screen.text)
        token = screen.buttons[0][0][1]
        self.assertTrue(token.startswith("tgn:job:"))
        selected = navigation_screen(token.removeprefix("tgn:"), self.settings, queue, "42", "100")
        self.assertIn("mine-1", selected.text)
        self.assertIsNone(
            navigation_screen(token.removeprefix("tgn:"), self.settings, queue, "43", "100")
        )
        self.assertIsNone(
            navigation_screen(token.removeprefix("tgn:"), self.settings, queue, "42", "200")
        )

    def test_agents_card_is_scoped_and_never_leaks_paths_or_instructions(self):
        import agents
        self.settings.agents_enabled = True
        store = agents.AgentStore(self.settings)
        created = store.create({
            "name": "Research Worker", "workspace_path": "/private/project",
            "instructions": "DO_NOT_LEAK_SECRET_CONTEXT",
        })
        screen = workers_screen(self.settings, FakeQueue(), "42", "100")
        self.assertIn("Research Worker", screen.text)
        self.assertNotIn("/private/project", screen.text)
        detail = navigation_screen(
            f"agent:{created['id']}", self.settings, FakeQueue(), "42", "100",
        )
        self.assertIn("Web Agent", detail.text)
        self.assertNotIn("/private/project", detail.text)
        self.assertNotIn("DO_NOT_LEAK", detail.text)
        self.assertIsNone(navigation_screen("agent:notfound", self.settings, FakeQueue(), "42", "100"))

    def test_callback_namespace_is_strict_and_other_callbacks_untouched(self):
        self.assertEqual(callback_action("tgn:workers"), "workers")
        self.assertEqual(callback_action("tgn:sessions"), "sessions")
        self.assertEqual(callback_action("tgn:job:" + "a" * 16), "job:" + "a" * 16)
        for invalid in (
            "tool:confirm:token", "relay:approve:token", "ob:start", "deep",
            "tgn:job:../../other", "tgn:agent:../private", "tgn:agent:Upper",
            "tgn:delete", "tgn:sessions:garbage", "tgn:" + "x" * 80,
        ):
            self.assertIsNone(callback_action(invalid))


if __name__ == "__main__":
    unittest.main()
