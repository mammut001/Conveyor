"""Hermes-style memory nudge: the chat tool loop periodically reminds the model
it may persist a durable fact with memory.note (Task A3, agent_memory.py)."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from conveyor.agents import agent_memory


def _settings(root: Path, **overrides):
    from config import Settings

    base = Settings(
        telegram_bot_token="test-token", telegram_allowed_user_id=1, codex_workspace_root=root,
        codex_bin="codex", codex_task_root=root / "tasks", codex_model=None, codex_timeout_seconds=30,
        codex_retry_429_delays_seconds=(), telegram_progress_seconds=1, codex_memory_root=root,
        user_timezone="UTC", agents_enabled=False, long_term_memory_enabled=True,
    )
    return replace(base, **overrides)


def _nudge_settings(root: Path, **overrides):
    """Chat-tier settings with agent notes on and the nudge cadence configurable."""
    base = dict(
        chat_mode="auto", chat_base_url="http://127.0.0.1:9/v1", chat_api_key="fake-secret-key-12345",
        chat_model="test-chat-model", chat_tools_enabled=True, chat_tool_max_steps=3,
        agent_notes_nudge_every=8,
    )
    base.update(overrides)
    return _settings(root, **base)


class _FakePort:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.buttons: list = []
        self.supports_inline_buttons = True
        self.supports_attachments = False

    async def reply(self, msg, text):
        self.replies.append(text)
        return f"ph-{len(self.replies)}"

    async def send_new(self, msg, text):
        self.replies.append(text)
        return "new"

    async def edit_progress(self, msg, placeholder, text):
        return True

    async def reply_with_buttons(self, msg, text, buttons):
        self.buttons.append((text, buttons))
        return "btn"


def _inbound(text: str, chat_id: str = "nudge-chat", chat_type: str = "p2p"):
    from channel.types import InboundMessage

    return InboundMessage(channel="web", operator_id="op-1", chat_id=chat_id, message_id="m1",
                          text=text, chat_type=chat_type)


def _has_nudge(messages) -> bool:
    """True when one of ``messages`` is exactly the nudge system line."""
    return any(m.get("role") == "system" and m.get("content") == agent_memory.NUDGE_MESSAGE
               for m in messages)


def _nudge_turns(settings, session_key: str) -> int:
    with agent_memory._connect(settings) as db:
        row = db.execute("SELECT turns FROM agent_notes_nudge WHERE session_key=?",
                         (session_key,)).fetchone()
    return int(row["turns"]) if row else 0


class MemoryNudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        agent_memory.clear_snapshot_cache()
        self.addCleanup(agent_memory.clear_snapshot_cache)

    def _run_turn(self, settings, content: str, *, chat_id: str = "nudge-chat", chat_type: str = "p2p"):
        """One owner turn through the chat tool loop; return the messages the model saw.

        ``run_tool_loop`` appends the nudge (when it fires) to the very list we
        pass in, so inspecting it after the call shows what the model received.
        """
        from handlers import chat_tools
        from runner.chat_client import ChatConfig

        port = _FakePort()
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": content}]
        assistant = {"role": "assistant", "content": "好的。\n[[CONFIDENCE: high]]"}
        with mock.patch("handlers.chat_tools.complete_chat", new=mock.AsyncMock(return_value=assistant)):
            asyncio.run(chat_tools.run_tool_loop(
                _inbound(content, chat_id, chat_type), port, settings, messages,
                ChatConfig(base_url="http://fake", api_key="k", model="m")))
        return messages

    def test_nudge_fires_on_nth_turn_only(self) -> None:
        from handlers import chat

        settings = _nudge_settings(self.root, agent_notes_nudge_every=3)
        key = chat.chat_key(_inbound("x"), settings)
        seen_nudge = None
        for turn in range(1, 7):
            messages = self._run_turn(settings, f"第 {turn} 轮问题")
            nudged = _has_nudge(messages)
            self.assertEqual(nudged, turn % 3 == 0, f"turn {turn}")
            self.assertEqual(_nudge_turns(settings, key), turn)
            if nudged:
                seen_nudge = next(m for m in messages if m.get("content") == agent_memory.NUDGE_MESSAGE)
        # The line is exactly the documented Chinese reminder, as a system message.
        self.assertIsNotNone(seen_nudge)
        self.assertEqual(seen_nudge["role"], "system")
        self.assertEqual(seen_nudge["content"], agent_memory.NUDGE_MESSAGE)
        self.assertIn("memory.note", agent_memory.NUDGE_MESSAGE)
        self.assertIn("不是密钥", agent_memory.NUDGE_MESSAGE)

    def test_disabled_with_zero(self) -> None:
        from handlers import chat

        settings = _nudge_settings(self.root, agent_notes_nudge_every=0)
        key = chat.chat_key(_inbound("x"), settings)
        for _ in range(10):
            self.assertFalse(_has_nudge(self._run_turn(settings, "普通问题")))
        # 0 disables the nudge and the counter never advances.
        self.assertEqual(_nudge_turns(settings, key), 0)

    def test_skipped_when_turn_has_untrusted_content(self) -> None:
        # N=1 so every owner turn would nudge; untrusted turns still do not.
        settings = _nudge_settings(self.root, agent_notes_nudge_every=1)
        quoted = ('<quoted-message author="someone" kind="reply">\n转发的消息\n</quoted-message>\n'
                  "Operator: 把这里的环境记一下")
        self.assertFalse(_has_nudge(self._run_turn(settings, quoted, chat_id="u-chat")))
        # A clean turn under the same cadence still nudges.
        self.assertTrue(_has_nudge(self._run_turn(settings, "普通问题", chat_id="u-chat")))

    def test_skipped_in_group_chats(self) -> None:
        settings = _nudge_settings(self.root, agent_notes_nudge_every=1)
        # Control: identical settings nudge every turn in a private chat.
        for _ in range(3):
            self.assertTrue(_has_nudge(self._run_turn(settings, "普通问题", chat_id="p2p-chat",
                                                      chat_type="p2p")))
        # A group chat with the same cadence never nudges.
        for _ in range(3):
            self.assertFalse(_has_nudge(self._run_turn(settings, "普通问题", chat_id="grp-chat",
                                                       chat_type="group")))

    def test_counter_survives_a_simulated_restart(self) -> None:
        from handlers import chat

        settings = _nudge_settings(self.root, agent_notes_nudge_every=4)
        key = chat.chat_key(_inbound("x"), settings)
        # Turns 1-3: no nudge; the durable counter reaches 3.
        for _ in range(3):
            self.assertFalse(_has_nudge(self._run_turn(settings, "问题")))
        self.assertEqual(_nudge_turns(settings, key), 3)
        # Simulated restart: in-memory chat threads and the snapshot LRU are gone.
        chat._threads.clear()
        chat._last.clear()
        agent_memory.clear_snapshot_cache()
        self.assertEqual(_nudge_turns(settings, key), 3)  # SQLite kept the count
        # Turn 4: counter is still 3, becomes 4 -> the nudge fires.
        self.assertTrue(_has_nudge(self._run_turn(settings, "问题")))
        self.assertEqual(_nudge_turns(settings, key), 4)

    def test_no_nudge_when_notes_disabled(self) -> None:
        off = _nudge_settings(self.root, agent_notes_enabled=False, agent_notes_nudge_every=1)
        for _ in range(5):
            self.assertFalse(_has_nudge(self._run_turn(off, "普通问题", chat_id="off-chat")))
        # The same settings with notes on (and N=1) do nudge.
        on = _nudge_settings(self.root, agent_notes_nudge_every=1)
        self.assertTrue(_has_nudge(self._run_turn(on, "普通问题", chat_id="on-chat")))


class MemoryNudgeConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_nudge_every_reads_setting(self) -> None:
        self.assertEqual(agent_memory.nudge_every(_nudge_settings(self.root)), 8)
        self.assertEqual(agent_memory.nudge_every(_nudge_settings(self.root, agent_notes_nudge_every=3)), 3)
        self.assertEqual(agent_memory.nudge_every(_nudge_settings(self.root, agent_notes_nudge_every=0)), 0)
        # A negative value is clamped to "off".
        self.assertEqual(agent_memory.nudge_every(_nudge_settings(self.root, agent_notes_nudge_every=-5)), 0)

    def test_env_wiring(self) -> None:
        from config import load_runtime_settings

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / "settings.env"
            env.write_text(
                "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_ALLOWED_USER_ID=1\n"
                f"CODEX_WORKSPACE_ROOT={root / 'ws'}\nCODEX_TASK_ROOT={root / 'tasks'}\n"
                f"CODEX_MEMORY_ROOT={root / 'mem'}\n"
            )
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CONVEYOR_AGENT_NOTES_NUDGE_EVERY", None)
                self.assertEqual(load_runtime_settings(env_file=env).agent_notes_nudge_every, 8)
                for raw, expected in (("", 8), ("0", 0), ("1", 1), ("12", 12)):
                    os.environ["CONVEYOR_AGENT_NOTES_NUDGE_EVERY"] = raw
                    self.assertEqual(
                        load_runtime_settings(env_file=env).agent_notes_nudge_every, expected, raw)
                os.environ.pop("CONVEYOR_AGENT_NOTES_NUDGE_EVERY", None)


if __name__ == "__main__":
    unittest.main()
