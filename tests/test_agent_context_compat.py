"""Regression: strict AgentContext must preserve default-bot state and safety stop."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agents import AgentStore, chat_id_for
from channel.types import InboundMessage
from config import Settings
from handlers.chat import chat_key, history, remember
from handlers.dispatch import dispatch
from handlers.intent import COMPUTER_STOP_TEXTS
from handlers.session import append_turn, get_recent_turns, session_path


def _settings(root: Path) -> Settings:
    return Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_id=1,
        codex_workspace_root=root / "workspace",
        codex_bin="codex",
        codex_task_root=root / "tasks",
        codex_model=None,
        codex_timeout_seconds=30,
        codex_retry_429_delays_seconds=(),
        telegram_progress_seconds=1,
        codex_memory_root=root / "memory",
        user_timezone="UTC",
        agents_enabled=True,
        conveyor_computer_use_enabled=True,
    )


class Port:
    def __init__(self):
        self.replies = []

    async def reply(self, msg, text):
        self.replies.append(text)
        return "ok"


class DefaultAgentCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = _settings(Path(tmp.name))
        self.store = AgentStore(self.settings)
        flags = patch.dict(os.environ, {
            "CONVEYOR_NAMED_AGENTS": "1",
            "CONVEYOR_AGENT_CONTEXT": "1",
        })
        flags.start()
        self.addCleanup(flags.stop)

    def test_default_history_and_session_files_survive_enabling_flag(self):
        channels = (
            ("telegram", "12345", "1"),
            ("feishu", "oc_existing", "ou_existing"),
            ("web", "web-console", "web-console"),
        )
        for channel, chat_id, operator_id in channels:
            with self.subTest(channel=channel):
                msg = InboundMessage(channel=channel, chat_id=chat_id,
                                     operator_id=operator_id, message_id="1",
                                     text="existing question")
                with patch.dict(os.environ, {"CONVEYOR_AGENT_CONTEXT": "0"}):
                    old_key = chat_key(msg, self.settings)
                    old_path = session_path(self.settings, msg)
                    remember(old_key, f"old {channel} question",
                             f"old {channel} answer", 6, settings=self.settings)
                    append_turn(self.settings, msg, f"old {channel} job",
                                f"old {channel} result", mirror_transcript=False)
                self.assertEqual(old_key, f"{channel}:{chat_id}")
                self.assertTrue(old_path.is_file())
                # Strict mode must validate identity without changing default
                # paths, and must read the history and session already saved.
                self.assertEqual(chat_key(msg, self.settings), old_key)
                self.assertEqual(session_path(self.settings, msg), old_path)
                turns = history(chat_key(msg, self.settings), 6,
                                settings=self.settings)
                self.assertIn(f"old {channel} question",
                              [x["content"] for x in turns])
                records = get_recent_turns(self.settings, msg)
                self.assertEqual(records[-1]["assistant"],
                                 f"old {channel} result")

    def test_named_agent_still_receives_private_history_and_session(self):
        named = self.store.create({"name": "Worker", "tools_allowlist": []})
        msg = InboundMessage(channel="web", operator_id="web-console",
                             chat_id=chat_id_for(named["id"]), message_id="1",
                             text="work")
        self.assertTrue(chat_key(msg, self.settings).startswith(
            f"agent:{named['id']}:web:web-console:"))
        self.assertTrue(session_path(self.settings, msg).name.startswith(
            f"{named['id']}_"))

    async def test_archived_agent_can_issue_safety_stop_without_tool_access(self):
        named = self.store.create({"name": "Stopped", "tools_allowlist": []})
        chat_id = chat_id_for(named["id"])
        self.store.archive(named["id"])
        stop_text = next(iter(COMPUTER_STOP_TEXTS))
        msg = InboundMessage(channel="web", operator_id="web-console",
                             chat_id=chat_id, message_id="1", text=stop_text)
        port = Port()
        with (patch("handlers.tools.executors.exec_computer_stop",
                    new_callable=AsyncMock, return_value="stopped") as cancel,
              patch("handlers.tools.runner.run_tool", side_effect=AssertionError(
                  "emergency stop must bypass tool execution"))):
            await dispatch(msg, port, self.settings, object())
            cancel.assert_awaited_once_with(self.settings, "")
        self.assertEqual(port.replies, ["stopped"])
        # Any other command remains fail-closed after archive.
        await dispatch(replace(msg, text="run something"), port,
                       self.settings, object())
        self.assertEqual(port.replies[-1], "agent identity denied")

    async def test_safety_stop_still_requires_channel_authorization(self):
        msg = InboundMessage(channel="telegram", operator_id="not-owner",
                             chat_id="12345", message_id="1",
                             text=next(iter(COMPUTER_STOP_TEXTS)))
        port = Port()
        with patch("handlers.tools.executors.exec_computer_stop",
                   new_callable=AsyncMock) as cancel:
            await dispatch(msg, port, self.settings, object())
            cancel.assert_not_awaited()
        self.assertEqual(port.replies, ["Unauthorized."])


if __name__ == "__main__":
    unittest.main()
