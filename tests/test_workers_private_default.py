"""Private Telegram and Feishu chats default to the canonical Web main session.

The runner is never started. Bot globals are restored when a test touches them.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from agents import AgentError, AgentStore, session_id_for
from channel.types import InboundMessage
from handlers.agent_selection import handle_agent_command
from handlers.dispatch import dispatch
from handlers.job_queue import JobQueue, reset_job_queue
from handlers.workers import (
    _sessions_for,
    bind_execution,
    context_line_for_session,
    handle_workers_command,
    handle_workers_token,
    resolve_effective,
)
from tests.test_agents import _settings
from transcript_store import get_transcript_store, session_identity
from worker_sessions import WorkerSessionStore


class Port:
    supports_inline_buttons = True
    supports_attachments = False
    wait_for_job = False

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []
        self.buttons: list = []

    async def reply(self, msg, text):
        self.messages.append((msg.channel, msg.chat_id, text))
        return "1"

    send_new = reply

    async def edit_progress(self, msg, placeholder, text):
        return True

    async def reply_with_buttons(self, msg, text, buttons):
        self.buttons.append(buttons)
        return await self.reply(msg, text)

    async def send_card(self, msg, card, reply_to=None):
        return await self.reply(msg, str(card))


def _load_bot(settings, queue):
    if "bot" not in sys.modules:
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_ALLOWED_USER_ID": "1",
            "CODEX_WORKSPACE_ROOT": str(settings.codex_workspace_root),
        }):
            with patch("config.load_settings", return_value=settings), \
                    patch("runner.CodexRunner", return_value=SimpleNamespace()), \
                    patch("handlers.job_queue.get_job_queue", return_value=queue):
                import bot  # noqa: F401
        import handlers.job_queue as job_queue
        sys.modules["bot"].get_job_queue = job_queue.get_job_queue
    return sys.modules["bot"]


class PrivateDefaultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = _settings(self.root)
        self.agents = AgentStore(self.settings)
        self.alpha = self.agents.create({"name": "Alpha", "instructions": "A"})
        self.queue = JobQueue()
        self.queue.configure(self.settings, runner=None, recover=False)
        patcher = patch("handlers.job_queue.get_job_queue", return_value=self.queue)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(reset_job_queue)
        self.port = Port()
        self.store = WorkerSessionStore(self.settings)

    def message(self, text="hello", *, channel="telegram", chat="42", chat_type="p2p", operator="1"):
        return InboundMessage(channel, operator, chat, "9", text, chat_type=chat_type)

    def _job_count(self) -> int:
        path = self.root / "state" / "job_queue.sqlite3"
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute("SELECT COUNT(*) FROM queued_jobs").fetchone()
        except sqlite3.OperationalError:
            return 0
        finally:
            conn.close()
        return int(row[0])

    async def test_fresh_private_telegram_and_feishu_use_canonical_and_physical_reply(self) -> None:
        runner = SimpleNamespace(status_text=lambda: "IDLE")
        for channel, chat in (("telegram", "42"), ("feishu", "oc_room")):
            self.port.messages.clear()
            msg = self.message("/status", channel=channel, chat=chat)
            await dispatch(msg, self.port, self.settings, runner)
            selected = self.store.selected(channel, chat, "1")
            self.assertEqual(selected["session_id"], session_id_for("default"))
            self.assertEqual(selected["source_chat_id"], "agent-default")
            delivered = self.port.messages[-1]
            self.assertEqual(delivered[0], channel)
            self.assertEqual(delivered[1], chat)
            self.assertIn("当前：Conveyor › 主会话", delivered[2])
            self.assertIn("此 Agent 对话还没有任务", delivered[2])
            self.assertNotIn("/status", delivered[2])
            self.assertNotIn("IDLE", delivered[2])
        rewritten, wrapped = bind_execution(self.message("hello"), self.port, self.settings)
        self.assertEqual(rewritten.text, "hello")
        self.assertEqual(rewritten.channel, "web")
        self.assertEqual(rewritten.chat_id, "agent-default")
        await wrapped.reply(rewritten, "answer")
        self.assertEqual(self.port.messages[-1][0], "telegram")
        self.assertEqual(self.port.messages[-1][1], "42")
        self.assertEqual(self.port.messages[-1][2], "answer")
        self.assertFalse(get_transcript_store(self.settings).list_sessions(20))
        web = InboundMessage("web", "web-console", "agent-default", "1", "ship")
        await self.queue.enqueue("run", "ship the widget", web, self.port, SimpleNamespace(settings=self.settings))
        other = InboundMessage("web", "web-console", "agent-other", "1", "elsewhere")
        await self.queue.enqueue("run", "other project only", other, self.port, SimpleNamespace(settings=self.settings))
        self.port.messages.clear()
        await dispatch(self.message("/status"), self.port, self.settings, runner)
        body = self.port.messages[-1][2]
        self.assertEqual(self.port.messages[-1][0], "telegram")
        self.assertIn("当前：Conveyor › 主会话", body)
        self.assertIn("ship the widget", body)
        self.assertNotIn("other project only", body)
        self.assertFalse(any("当前：" in (item.get("content") or "") for item in (
            (get_transcript_store(self.settings).get_session(session_id_for("default")) or {}).get("messages") or []
        )))
        plain = Port()
        from handlers.conversation_jobs import handle_conversation_command
        await handle_conversation_command(
            "jobs", web, plain, runner, self.settings, "",
        )
        self.assertNotIn("当前：", plain.messages[-1][2])
        self.assertIn("ship the widget", plain.messages[-1][2])

    async def test_cleared_private_selection_blocks_old_secondary_confirm(self) -> None:
        side = self.store.create(self.alpha["id"], title="Side A")
        self.store.select("telegram", "42", "1", side["session_id"])
        token = self.store.issue_token(
            operator_id="1", channel="telegram", topic="42", agent_id=self.alpha["id"],
            session_id=side["session_id"], action="confirm",
            extra={"native": "tool:confirm:abc"}, bound_chat_type="p2p",
        )
        self.store.clear("telegram", "42", "1")
        execute = AsyncMock()
        with patch("handlers.tools.runner.execute_confirmed", execute):
            await handle_workers_token(self.message(""), self.port, self.settings, None, token)
        execute.assert_not_awaited()
        self.assertIn("当前已切换到其他会话", self.port.messages[-1][2])
        self.assertIsNone(self.store.selected("telegram", "42", "1"))
        group = self.message("", chat="-100", chat_type="group")
        group_token = self.store.issue_token(
            operator_id="1", channel="telegram", topic="-100", agent_id=self.alpha["id"],
            session_id=side["session_id"], action="confirm",
            extra={"native": "tool:confirm:group"}, bound_chat_type="group",
        )
        execute.reset_mock()
        with patch("handlers.tools.runner.execute_confirmed", execute):
            await handle_workers_token(group, self.port, self.settings, None, group_token)
        execute.assert_awaited()
        self.assertIsNone(self.store.selected("telegram", "-100", "1"))

    async def test_secondary_is_retained(self) -> None:
        side = self.store.create(self.alpha["id"], title="Side A")
        self.store.select("telegram", "42", "1", side["session_id"])
        rewritten, _port = bind_execution(self.message("hello"), self.port, self.settings)
        self.assertEqual(rewritten.text, "hello")
        self.assertEqual(rewritten.chat_id, side["source_chat_id"])
        self.assertEqual(self.store.selected("telegram", "42", "1")["session_id"], side["session_id"])

    async def test_groups_topics_and_disabled_agents_stay_unbound(self) -> None:
        group = self.message("hello", chat="-100", chat_type="group")
        rewritten, _port = bind_execution(group, self.port, self.settings)
        self.assertEqual(rewritten.chat_id, "-100")
        self.assertEqual(rewritten.text, "hello")
        self.assertIsNone(self.store.selected("telegram", "-100", "1"))
        topic = self.message("hello", chat="42:topic:3", chat_type="p2p")
        bind_execution(topic, self.port, self.settings)
        self.assertIsNone(self.store.selected("telegram", "42:topic:3", "1"))
        off = _settings(self.root, agents_enabled=False)
        bind_execution(self.message("hello"), self.port, off)
        self.assertIsNone(self.store.selected("telegram", "42", "1"))

    async def test_explicit_legacy_binding_is_kept_and_archived_fails_closed(self) -> None:
        self.agents.bind_chat("telegram", "42", self.alpha["id"])
        rewritten, _port = bind_execution(self.message("hello"), self.port, self.settings)
        self.assertEqual(rewritten.channel, "telegram")
        self.assertEqual(rewritten.text, "hello")
        self.assertIsNone(self.store.selected("telegram", "42", "1"))
        target = resolve_effective(self.settings, self.message("/status"), persist=True)
        self.assertEqual(target.mode, "legacy")
        self.assertEqual(target.legacy_agent["name"], "Alpha")
        self.assertIsNone(self.store.selected("telegram", "42", "1"))
        from handlers.workers import current_context_line
        self.assertEqual(
            current_context_line(self.settings, self.message("x"), persist=False),
            "当前：Alpha › Telegram 独立会话",
        )
        self.assertTrue(self.agents.archive(self.alpha["id"]))
        with self.assertRaises(AgentError) as caught:
            bind_execution(self.message("next"), self.port, self.settings)
        self.assertIn("已归档", str(caught.exception))
        self.assertIsNone(self.store.selected("telegram", "42", "1"))

    async def test_archived_canonical_primary_fails_closed(self) -> None:
        session_id = session_id_for("default")
        conn = sqlite3.connect(str(self.root / "state" / "job_queue.sqlite3"))
        conn.execute(
            """INSERT INTO worker_sessions
               (session_id, agent_id, channel, operator_id, source_chat_id, title, kind, archived, created_at)
               VALUES (?, 'default', 'web', 'web-console', 'agent-default', 'Conveyor', 'main', 1, 0)""",
            (session_id,),
        )
        conn.commit()
        conn.close()
        with self.assertRaises(AgentError) as caught:
            bind_execution(self.message("hello"), self.port, self.settings)
        self.assertIn("默认主会话不可用", str(caught.exception))
        self.assertIsNone(self.store.selected("telegram", "42", "1"))
        feishu = self.message("hello", channel="feishu", chat="oc_room")
        with self.assertRaises(AgentError):
            bind_execution(feishu, self.port, self.settings)
        self.assertIsNone(self.store.selected("feishu", "oc_room", "1"))

    async def test_private_reset_returns_to_main_without_deleting_history(self) -> None:
        side = self.store.create(self.alpha["id"], title="Side A")
        self.store.select("telegram", "42", "1", side["session_id"])
        transcripts = get_transcript_store(self.settings)
        transcripts.append(
            session_id=side["session_id"], role="user", content="web side",
            channel="web", operator_id="web-console", source_chat_id=side["source_chat_id"],
        )
        legacy_id = session_identity("telegram", "42", "1")
        transcripts.append(
            session_id=legacy_id, role="user", content="old telegram",
            channel="telegram", operator_id="1", source_chat_id="42",
        )
        msg = self.message("/workers exit")
        await handle_workers_command(msg, self.port, None, self.settings, "exit")
        self.assertEqual(self.store.selected("telegram", "42", "1")["session_id"], session_id_for("default"))
        self.assertIn("已返回「Conveyor」主会话", self.port.messages[-1][2])
        self.assertEqual(transcripts.get_session(side["session_id"])["messages"][0]["content"], "web side")
        self.assertEqual(transcripts.get_session(legacy_id)["messages"][0]["content"], "old telegram")
        self.assertFalse(self.store.get(side["session_id"])["archived"])
        group = self.message("/workers exit", chat="-100", chat_type="group")
        self.store.select("telegram", "-100", "1", side["session_id"])
        await handle_workers_command(group, self.port, None, self.settings, "exit")
        self.assertIsNone(self.store.selected("telegram", "-100", "1"))
        self.assertIn("回到原来的对话", self.port.messages[-1][2])

    async def test_duplicate_titles_use_session_suffix(self) -> None:
        first = self.store.create("default", title="Conveyor")
        second = self.store.create("default", title="Conveyor")
        label = context_line_for_session(self.settings, second)
        self.assertEqual(label, f"当前：Conveyor › Conveyor · {second['session_id'][-6:]}")
        self.assertNotEqual(first["session_id"][-6:], second["session_id"][-6:])
        from handlers.workers import _render_switch
        msg = self.message("/workers")
        await _render_switch(msg, self.port, self.settings, second, 0, 0)
        flat = " ".join(
            button["text"] for grid in self.port.buttons for row in grid for button in row
        )
        self.assertIn(second["session_id"][-6:], flat)
        self.assertIn("当前：", self.port.messages[-1][2])

    async def test_start_shows_current_secondary_without_replacing_it(self) -> None:
        bot = _load_bot(self.settings, self.queue)
        prior = (bot.settings, bot.runner, bot.get_job_queue)
        bot.settings = self.settings
        bot.runner = SimpleNamespace()
        side = self.store.create(self.alpha["id"], title="Side A")
        self.store.select("telegram", "42", "1", side["session_id"])
        (self.root / "operator.json").write_text("{}", encoding="utf-8")
        message = SimpleNamespace(
            text="/start", caption=None, photo=None, document=None, message_id=9,
            message_thread_id=None, is_topic_message=False, entities=(),
            caption_entities=(), reply_to_message=None, quote=None, external_reply=None,
            reply_text=AsyncMock(return_value=SimpleNamespace(message_id=11)),
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1, username="op"),
            effective_chat=SimpleNamespace(id=42, type="private"),
            effective_message=message,
            callback_query=None,
        )
        try:
            await bot.start_cmd(update, MagicMock())
        finally:
            bot.settings, bot.runner, bot.get_job_queue = prior
        body = "\n".join(call.args[0] for call in message.reply_text.await_args_list)
        self.assertIn("当前：Alpha › Side A", body)
        self.assertEqual(self.store.selected("telegram", "42", "1")["session_id"], side["session_id"])

    async def test_navigation_does_not_write_transcript_jobs_or_rebind(self) -> None:
        side = self.store.create(self.alpha["id"], title="Side A")
        self.store.select("telegram", "42", "1", side["session_id"])
        other = next(row for row in self.store.list("default") if row["kind"] == "main")
        before = get_transcript_store(self.settings).list_sessions(50)
        msg = self.message("")
        token = self.store.issue_token(
            operator_id="1", channel="telegram", topic="42", agent_id="default",
            session_id=other["session_id"], action="open",
        )
        await handle_workers_token(msg, self.port, self.settings, None, token)
        self.assertIn("正在查看", self.port.messages[-1][2])
        self.assertIn("尚未选中", self.port.messages[-1][2])
        tasks = self.store.issue_token(
            operator_id="1", channel="telegram", topic="42", agent_id="default",
            session_id=other["session_id"], action="tasks",
        )
        await handle_workers_token(msg, self.port, self.settings, None, tasks)
        self.assertEqual(self.store.selected("telegram", "42", "1")["session_id"], side["session_id"])
        self.assertEqual(get_transcript_store(self.settings).list_sessions(50), before)
        self.assertEqual(self._job_count(), 0)

    async def test_agent_command_is_not_replaced_by_the_default_on_the_next_message(self) -> None:
        msg = self.message("/agent")
        await handle_agent_command(msg, self.port, None, self.settings, self.alpha["id"])
        self.assertIsNone(self.store.selected("telegram", "42", "1"))
        rewritten, _port = bind_execution(self.message("hello"), self.port, self.settings)
        self.assertEqual(rewritten.channel, "telegram")
        self.assertEqual(rewritten.text, "hello")
        self.assertIsNone(self.store.selected("telegram", "42", "1"))

    async def test_feishu_group_token_is_not_treated_as_private(self) -> None:
        side = self.store.create(self.alpha["id"], title="Side A")
        self.store.select("feishu", "oc_group", "1", side["session_id"])
        buttons = SimpleNamespace(messages=[], buttons=[], supports_inline_buttons=True)

        async def reply(_msg, text):
            buttons.messages.append(text)
            return "1"

        async def reply_with_buttons(_msg, text, rows):
            buttons.buttons.append(rows)
            return await reply(_msg, text)

        buttons.reply = reply
        buttons.reply_with_buttons = reply_with_buttons
        group = self.message("/workers", channel="feishu", chat="oc_group", chat_type="group")
        await handle_workers_command(group, buttons, None, self.settings, "")
        self.assertEqual(self.store.selected("feishu", "oc_group", "1")["session_id"], side["session_id"])
        exit_token = next(
            button["callback_data"][3:]
            for grid in buttons.buttons
            for row in grid
            for button in row
            if button["text"] == "退出 Workers"
        )
        clicked = replace(group, text="", chat_type="p2p")
        await handle_workers_token(clicked, buttons, self.settings, None, exit_token)
        self.assertIsNone(self.store.selected("feishu", "oc_group", "1"))
        self.assertIn("回到原来的对话", buttons.messages[-1])
        self.assertNotIn("主会话", buttons.messages[-1])

        bare = self.store.issue_token(
            operator_id="1", channel="feishu", topic="oc_group", agent_id=self.alpha["id"],
            session_id=side["session_id"], action="exit",
        )
        self.store.select("feishu", "oc_group", "1", side["session_id"])
        await handle_workers_token(clicked, buttons, self.settings, None, bare)
        self.assertIsNone(self.store.selected("feishu", "oc_group", "1"))

        quiet = SimpleNamespace(messages=[], buttons=[], supports_inline_buttons=True)

        async def quiet_reply(_msg, text):
            quiet.messages.append(text)
            return "1"

        async def quiet_buttons(_msg, text, rows):
            quiet.buttons.append(rows)
            return await quiet_reply(_msg, text)

        quiet.reply = quiet_reply
        quiet.reply_with_buttons = quiet_buttons
        await handle_workers_command(
            self.message("/workers", channel="feishu", chat="oc_other", chat_type="group"),
            quiet, None, self.settings, "",
        )
        self.assertIsNone(self.store.selected("feishu", "oc_other", "1"))
        listed = next(
            button["callback_data"][3:]
            for grid in quiet.buttons
            for row in grid
            for button in row
            if button["callback_data"].startswith("wk:")
        )
        await handle_workers_token(
            self.message("", channel="feishu", chat="oc_other", chat_type="p2p"),
            quiet, self.settings, None, listed,
        )
        self.assertIsNone(self.store.selected("feishu", "oc_other", "1"))
        fresh = self.message("hello", channel="feishu", chat="oc_private", chat_type="p2p")
        rewritten, _port = bind_execution(fresh, self.port, self.settings)
        self.assertEqual(rewritten.chat_id, "agent-default")
        self.assertEqual(rewritten.text, "hello")
        self.assertEqual(
            self.store.selected("feishu", "oc_private", "1")["session_id"],
            session_id_for("default"),
        )

    async def test_tasks_keep_viewed_session_when_empty(self) -> None:
        side = self.store.create(self.alpha["id"], title="Side A")
        self.store.select("telegram", "42", "1", side["session_id"])
        other = self.store.create(self.alpha["id"], title="主会话")
        token = self.store.issue_token(
            operator_id="1", channel="telegram", topic="42", agent_id=self.alpha["id"],
            session_id=other["session_id"], action="tasks", bound_chat_type="p2p",
        )
        await handle_workers_token(self.message(""), self.port, self.settings, None, token)
        body = self.port.messages[-1][2]
        self.assertIn("Side A", body)
        self.assertIn("尚未选中", body)
        self.assertIn("主会话 的任务", body)
        self.assertIn("此会话还没有任务。", body)
        self.assertNotIn("这是当前选中的会话", body)
        detail = self.store.issue_token(
            operator_id="1", channel="telegram", topic="42", agent_id=self.alpha["id"],
            session_id=other["session_id"], action="open", bound_chat_type="p2p",
        )
        await handle_workers_token(self.message(""), self.port, self.settings, None, detail)
        shown = self.port.messages[-1][2]
        self.assertIn("尚未选中", shown)
        self.assertNotIn("这是当前选中的会话", shown)
        self.assertEqual(self.store.selected("telegram", "42", "1")["session_id"], side["session_id"])

    async def test_archived_primary_transcript_fails_closed(self) -> None:
        transcripts = get_transcript_store(self.settings)
        session_id = session_id_for("default")
        transcripts.ensure_session(
            session_id, channel="web", operator_id="web-console", source_chat_id="agent-default",
        )
        self.assertTrue(transcripts.archive_session(session_id))
        with self.assertRaises(AgentError) as caught:
            bind_execution(self.message("hello"), self.port, self.settings)
        self.assertIn("默认主会话不可用", str(caught.exception))
        self.assertIsNone(self.store.selected("telegram", "42", "1"))
        self.assertTrue(transcripts.get_session(session_id)["archived"])
        self.store.select("telegram", "42", "1", session_id)
        with self.assertRaises(AgentError):
            resolve_effective(self.settings, self.message("again"), persist=True)
        self.assertTrue(transcripts.get_session(session_id)["archived"])
        self.assertFalse(transcripts.get_session(session_id)["messages"])

    async def test_unbound_raw_telegram_history_is_selectable_for_this_chat_only(self) -> None:
        transcripts = get_transcript_store(self.settings)
        raw = session_identity("telegram", "42", "1")
        transcripts.append(
            session_id=raw, role="user", content="raw mine",
            channel="telegram", operator_id="1", source_chat_id="42",
        )
        transcripts.append(
            session_id=session_identity("telegram", "42", "8"), role="user", content="other person",
            channel="telegram", operator_id="8", source_chat_id="42",
        )
        transcripts.append(
            session_id=session_identity("telegram", "99", "1"), role="user", content="other chat",
            channel="telegram", operator_id="1", source_chat_id="99",
        )
        msg = self.message("/workers")
        found = _sessions_for(self.settings, msg, "default")
        legacy = [row for row in found if row["kind"] == "legacy" and row["source_chat_id"] == "42"]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0]["operator_id"], "1")
        self.assertFalse(any(row["operator_id"] == "8" or row["source_chat_id"] == "99" for row in found))
        self.store.select("telegram", "42", "1", legacy[0]["session_id"])
        rewritten, _port = bind_execution(self.message("hello"), self.port, self.settings)
        self.assertEqual((rewritten.channel, rewritten.chat_id, rewritten.text), ("telegram", "42", "hello"))
        self.assertEqual(transcripts.get_session(raw)["messages"][0]["content"], "raw mine")
        self.assertIsNone(get_transcript_store(self.settings).get_session(session_id_for("default")))
        self.assertIsNone(self.store.selected("telegram", "42", "8"))
        self.assertIsNone(self.store.selected("telegram", "99", "1"))
        self.agents.bind_chat("telegram", "7", self.alpha["id"])
        bound_msg = self.message("/workers", chat="7")
        transcripts.append(
            session_id=session_identity("telegram", "7", "1"), role="user", content="before bind",
            channel="telegram", operator_id="1", source_chat_id="7",
        )
        bound_rows = _sessions_for(self.settings, bound_msg, "default")
        self.assertFalse(any(
            row["kind"] == "legacy" and row["source_chat_id"] == "7" for row in bound_rows
        ))


if __name__ == "__main__":
    unittest.main()
