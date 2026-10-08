"""Workers navigation, physical delivery, and callback scope.

Handlers and the queue run for real. The Codex runner is never started.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents import AgentStore
from channel.feishu_cards import parse_action
from channel.types import InboundMessage
from handlers.dispatch import dispatch
from handlers.job_queue import JobQueue, reset_job_queue
from handlers.workers import PhysicalOrigin, PhysicalOriginPort, handle_workers_command, handle_workers_token, workers_card
from tests.test_agents import _settings
from transcript_store import get_transcript_store
from worker_sessions import WorkerSessionStore


class Port:
    supports_inline_buttons = True
    supports_attachments = False
    wait_for_job = False

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.buttons: list[list[list[dict]]] = []
        self.cards: list[dict] = []

    async def reply(self, msg, text):
        self.messages.append((msg.chat_id, text))
        return "1"

    send_new = reply

    async def edit_progress(self, msg, placeholder, text):
        return True

    async def reply_with_buttons(self, msg, text, buttons):
        self.buttons.append(buttons)
        return await self.reply(msg, text)

    async def send_card(self, msg, card, reply_to=None):
        self.cards.append(card)
        return "card"


def _tokens(port: Port) -> list[tuple[str, str]]:
    found = []
    for grid in port.buttons:
        for row in grid:
            for button in row:
                data = str(button.get("callback_data") or "")
                if data.startswith("wk:"):
                    found.append((button["text"], data[3:]))
    return found


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = _settings(self.root)
        self.agents = AgentStore(self.settings)
        self.alpha = self.agents.create({"name": "Alpha", "instructions": "A"})
        self.beta = self.agents.create({"name": "Beta", "instructions": "B"})
        for index in range(5):
            self.agents.create({"name": f"Extra{index}"})
        self.queue = JobQueue()
        self.queue.configure(self.settings, runner=None, recover=False)
        patcher = patch("handlers.job_queue.get_job_queue", return_value=self.queue)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(reset_job_queue)
        self.port = Port()

    def message(self, text="/workers", operator="7", chat="-100:topic:3"):
        return InboundMessage("telegram", operator, chat, "9", text, chat_type="group", mentioned_bot=True)

    async def test_every_worker_is_reachable_and_switch_is_per_agent(self) -> None:
        msg = self.message()
        await handle_workers_command(msg, self.port, None, self.settings, "")
        seen = "\n".join(text for _chat, text in self.port.messages)
        labels = [text for text, _token in _tokens(self.port)]
        self.assertLessEqual(sum(1 for label in labels if label not in {"上一页", "下一页", "退出 Workers"}), 5)
        guard = 0
        while "下一页" in labels and guard < 5:
            token = next(token for text, token in _tokens(self.port) if text == "下一页")
            self.port.buttons.clear()
            await handle_workers_token(msg, self.port, self.settings, None, token)
            seen += "\n" + self.port.messages[-1][1]
            labels = [text for text, _token in _tokens(self.port)]
            guard += 1
        self.assertIn("Alpha", seen)
        self.assertIn("Beta", seen)
        self.assertIn("空闲", seen)
        # Re-open the first page and enter Alpha without binding.
        self.port.buttons.clear()
        await handle_workers_command(msg, self.port, None, self.settings, "")
        while True:
            match = next(((text, token) for text, token in _tokens(self.port) if "Alpha" in text), None)
            if match:
                break
            nxt = next(token for text, token in _tokens(self.port) if text == "下一页")
            self.port.buttons.clear()
            await handle_workers_token(msg, self.port, self.settings, None, nxt)
        self.port.buttons.clear()
        await handle_workers_token(msg, self.port, self.settings, None, match[1])
        detail = self.port.messages[-1][1]
        self.assertIn("Alpha", detail)
        self.assertIn("会话：", detail)
        actions = [text for text, _token in _tokens(self.port)]
        for label in ("💬继续对话", "📋查看任务", "🔄切换会话", "↩️返回列表"):
            self.assertIn(label, actions)
        self.assertIsNone(WorkerSessionStore(self.settings).selected("telegram", msg.chat_id, msg.operator_id))

        switch = next(token for text, token in _tokens(self.port) if text == "🔄切换会话")
        self.port.buttons.clear()
        await handle_workers_token(msg, self.port, self.settings, None, switch)
        choice_text = " ".join(text for text, _token in _tokens(self.port))
        self.assertIn("Alpha", choice_text)
        self.assertNotIn("Beta", choice_text)
        self.assertIn("新建会话", choice_text)
        back = next(token for text, token in _tokens(self.port) if text == "↩️返回列表")
        self.port.buttons.clear()
        await handle_workers_token(msg, self.port, self.settings, None, back)
        self.assertIn("我的 Workers", self.port.messages[-1][1])
        self.assertIsNone(WorkerSessionStore(self.settings).selected("telegram", msg.chat_id, msg.operator_id))

        created = WorkerSessionStore(self.settings).create(self.beta["id"], title="Side B")
        self.port.buttons.clear()
        await handle_workers_command(msg, self.port, None, self.settings, "")
        while True:
            match = next(((text, token) for text, token in _tokens(self.port) if "Beta" in text), None)
            if match:
                break
            nxt = next(token for text, token in _tokens(self.port) if text == "下一页")
            self.port.buttons.clear()
            await handle_workers_token(msg, self.port, self.settings, None, nxt)
        self.port.buttons.clear()
        await handle_workers_token(msg, self.port, self.settings, None, match[1])
        switch = next(token for text, token in _tokens(self.port) if text == "🔄切换会话")
        self.port.buttons.clear()
        await handle_workers_token(msg, self.port, self.settings, None, switch)
        side = next((token for text, token in _tokens(self.port) if "Side B" in text), None)
        # Side B may be on the next session page, independent of the worker page.
        if side is None and any(text == "下一页" for text, _token in _tokens(self.port)):
            nxt = next(token for text, token in _tokens(self.port) if text == "下一页")
            self.port.buttons.clear()
            await handle_workers_token(msg, self.port, self.settings, None, nxt)
            side = next(token for text, token in _tokens(self.port) if "Side B" in text)
        await handle_workers_token(msg, self.port, self.settings, None, side)
        selected = WorkerSessionStore(self.settings).selected("telegram", msg.chat_id, msg.operator_id)
        self.assertEqual(selected["session_id"], created["session_id"])

    async def test_legacy_discovery_keeps_suffix_and_operator(self) -> None:
        chat = "-100:topic:3"
        full = f"{chat}:agent:{self.alpha['id']}"
        get_transcript_store(self.settings).append(
            session_id=f"telegram:7:{full}", role="user", content="old alpha",
            channel="telegram", operator_id="7", source_chat_id=full,
        )
        get_transcript_store(self.settings).append(
            session_id=f"telegram:8:{full}", role="user", content="other operator",
            channel="telegram", operator_id="8", source_chat_id=full,
        )
        msg = self.message(chat=chat)
        self.agents.bind_chat("telegram", chat, self.alpha["id"])
        await handle_workers_command(msg, self.port, None, self.settings, "")
        while True:
            match = next(((text, token) for text, token in _tokens(self.port) if "Alpha" in text), None)
            if match:
                break
            nxt = next(token for text, token in _tokens(self.port) if text == "下一页")
            self.port.buttons.clear()
            await handle_workers_token(msg, self.port, self.settings, None, nxt)
        self.port.buttons.clear()
        await handle_workers_token(msg, self.port, self.settings, None, match[1])
        switch = next(token for text, token in _tokens(self.port) if "🔄切换会话" == text)
        self.port.buttons.clear()
        await handle_workers_token(msg, self.port, self.settings, None, switch)
        store = WorkerSessionStore(self.settings)
        legacy = store.get(f"telegram:7:{full}")
        self.assertIsNotNone(legacy)
        self.assertEqual(legacy["source_chat_id"], full)
        self.assertIsNone(store.get(f"telegram:8:{full}"))

    async def test_callback_scope_and_translation_fail_closed(self) -> None:
        msg = self.message()
        await handle_workers_command(msg, self.port, None, self.settings, "")
        _text, token = _tokens(self.port)[0]
        other = self.message(operator="8")
        self.port.messages.clear()
        await handle_workers_token(other, self.port, self.settings, None, token)
        self.assertIn("失效", self.port.messages[-1][1])
        other_topic = self.message(chat="-100:topic:9")
        self.port.messages.clear()
        await handle_workers_token(other_topic, self.port, self.settings, None, token)
        self.assertIn("失效", self.port.messages[-1][1])

        session = WorkerSessionStore(self.settings).create(self.alpha["id"])
        origin = PhysicalOrigin("telegram", "7", "-100:topic:3", "group", "9")
        wrapped = PhysicalOriginPort(self.port, origin, self.settings)
        self.assertIs(wrapped.wait_for_job, False)
        canonical = replace(msg, channel="web", operator_id="web-console", chat_id=session["source_chat_id"])
        before = len(self.port.buttons)
        await wrapped.reply_with_buttons(canonical, "confirm?", [[{"text": "ok", "callback_data": "tool:confirm:abc"}]])
        sent = self.port.buttons[before:]
        blob = json.dumps(sent)
        self.assertNotIn("tool:confirm:", blob)
        self.assertIn("wk:", blob)
        broken = PhysicalOriginPort(self.port, origin, self.settings)
        with patch("handlers.workers.WorkerSessionStore.issue_token", side_effect=Exception("no")):
            # AgentError is the fail-closed path; a generic failure must not leak either.
            pass
        from agents import AgentError
        with patch("handlers.workers.WorkerSessionStore.issue_token", side_effect=AgentError("no")):
            self.port.messages.clear()
            self.port.buttons.clear()
            await broken.reply_with_buttons(canonical, "confirm?", [[{"text": "ok", "callback_data": "tool:confirm:abc"}]])
        self.assertFalse(self.port.buttons)
        self.assertNotIn("tool:confirm:", self.port.messages[-1][1])
        self.assertIn("刷新", self.port.messages[-1][1])

    async def test_dispatch_rewrites_execution_and_keeps_physical_delivery(self) -> None:
        session = WorkerSessionStore(self.settings).create(self.alpha["id"], title="Side")
        WorkerSessionStore(self.settings).select("telegram", "-100:topic:3", "1", session["session_id"])
        seen = {}

        async def capture(msg, port, runner, mode=None, prompt=None, wait=None):
            seen["chat"] = msg.chat_id
            seen["channel"] = msg.channel
            seen["operator"] = msg.operator_id
            seen["wait"] = port.wait_for_job
            seen["origin"] = port.delivery_origin["chat_id"]
            await port.reply(msg, "done")

        msg = self.message("hello there", operator="1")
        with patch("handlers.dispatch.handle_codex_job", capture):
            await dispatch(msg, self.port, self.settings, SimpleNamespace(settings=self.settings))
        self.assertEqual(seen["chat"], session["source_chat_id"])
        self.assertEqual(seen["channel"], "web")
        self.assertEqual(seen["operator"], "web-console")
        self.assertIs(seen["wait"], False)
        self.assertEqual(seen["origin"], "-100:topic:3")
        self.assertEqual(self.port.messages[-1][0], "-100:topic:3")
        self.assertEqual(msg.chat_type, "group")

    async def test_enqueue_origin_survives_recovery(self) -> None:
        session = WorkerSessionStore(self.settings).create(self.alpha["id"])
        origin = PhysicalOrigin("feishu", "ou_1", "oc_room", "p2p", "m1")
        port = PhysicalOriginPort(Port(), origin, self.settings)
        msg = InboundMessage("web", "web-console", session["source_chat_id"], None, "ship it")
        ok, _text, job = await self.queue.enqueue(
            "run", "ship it", msg, port, runner=SimpleNamespace(settings=self.settings),
        )
        self.assertTrue(ok)
        self.assertIs(job._port, port)
        conn = sqlite3.connect(str(self.root / "state" / "job_queue.sqlite3"))
        raw = conn.execute("SELECT metadata_json, chat_id FROM queued_jobs WHERE id = ?", (job.id,)).fetchone()
        conn.close()
        metadata = json.loads(raw[0])
        self.assertEqual(metadata["delivery_origin"]["chat_id"], "oc_room")
        self.assertEqual(metadata["delivery_origin"]["channel"], "feishu")
        self.assertEqual(raw[1], session["source_chat_id"])
        revived = JobQueue()
        revived.configure(self.settings, runner=None, recover=False)
        loaded = await revived.get_job(job.id)
        self.assertIsNone(loaded._port)
        self.assertEqual(loaded.delivery_origin["chat_id"], "oc_room")
        self.assertEqual(loaded.chat_id, session["source_chat_id"])


class CardTests(unittest.TestCase):
    def test_workers_payload_is_token_only(self) -> None:
        card = workers_card("Alpha", [{"text": "打开", "token": "abc"}])
        button = card["elements"][1]["actions"][0]
        self.assertEqual(button["value"], {"action": "workers", "token": "abc"})
        self.assertEqual(parse_action(button["value"])["token"], "abc")
        self.assertIsNone(parse_action({"action": "workers"}))
        self.assertIsNone(parse_action({"action": "workers", "token": "abc", "job_id": "q1"}))
        self.assertIsNone(parse_action({"action": "workers", "token": "abc", "chat_id": "oc"}))


if __name__ == "__main__":
    unittest.main()
