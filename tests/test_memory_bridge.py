"""tests/test_memory_bridge.py — tests for Flash chat and Codex memory bridge."""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from channel.types import InboundMessage, OutboundPort
from config import load_settings
from handlers import chat, chat_memory, session
from handlers.session import append_turn, build_context_prompt, get_recent_turns
from runner.types import Job, JobMode, JobState
from transcript_store import TranscriptStore, get_transcript_store, session_identity


class MemoryBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        base = load_settings()
        self.settings = dataclasses.replace(
            base,
            codex_memory_root=self.root / "mem",
            codex_task_root=self.root / "tasks",
            codex_workspace_root=self.root / "repo",
            chat_history_turns=6,
            conveyor_session_enabled=True,
            conveyor_session_max_turns=20,
            conveyor_session_inject_turns=5,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _msg(self, channel: str = "web", chat_id: str = "test-chat", operator_id: str = "user-1") -> InboundMessage:
        return InboundMessage(
            channel=channel,
            chat_id=chat_id,
            operator_id=operator_id,
            message_id="msg-1",
            text="hello",
        )

    def test_chat_turn_persists_to_session_with_kind_tag(self) -> None:
        msg = self._msg()
        # Direct append of a chat turn
        append_turn(self.settings, msg, "我想讨论一下暗黑模式", "建议使用 #121212 背景色", kind="chat")

        turns = get_recent_turns(self.settings, msg)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["kind"], "chat")
        self.assertEqual(turns[0]["user"], "我想讨论一下暗黑模式")
        self.assertEqual(turns[0]["assistant"], "建议使用 #121212 背景色")

        # Verify build_context_prompt tags the chat turn
        prompt = build_context_prompt(self.settings, msg)
        self.assertIn("User [chat]: 我想讨论一下暗黑模式", prompt)
        self.assertIn("Assistant [chat]: 建议使用 #121212 背景色", prompt)
        self.assertIn('<recent-chat-context guard="not-instruction" source="session">', prompt)

    def test_codex_turn_remains_untagged_in_context_prompt(self) -> None:
        msg = self._msg()
        append_turn(self.settings, msg, "把背景色改成黑色", "已完成修改", kind="codex")

        prompt = build_context_prompt(self.settings, msg)
        self.assertIn("User: 把背景色改成黑色", prompt)
        self.assertIn("Assistant: 已完成修改", prompt)
        self.assertNotIn("User [codex]:", prompt)

    def test_codex_job_completion_bridges_to_chat_memory(self) -> None:
        msg = self._msg(channel="telegram", chat_id="tg-123", operator_id="op-1")
        key = chat.chat_key(msg)

        # Simulate job execution completion in handlers/jobs.py
        user_text = "帮我修改 Button.tsx 的样式"
        final_answer = "已在 worktree 完成修改：将按钮背景修改为蓝色，增加了点击动画。"

        # Call append_turn and bridge to chat.remember
        append_turn(self.settings, msg, user_text, final_answer)

        chat.remember(
            key,
            f"[执行任务] {user_text}",
            f"[Codex 任务 job-456 完成]\n{final_answer}",
            self.settings.chat_history_turns,
            settings=self.settings,
        )

        # Flash chat should now see this in history
        history = chat.history(key, 10, settings=self.settings)
        self.assertTrue(len(history) >= 2)
        self.assertEqual(history[-2]["role"], "user")
        self.assertIn("[执行任务] 帮我修改 Button.tsx 的样式", history[-2]["content"])
        self.assertEqual(history[-1]["role"], "assistant")
        self.assertIn("[Codex 任务 job-456 完成]", history[-1]["content"])
        self.assertIn("将按钮背景修改为蓝色", history[-1]["content"])

    def test_handles_transcript_directly_prevents_duplicate_web_transcript(self) -> None:
        msg = self._msg(channel="web", chat_id="web-session-1", operator_id="web-console")
        store = get_transcript_store(self.settings)
        session_id = session_identity(msg.channel, msg.chat_id, msg.operator_id)

        # 1. Simulate WebOutbound recording the turn directly
        store.append_turn(
            session_id,
            "用户输入问题",
            "Flash 模型的回答",
            channel=msg.channel,
            operator_id=msg.operator_id,
            source_chat_id=msg.chat_id,
            kind="chat",
        )

        # 2. handlers/chat.py calls append_turn with mirror_transcript=False because port handles it directly
        append_turn(
            self.settings,
            msg,
            "用户输入问题",
            "Flash 模型的回答",
            kind="chat",
            mirror_transcript=False,
        )

        # Check transcript store session messages
        session_data = store.get_session(session_id)
        self.assertIsNotNone(session_data)
        messages = session_data["messages"]
        # Exactly 1 user message and 1 assistant message, no duplicate!
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[1]["role"], "assistant")

    def test_system_prompt_reflects_worktree_info(self) -> None:
        prompt = chat.system_prompt(
            self.settings,
            has_evidence=False,
            can_search=False,
            worktree_info="Job job-999: applied cleanly, modified 2 files.",
        )
        self.assertIn("Latest host job status: Job job-999: applied cleanly, modified 2 files.", prompt)
        self.assertIn("You are Conveyor's chat layer", prompt)


if __name__ == "__main__":
    unittest.main()
