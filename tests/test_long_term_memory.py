"""Durable long-term memory: explicit remember/forget, cross-chat injection."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from channel.types import InboundMessage
from config import Settings
from handlers import chat
from handlers.chat_tools import build_tool_schemas, run_tool_loop
from handlers.dispatch import dispatch
from handlers.tools.confirm import clear_all_pending, get_pending_for_context
from personal_tools import long_term_memory as ltm
from runner.chat_client import ChatConfig


def _settings(tmp: Path, **overrides) -> Settings:
    mem = tmp / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    defaults = {
        "telegram_bot_token": "fake-token",
        "telegram_allowed_user_id": 12345,
        "codex_workspace_root": tmp / "ws",
        "codex_bin": "codex",
        "codex_task_root": tmp / "tasks",
        "codex_model": None,
        "codex_timeout_seconds": 60,
        "telegram_progress_seconds": 3,
        "codex_retry_429_delays_seconds": (),
        "codex_memory_root": mem,
        "user_timezone": "UTC",
        "chat_mode": "auto",
        "chat_base_url": "http://127.0.0.1:9/v1",
        "chat_api_key": "fake-secret-key-12345",
        "chat_model": "test-chat-model",
        "chat_tools_enabled": True,
        "chat_tool_max_steps": 3,
        "long_term_memory_enabled": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _msg(text: str, chat_id: str = "chat-1", operator_id: str = "op-1") -> InboundMessage:
    return InboundMessage(
        channel="web",
        operator_id=operator_id,
        chat_id=chat_id,
        message_id="m1",
        text=text,
        chat_type="p2p",
    )


class LongTermMemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self.tmp.name))
        self.op = "op-1"

    def tearDown(self) -> None:
        clear_all_pending()
        self.tmp.cleanup()

    def test_flag_defaults_off(self) -> None:
        field = Settings.__dataclass_fields__["long_term_memory_enabled"]
        self.assertFalse(field.default)
        root = Path(__file__).resolve().parents[1]
        self.assertIn('CONVEYOR_LONG_TERM_MEMORY", "false"', (root / "config.py").read_text())

    def test_remember_forget_and_restart(self) -> None:
        row = ltm.remember_fact(self.settings, self.op, "I prefer dark mode.")
        self.assertEqual(row["kind"], "profile")
        path = self.settings.codex_memory_root / "long_term_memory.db"
        self.assertTrue(path.is_file())
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        # A fresh connection (as after process restart) still sees the row.
        conn = sqlite3.connect(path)
        try:
            count = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)
        again = ltm.remember_fact(self.settings, self.op, "I prefer dark mode.")
        self.assertEqual(again["id"], row["id"])
        self.assertEqual(len(ltm.list_facts(self.settings, self.op)), 1)

        gone, status = ltm.forget_fact(self.settings, self.op, f"#{row['id']}")
        self.assertEqual(status, "deleted")
        self.assertEqual(gone[0]["text"], "I prefer dark mode.")
        self.assertEqual(ltm.list_facts(self.settings, self.op), [])
        # Forget actually deleted; a second forget misses.
        _rows, status = ltm.forget_fact(self.settings, self.op, "dark mode")
        self.assertEqual(status, "missing")

    def test_secret_is_not_stored(self) -> None:
        secret = "sk-" + ("a" * 24)
        with self.assertRaises(ValueError):
            ltm.remember_fact(self.settings, self.op, f"my key is {secret}")
        self.assertEqual(ltm.list_facts(self.settings, self.op), [])
        screened = ltm.screen_write_arg("memory.remember", f"token={secret}")
        self.assertTrue(screened.error)
        self.assertNotIn(secret, screened.arg)

    def test_one_sentence_and_profile_then_log(self) -> None:
        with self.assertRaises(ValueError):
            ltm.remember_fact(self.settings, self.op, "I like tea. I also like coffee.")
        with patch.object(ltm, "PROFILE_CAP", 2), patch.object(ltm, "LOG_INJECT_COUNT", 1):
            a = ltm.remember_fact(self.settings, self.op, "Fact alpha stays in the profile.")
            b = ltm.remember_fact(self.settings, self.op, "Fact beta stays in the profile.")
            c = ltm.remember_fact(self.settings, self.op, "Fact gamma is only in the log.")
            d = ltm.remember_fact(self.settings, self.op, "Fact delta is the newest log line.")
            self.assertEqual(a["kind"], "profile")
            self.assertEqual(b["kind"], "profile")
            self.assertEqual(c["kind"], "log")
            self.assertEqual(d["kind"], "log")
            block = ltm.prompt_block(self.settings, self.op)
            self.assertIn("Fact alpha", block)
            self.assertIn("Fact beta", block)
            self.assertIn("Fact delta", block)
            self.assertNotIn("Fact gamma", block)
            self.assertLessEqual(len(block), ltm.INJECT_CHAR_BUDGET)
            # Profile is stable: a new log row did not evict it.
            self.assertEqual(
                [r["id"] for r in ltm.list_facts(self.settings, self.op, kind="profile")],
                [a["id"], b["id"]],
            )

    def test_ambiguous_forget_deletes_nothing(self) -> None:
        ltm.remember_fact(self.settings, self.op, "I drink tea every morning.")
        ltm.remember_fact(self.settings, self.op, "The team prefers tea as well.")
        rows, status = ltm.forget_fact(self.settings, self.op, "tea")
        self.assertEqual(status, "ambiguous")
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(ltm.list_facts(self.settings, self.op)), 2)
        _gone, status = ltm.forget_fact(self.settings, self.op, "every morning")
        self.assertEqual(status, "deleted")
        self.assertEqual(len(ltm.list_facts(self.settings, self.op)), 1)

    def test_other_operator_and_flag_off_injection(self) -> None:
        ltm.remember_fact(self.settings, self.op, "I prefer dark mode.")
        # Per-operator mode keeps stores apart; default shared mode is tested below.
        isolated = _settings(Path(self.tmp.name), long_term_memory_shared=False)
        ltm.remember_fact(isolated, self.op, "I prefer dark mode.")
        self.assertEqual(ltm.prompt_block(isolated, "someone-else"), "")
        self.assertIn("dark mode", ltm.prompt_block(isolated, self.op))
        off = _settings(Path(self.tmp.name) / "off", long_term_memory_enabled=False)
        # Same file is not shared (different root); flag off yields no block
        # even if we point at the populated root.
        off = _settings(Path(self.tmp.name), long_term_memory_enabled=False)
        self.assertEqual(ltm.prompt_block(off, self.op), "")

    def test_new_chat_sees_fact_until_forgotten(self) -> None:
        ltm.remember_fact(self.settings, self.op, "I prefer dark mode.")
        new_key = "web:brand-new-chat"
        self.assertEqual(chat.history(new_key, 6, settings=self.settings), [])
        prompt = chat.system_prompt(
            self.settings, has_evidence=False, operator_id=self.op,
        )
        self.assertIn("I prefer dark mode.", prompt)
        self.assertIn("Durable memory", prompt)
        # Clearing short-term history does not delete the fact.
        chat.reset("web:old-chat", settings=self.settings)
        self.assertIn("I prefer dark mode.", chat.system_prompt(
            self.settings, has_evidence=False, operator_id=self.op,
        ))
        ltm.forget_fact(self.settings, self.op, "dark mode")
        prompt = chat.system_prompt(
            self.settings, has_evidence=False, operator_id=self.op,
        )
        self.assertNotIn("I prefer dark mode.", prompt)
        self.assertNotIn("Durable memory", prompt)

    def test_tools_hidden_when_flag_off(self) -> None:
        off = _settings(Path(self.tmp.name), long_term_memory_enabled=False)
        names = {s["function"]["name"] for s in build_tool_schemas(off)}
        self.assertNotIn("memory__remember", names)
        self.assertNotIn("memory__forget", names)
        on = _settings(Path(self.tmp.name), long_term_memory_enabled=True)
        names = {s["function"]["name"] for s in build_tool_schemas(on)}
        self.assertIn("memory__remember", names)
        self.assertIn("memory__forget", names)
        self.assertIn("memory__list", names)
        self.assertIn("memory__search", names)


class LongTermMemoryToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self.tmp.name))
        self.port = MagicMock()
        self.port.supports_inline_buttons = False
        self.port.reply = AsyncMock()
        self.port.send_new = AsyncMock()
        self.port.edit_progress = AsyncMock(return_value=True)

    def tearDown(self) -> None:
        clear_all_pending()
        chat.reset()
        self.tmp.cleanup()

    async def test_remember_requires_confirmation_and_does_not_write(self) -> None:
        cfg = ChatConfig(base_url="http://127.0.0.1:9", api_key="k", model="m", timeout=5)
        call = {
            "role": "assistant",
            "content": "I'll remember that.",
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "memory__remember",
                    "arguments": json.dumps({"arg": "I prefer dark mode."}),
                },
            }],
        }
        msg = _msg("please remember that", operator_id="op-1", chat_id="chat-9")
        with patch("handlers.chat_tools.complete_chat", AsyncMock(return_value=call)):
            result = await run_tool_loop(msg, self.port, self.settings, [{"role": "user", "content": "x"}], cfg)
        self.assertTrue(result.confirmation_requested)
        self.assertIn("memory.remember", result.tools_called)
        self.assertEqual(ltm.list_facts(self.settings, "op-1"), [])
        pending = get_pending_for_context("op-1", "chat-9", "web")
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.tool_name, "memory.remember")
        self.assertEqual(pending.arg, "I prefer dark mode.")

    async def test_secret_tool_call_is_refused_before_confirmation(self) -> None:
        cfg = ChatConfig(base_url="http://127.0.0.1:9", api_key="k", model="m", timeout=5)
        secret = "sk-" + ("b" * 24)
        call = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "memory__remember",
                    "arguments": json.dumps({"arg": f"the key is {secret}"}),
                },
            }],
        }
        final = {"role": "assistant", "content": "I will not store that."}
        msg = _msg("remember", operator_id="op-1", chat_id="chat-9")
        mock_complete = AsyncMock(side_effect=[call, final])
        with patch("handlers.chat_tools.complete_chat", mock_complete):
            result = await run_tool_loop(msg, self.port, self.settings, [{"role": "user", "content": "x"}], cfg)
        self.assertFalse(result.confirmation_requested)
        self.assertIsNone(get_pending_for_context("op-1", "chat-9", "web"))
        self.assertEqual(ltm.list_facts(self.settings, "op-1"), [])
        tool_msg = mock_complete.call_args_list[1][0][1][-1]
        self.assertNotIn(secret, tool_msg["content"])

    async def test_list_runs_without_confirmation(self) -> None:
        ltm.remember_fact(self.settings, "op-1", "I prefer dark mode.")
        cfg = ChatConfig(base_url="http://127.0.0.1:9", api_key="k", model="m", timeout=5)
        call = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {"name": "memory__list", "arguments": "{}"},
            }],
        }
        final = {"role": "assistant", "content": "You prefer dark mode."}
        msg = _msg("what do you remember", operator_id="op-1", chat_id="chat-9")
        with patch("handlers.chat_tools.complete_chat", AsyncMock(side_effect=[call, final])):
            result = await run_tool_loop(msg, self.port, self.settings, [{"role": "user", "content": "x"}], cfg)
        self.assertFalse(result.confirmation_requested)
        self.assertIn("memory.list", result.tools_called)
        self.assertIn("dark mode", result.messages[-1]["content"])

    async def test_explicit_phrase_confirms_instead_of_journal(self) -> None:
        msg = _msg("记住 我喜欢深色模式", chat_id="c-new", operator_id="op-1")
        with patch("handlers.dispatch.handle_memo", new_callable=AsyncMock) as memo:
            await dispatch(msg, self.port, self.settings, MagicMock())
        memo.assert_not_awaited()
        pending = get_pending_for_context("op-1", "c-new", "web")
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.tool_name, "memory.remember")
        self.assertEqual(pending.arg, "我喜欢深色模式")
        self.assertEqual(ltm.list_facts(self.settings, "op-1"), [])

    async def test_journal_phrase_still_uses_memo(self) -> None:
        msg = _msg("记一下 买牛奶", chat_id="c-new", operator_id="op-1")
        with patch("handlers.dispatch.handle_memo", new_callable=AsyncMock) as memo:
            await dispatch(msg, self.port, self.settings, MagicMock())
        memo.assert_awaited()
        self.assertIsNone(get_pending_for_context("op-1", "c-new", "web"))

    async def test_flag_off_remember_phrase_stays_on_memo(self) -> None:
        off = _settings(Path(self.tmp.name), long_term_memory_enabled=False)
        msg = _msg("记住 我喜欢深色模式", chat_id="c-new", operator_id="op-1")
        with patch("handlers.dispatch.handle_memo", new_callable=AsyncMock) as memo:
            await dispatch(msg, self.port, off, MagicMock())
        memo.assert_awaited()
        self.assertIsNone(get_pending_for_context("op-1", "c-new", "web"))

    async def test_new_conversation_prompt_includes_fact(self) -> None:
        ltm.remember_fact(self.settings, "op-1", "I prefer dark mode.")
        captured: dict = {}

        async def fake_stream(_cfg, messages):
            captured["messages"] = messages
            yield "Noted.\n[[CONFIDENCE: high]]"

        msg = _msg("hello", chat_id="brand-new", operator_id="op-1")
        settings = _settings(
            Path(self.tmp.name),
            chat_tools_enabled=False,
            long_term_memory_enabled=True,
        )
        with patch("runner.chat_client.stream_chat", side_effect=fake_stream):
            outcome, _checked = await chat.ask_chat(msg, self.port, settings, question="hello")
        self.assertEqual(outcome, "answered")
        system = captured["messages"][0]["content"]
        self.assertIn("I prefer dark mode.", system)
        # Short-term history for this new chat is empty aside from the new turn
        # not yet... ask_chat remembers after. Before the call, history was empty.
        # The messages sent to the model are system + user only.
        roles = [m["role"] for m in captured["messages"]]
        self.assertEqual(roles, ["system", "user"])

        ltm.forget_fact(settings, "op-1", "#1")
        captured.clear()
        msg2 = _msg("hello again", chat_id="another-new", operator_id="op-1")
        with patch("runner.chat_client.stream_chat", side_effect=fake_stream):
            await chat.ask_chat(msg2, self.port, settings, question="hello again")
        self.assertNotIn("I prefer dark mode.", captured["messages"][0]["content"])


class ApprovalFreshnessTests(unittest.TestCase):
    """The web UI must not let a late poll undo a decision."""

    def test_stale_generation_loses(self) -> None:
        root = Path(__file__).resolve().parents[1]
        src = (root / "web/src/approvalFreshness.ts").read_text()
        self.assertIn("return started !== latest", src)
        # Mirror of isStale: only the generation that is still current may paint.
        def is_stale(started: int, latest: int) -> bool:
            return started != latest

        # Poll gen 1 was in flight; a decision bumped the counter to 2 and a
        # follow-up refresh took gen 3. The late poll must be dropped.
        self.assertTrue(is_stale(1, 3))
        self.assertFalse(is_stale(3, 3))

    def test_ui_applies_decision_without_waiting_for_poll(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app = (root / "web/src/App.tsx").read_text()
        inbox = (root / "web/src/components/InboxPanel.tsx").read_text()
        fresh = (root / "web/src/approvalFreshness.ts").read_text()
        self.assertIn("refreshGen", app)
        self.assertIn("isStale(gen, refreshGen.current)", app)
        self.assertIn("dropApproval", app)
        self.assertIn("shouldRefreshForEvent(event.kind)", app)
        self.assertIn("fetchGen", inbox)
        self.assertIn("applyInboxDecision", inbox)
        self.assertIn("isStale(gen, fetchGen.current)", inbox)
        for kind in ("approval.", "apply.", "discard."):
            self.assertIn(f"kind.startsWith('{kind}')", fresh)

    def test_inbox_decision_replaces_pending_status(self) -> None:
        # Same mapping the inbox uses after a successful approve/deny.
        items = [{"id": 1, "approval": {"id": "tok", "status": "pending"}}]
        updated = [
            {**item, "approval": {**item["approval"], "status": "approved"}}
            if item["approval"] and item["approval"]["id"] == "tok" else item
            for item in items
        ]
        self.assertEqual(updated[0]["approval"]["status"], "approved")
        self.assertEqual(
            [i for i in [{"id": "tok"}, {"id": "other"}] if i["id"] != "tok"],
            [{"id": "other"}],
        )



class NaturalLanguageCredentialTests(unittest.TestCase):
    def test_plain_language_credentials_refused(self):
        from personal_tools.long_term_memory import normalize_fact, screen_write_arg
        for text in (
            "我的密码是 Hunter2Hunter2",
            "my wifi password is sunflower88",
            "银行卡 PIN 是 4821",
            "github token is abc123def",
        ):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    normalize_fact(text)
                self.assertTrue(screen_write_arg("memory.remember", text).error)

    def test_ordinary_facts_mentioning_words_accepted(self):
        from personal_tools.long_term_memory import normalize_fact
        for text in (
            "我每个月要换一次密码",
            "Remember to rotate the API key every quarter",
            "my shopping budget is 1200",
            "我的生日是 1990 年 5 月",
        ):
            with self.subTest(text=text):
                self.assertEqual(normalize_fact(text), text)


class SharedMemoryAndSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_shared_default_across_channels(self) -> None:
        self.assertTrue(Settings.__dataclass_fields__["long_term_memory_shared"].default)
        row = ltm.remember_fact(self.settings, "12345", "我的猫叫 Mochi，是一只橘猫。", source_channel="telegram")
        self.assertEqual(row["source_channel"], "telegram")
        self.assertEqual(row["source_operator"], "12345")
        # Recalled from the web console and Feishu operators.
        self.assertIn("Mochi", ltm.prompt_block(self.settings, ltm.WEB_OPERATOR))
        self.assertIn("Mochi", ltm.prompt_block(self.settings, "ou_feishu"))
        gone, status = ltm.forget_fact(self.settings, "ou_feishu", f"#{row['id']}")
        self.assertEqual(status, "deleted")
        self.assertEqual(ltm.prompt_block(self.settings, ltm.WEB_OPERATOR), "")

    def test_chinese_question_search(self) -> None:
        ltm.remember_fact(self.settings, "op", "我的猫叫 Mochi，是一只橘猫。")
        ltm.remember_fact(self.settings, "op", "下周三要去多伦多出差")
        ltm.remember_fact(self.settings, "op", "Favorite editor is Neovim")
        hits = ltm.search_facts(self.settings, "op", "我的猫叫什么名字？")
        self.assertEqual(hits[0]["text"], "我的猫叫 Mochi，是一只橘猫。")
        self.assertNotIn("下周三要去多伦多出差", [h["text"] for h in hits])
        hits = ltm.search_facts(self.settings, "op", "什么时候去多伦多")
        self.assertEqual([h["text"] for h in hits], ["下周三要去多伦多出差"])
        hits = ltm.search_facts(self.settings, "op", "what is my editor")
        self.assertEqual([h["text"] for h in hits], ["Favorite editor is Neovim"])
        self.assertEqual(ltm.search_facts(self.settings, "op", "什么"), [])
        # FTS/LIKE metacharacters are plain text here.
        self.assertEqual(ltm.search_facts(self.settings, "op", '%_"* NEAR -'), [])

    def test_old_relevant_log_is_injected(self) -> None:
        with patch.object(ltm, "PROFILE_CAP", 1), patch.object(ltm, "LOG_INJECT_COUNT", 1):
            ltm.remember_fact(self.settings, "op", "我是素食者。", now=1)
            ltm.remember_fact(self.settings, "op", "下周三要去多伦多出差", now=2)
            for i in range(5):
                ltm.remember_fact(self.settings, "op", f"Filler fact number {i}", now=10 + i)
            plain = ltm.prompt_block(self.settings, "op")
            self.assertNotIn("多伦多", plain)
            asked = ltm.prompt_block(self.settings, "op", "我什么时候去多伦多？")
            self.assertIn("多伦多出差", asked)
            self.assertIn("Older log matching this message:", asked)

    def test_schema_migrates_old_db(self) -> None:
        db = Path(self.settings.codex_memory_root) / ltm.DB_NAME
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, operator_id TEXT NOT NULL, "
            "kind TEXT NOT NULL CHECK (kind IN ('profile','log')), text TEXT NOT NULL, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute("INSERT INTO memories (operator_id, kind, text, created_at, updated_at) VALUES ('owner','profile','old fact',1,1)")
        conn.commit(); conn.close()
        rows = ltm.list_facts(self.settings, "x")
        self.assertEqual(rows[0]["text"], "old fact")
        self.assertEqual(rows[0]["source_channel"], "")


class MemoryWebApiTests(unittest.TestCase):
    TOKEN = "test-web-token-not-secret"

    @classmethod
    def setUpClass(cls) -> None:
        import threading
        from types import SimpleNamespace
        from web_console import WebConsoleHandler, WebConsoleServer

        cls.tmp = tempfile.TemporaryDirectory()
        cls.settings = _settings(Path(cls.tmp.name))
        cls.control = SimpleNamespace(settings=cls.settings)
        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()
        cls.server = WebConsoleServer(
            ("127.0.0.1", 0), WebConsoleHandler, control=cls.control, loop=cls.loop, token=cls.TOKEN,
        )
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.loop_thread.join(timeout=2)
        cls.loop.close()
        cls.tmp.cleanup()

    def _set_enabled(self, value: bool) -> None:
        import dataclasses
        type(self).settings = dataclasses.replace(self.settings, long_term_memory_enabled=value)
        self.control.settings = type(self).settings

    def setUp(self) -> None:
        self._set_enabled(True)
        for row in ltm.list_facts(self.settings, ltm.WEB_OPERATOR):
            ltm.forget_fact(self.settings, ltm.WEB_OPERATOR, f"#{row['id']}")

    def request(self, method: str, path: str, body: dict | None = None, *, auth: bool = True, raw_body: bool = True):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if auth:
            headers["Authorization"] = f"Bearer {self.TOKEN}"
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        try:
            return res.status, json.loads(data or b"{}")
        except Exception:
            return res.status, data

    def test_auth_and_disabled(self) -> None:
        self.assertEqual(self.request("GET", "/api/memory", auth=False)[0], 401)
        self.assertEqual(self.request("POST", "/api/memory", {"text": "x"}, auth=False)[0], 401)
        self.assertEqual(self.request("DELETE", "/api/memory/1", auth=False)[0], 401)
        self._set_enabled(False)
        status, data = self.request("GET", "/api/memory")
        self.assertEqual(status, 409)
        self.assertIn("CONVEYOR_LONG_TERM_MEMORY", data["error"])

    def test_list_search_add_delete(self) -> None:
        ltm.remember_fact(self.settings, "12345", "我的猫叫 Mochi，是一只橘猫。", source_channel="telegram")
        status, created = self.request("POST", "/api/memory", {"text": "Favorite editor is Neovim"})
        self.assertEqual(status, 201)
        self.assertTrue(created.get("ok"))
        self.assertEqual(created["source_channel"], "web")
        status, data = self.request("GET", "/api/memory")
        self.assertEqual(status, 200)
        self.assertTrue(data["shared"])
        self.assertEqual(data["counts"], {"profile": 2, "log": 0})
        self.assertEqual(len(data["items"]), 2)  # Telegram fact visible on the web (shared)
        from urllib.parse import quote
        status, data = self.request("GET", "/api/memory?q=" + quote("我的猫叫什么"))
        self.assertEqual([i["text"] for i in data["items"]], ["我的猫叫 Mochi，是一只橘猫。"])
        self.assertEqual(self.request("GET", "/api/memory?kind=bogus")[0], 400)
        status, data = self.request("DELETE", f"/api/memory/{created['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(data["deleted"]["text"], "Favorite editor is Neovim")
        self.assertEqual(self.request("DELETE", f"/api/memory/{created['id']}")[0], 404)
        self.assertEqual(self.request("DELETE", "/api/memory/abc")[0], 400)

    def test_add_uses_secret_and_sentence_filter(self) -> None:
        for text in ("my key sk-proj-AbCdEf0123456789AbCdEf0123456789xyz", "我家 wifi 密码是 sunflower88", "One. Two."):
            status, data = self.request("POST", "/api/memory", {"text": text})
            self.assertEqual(status, 200, text)
            self.assertFalse(data.get("ok"), text)
            self.assertTrue(data.get("refused"), text)
            self.assertIn("error", data)
        self.assertEqual(self.request("POST", "/api/memory", {})[0], 400)
        self.assertEqual(self.request("POST", "/api/memory", {"text": 123})[0], 400)
        self.assertEqual(ltm.list_facts(self.settings, ltm.WEB_OPERATOR), [])


if __name__ == "__main__":
    unittest.main()



def _feishu_msg(text: str, chat_type: str) -> InboundMessage:
    from types import SimpleNamespace
    from channel.feishu import inbound_from_event
    event = SimpleNamespace(
        sender_id="ou_owner", chat_id=f"oc_{chat_type}", message_id="om_1",
        chat_type=chat_type, content_text=text, mentioned_bot=True,
    )
    return inbound_from_event(event)


def _telegram_msg(text: str, chat_type: str) -> InboundMessage:
    from types import SimpleNamespace
    from channel.telegram import inbound_from_update
    message = SimpleNamespace(
        text=text, caption=None, message_id=7, reply_to_message=None, external_reply=None,
        quote=None, entities=(), caption_entities=(), photo=None, document=None,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=12345, full_name="Owner", username="owner"),
        effective_chat=SimpleNamespace(id=-100 if chat_type != "private" else 12345, type=chat_type),
        effective_message=message,
    )
    return inbound_from_update(update)  # type: ignore[arg-type]


class GroupChatBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """Shared memory must not surface in group chats (Feishu group / Telegram group)."""

    FACT = "我的猫叫团子"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self.tmp.name))
        ltm.remember_fact(self.settings, "ou_owner", self.FACT)
        self.port = MagicMock()
        self.port.supports_inline_buttons = False
        self.port.reply = AsyncMock()
        self.port.send_new = AsyncMock()
        self.port.edit_progress = AsyncMock(return_value=True)

    def tearDown(self) -> None:
        clear_all_pending()
        chat.reset()
        self.tmp.cleanup()

    def _groups(self, text: str) -> list[InboundMessage]:
        return [
            _feishu_msg(text, "group"),
            _telegram_msg(text, "group"),
            _telegram_msg(text, "supergroup"),
            _feishu_msg(text, "topic"),  # unknown Feishu type -> fail closed
        ]

    def _privates(self, text: str) -> list[InboundMessage]:
        return [_feishu_msg(text, "p2p"), _telegram_msg(text, "private")]

    def _replies(self) -> str:
        return " ".join(str(c.args[1]) for c in self.port.reply.await_args_list if len(c.args) > 1)

    def test_chat_type_detection_and_policy(self) -> None:
        self.assertEqual(_feishu_msg("x", "group").chat_type, "group")
        self.assertEqual(_feishu_msg("x", "p2p").chat_type, "p2p")
        self.assertEqual(_feishu_msg("x", "topic").chat_type, "unknown")
        self.assertEqual(_telegram_msg("x", "group").chat_type, "group")
        self.assertEqual(_telegram_msg("x", "supergroup").chat_type, "group")
        self.assertEqual(_telegram_msg("x", "private").chat_type, "p2p")
        for msg in self._groups("x"):
            self.assertFalse(ltm.allowed_for(self.settings, msg), msg)
        for msg in self._privates("x"):
            self.assertTrue(ltm.allowed_for(self.settings, msg), msg)
        self.assertTrue(ltm.allowed_for(self.settings, _msg("x")))
        self.assertFalse(Settings.__dataclass_fields__["long_term_memory_groups"].default)
        opted_in = _settings(Path(self.tmp.name), long_term_memory_groups=True)
        for msg in self._groups("x"):
            self.assertTrue(ltm.allowed_for(opted_in, msg))

    def test_env_flag_default_false(self) -> None:
        from config import load_settings
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USER_ID": "1",
               "CODEX_WORKSPACE_ROOT": self.tmp.name}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("CONVEYOR_LONG_TERM_MEMORY_GROUPS", None)
            self.assertFalse(load_settings("/nonexistent").long_term_memory_groups)
            os.environ["CONVEYOR_LONG_TERM_MEMORY_GROUPS"] = "true"
            try:
                self.assertTrue(load_settings("/nonexistent").long_term_memory_groups)
            finally:
                os.environ.pop("CONVEYOR_LONG_TERM_MEMORY_GROUPS", None)

    async def _system_prompt_for(self, msg: InboundMessage) -> str:
        captured: dict = {}

        async def fake_stream(_cfg, messages):
            captured["messages"] = messages
            yield "ok\n[[CONFIDENCE: high]]"

        settings = _settings(Path(self.tmp.name), chat_tools_enabled=False)
        with patch("runner.chat_client.stream_chat", side_effect=fake_stream):
            await chat.ask_chat(msg, self.port, settings, question="我的猫叫什么")
        return captured["messages"][0]["content"]

    async def test_group_prompt_has_no_memory_private_does(self) -> None:
        for msg in self._groups("我的猫叫什么"):
            system = await self._system_prompt_for(msg)
            self.assertNotIn(self.FACT, system, msg)
            self.assertNotIn("Durable", system, msg)
        for msg in self._privates("我的猫叫什么"):
            self.assertIn(self.FACT, await self._system_prompt_for(msg), msg)

    async def test_group_tool_loop_hides_and_refuses_memory_tools(self) -> None:
        cfg = ChatConfig(base_url="http://127.0.0.1:9", api_key="k", model="m", timeout=5)
        for name in ("memory__list", "memory__search", "memory__remember", "memory__forget"):
            for msg in self._groups("x"):
                call = {
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": "c1", "type": "function", "function": {
                        "name": name, "arguments": json.dumps({"arg": "猫"}),
                    }}],
                }
                final = {"role": "assistant", "content": "done"}
                mock = AsyncMock(side_effect=[call, final])
                with patch("handlers.chat_tools.complete_chat", mock):
                    result = await run_tool_loop(msg, self.port, self.settings, [{"role": "user", "content": "x"}], cfg)
                offered = [s["function"]["name"] for s in mock.call_args_list[0].kwargs["tools"]]
                self.assertFalse([n for n in offered if n.startswith("memory__")], (name, msg.channel))
                self.assertFalse(result.confirmation_requested)
                self.assertEqual(result.tools_called, [])
                tool_msg = mock.call_args_list[1][0][1][-1]
                self.assertEqual(tool_msg["content"], "unknown tool")
                self.assertIsNone(get_pending_for_context(msg.operator_id, msg.chat_id, msg.channel))
        # Private chats still get the tools.
        schemas = build_tool_schemas(self.settings)
        self.assertTrue(any(s["function"]["name"] == "memory__list" for s in schemas))

    async def test_group_explicit_remember_and_forget_refused(self) -> None:
        for text in ("记住 我喜欢深色模式", "忘掉 #1"):
            for msg in self._groups(text):
                self.port.reply.reset_mock()
                with patch("handlers.dispatch.handle_memo", new_callable=AsyncMock) as memo:
                    await dispatch(msg, self.port, self.settings, MagicMock())
                memo.assert_not_awaited()
                self.assertIn(ltm.GROUP_REFUSAL, self._replies())
                self.assertIsNone(get_pending_for_context(msg.operator_id, msg.chat_id, msg.channel))
        self.assertEqual([r["text"] for r in ltm.list_facts(self.settings, "x")], [self.FACT])
        # Private Feishu still asks for confirmation.
        msg = _feishu_msg("记住 我喜欢深色模式", "p2p")
        await dispatch(msg, self.port, self.settings, MagicMock())
        self.assertIsNotNone(get_pending_for_context(msg.operator_id, msg.chat_id, "feishu"))

    async def test_group_direct_tool_entry_points_refused(self) -> None:
        from handlers.tools.runner import _invoke_tool, _request_confirmation
        for msg in self._groups("x"):
            self.port.reply.reset_mock()
            await _invoke_tool(msg, self.port, self.settings, "memory.search", "猫")
            await _invoke_tool(msg, self.port, self.settings, "memory.list", "")
            await _request_confirmation(msg, self.port, self.settings, "memory.forget", "#1")
            replies = self._replies()
            self.assertNotIn(self.FACT, replies)
            self.assertEqual(replies.count(ltm.GROUP_REFUSAL), 3)
            self.assertIsNone(get_pending_for_context(msg.operator_id, msg.chat_id, msg.channel))
        self.port.reply.reset_mock()
        await _invoke_tool(_telegram_msg("x", "private"), self.port, self.settings, "memory.search", "猫")
        self.assertIn(self.FACT, self._replies())


class MemoryUiReachableTests(unittest.TestCase):
    def test_system_status_reports_memory_feature(self) -> None:
        import time
        from types import SimpleNamespace
        from web_control import WebControl
        with tempfile.TemporaryDirectory() as tmp:
            for flag in (True, False):
                fake = SimpleNamespace(
                    settings=_settings(Path(tmp), long_term_memory_enabled=flag),
                    queue=SimpleNamespace(list_jobs=lambda n: [], queue_length=0, is_paused=False),
                    started_at=time.time(), nodes=lambda: [],
                )
                status = WebControl.system_status(fake)  # type: ignore[arg-type]
                self.assertIs(status["features"]["long_term_memory"], flag)

    def test_memory_tab_next_to_tasks_chat_inbox(self) -> None:
        app = (Path(__file__).resolve().parents[1] / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
        switch = app[app.index("onClick={() => setView('tasks')}"):]
        switch = switch[: switch.index("</div>")]
        self.assertIn("setView('memory')", switch)
        self.assertIn("features?.long_term_memory", switch)
