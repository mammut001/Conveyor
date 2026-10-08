"""Regressions for the workers-card review: fail closed, claims, legacy callbacks."""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import agents
from agents import AgentStore
from channel.types import InboundMessage
from handlers.job_queue import JobQueue, reset_job_queue
from handlers.jobs import JobMode, _execute_codex_job, _feishu_http_text
from handlers.tools.confirm import (
    clear_all_pending,
    create_pending,
    get_pending,
    list_pending,
    pop_pending,
)
from personal_tools import long_term_memory as ltm
from tests.test_agents import _settings
from transcript_store import get_transcript_store
from web_chat import resolve_or_create_session
from web_control import WebControl
from worker_sessions import WorkerSessionStore

ROOT = Path(__file__).resolve().parents[1]


class FailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = _settings(self.root, agent_desktops_enabled=True)
        self.agent = AgentStore(self.settings).create({
            "name": "Alpha", "instructions": "stay here", "workspace_path": str(self.root / "repo"),
        })
        self.secondary = f"agent-{self.agent['id']}-s-abc123abc123"
        self.primary = f"agent-{self.agent['id']}"

    def test_lookup_errors_do_not_use_host_or_operator_memory(self) -> None:
        boom = sqlite3.OperationalError("locked")
        with patch.object(WorkerSessionStore, "owner_agent_id", side_effect=boom):
            for chat in (self.secondary, self.primary):
                with self.assertRaises(agents.AgentError):
                    agents.workspace_for_chat(self.settings, "web", chat)
                with self.assertRaises(agents.AgentError):
                    agents.computer_target_for_chat(self.settings, "web", chat)
                with self.assertRaises(agents.AgentError):
                    agents.instructions_for_chat(self.settings, "web", chat)
                with self.assertRaises(sqlite3.OperationalError):
                    ltm.owner_for_chat(self.settings, "operator", "web", chat)
            self.assertIsNone(agents.workspace_for_chat(self.settings, "web", "web-plain"))
            self.assertEqual(agents.computer_target_for_chat(self.settings, "web", "web-plain")["scope"], "default")
            self.assertEqual(agents.instructions_for_chat(self.settings, "web", "web-plain"), ("", ""))
            self.assertEqual(ltm.owner_for_chat(self.settings, "operator", "web", "web-plain"), "operator")

    def test_archived_canonical_job_fails_instead_of_default_workspace(self) -> None:
        AgentStore(self.settings).archive(self.agent["id"])
        msg = InboundMessage("web", "web-console", self.primary, None, "ship it")
        port = SimpleNamespace(replies=[])

        async def reply(_msg, text):
            port.replies.append(text)
            return "1"

        port.reply = reply
        queue = JobQueue()
        queue.configure(self.settings, runner=None, recover=False)
        self.addCleanup(reset_job_queue)

        async def scenario():
            ok, _text, job = await queue.enqueue(
                "run", "ship it", msg, port, runner=SimpleNamespace(settings=self.settings),
            )
            self.assertTrue(ok)
            conn = sqlite3.connect(str(self.root / "state" / "job_queue.sqlite3"))
            conn.execute("UPDATE queued_jobs SET state = 'running' WHERE id = ?", (job.id,))
            conn.commit()
            conn.close()
            with patch("handlers.job_queue.get_job_queue", return_value=queue):
                await _execute_codex_job(
                    msg, port, SimpleNamespace(settings=self.settings), JobMode.RUN, "ship it",
                    queue_job_id=job.id,
                )
            loaded = await queue.get_job(job.id)
            self.assertEqual(loaded.state, "failed")
            self.assertTrue(port.replies)
            self.assertIn("不会转到默认项目", port.replies[-1])

        asyncio.run(scenario())


class SessionRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = _settings(self.root)
        self.agent = AgentStore(self.settings).create({"name": "Alpha", "instructions": "A"})
        self.queue = JobQueue()
        self.queue.configure(self.settings, runner=None, recover=False)
        self.addCleanup(reset_job_queue)
        self.control = WebControl(self.settings, runner=None, queue=self.queue)
        self.store = WorkerSessionStore(self.settings)

    def test_archive_and_delete_hide_secondary_and_reject_guesses(self) -> None:
        created = self.store.create(self.agent["id"])
        self.store.select("telegram", "42", "1", created["session_id"])
        self.assertTrue(self.control.archive_session(created["session_id"]))
        self.assertTrue(self.store.get(created["session_id"])["archived"])
        self.assertNotIn(created["session_id"], [item["session_id"] for item in self.store.list(self.agent["id"])])
        with self.assertRaises(agents.AgentError):
            self.store.selected("telegram", "42", "1")
        self.assertIsNone(resolve_or_create_session(self.control, created["source_chat_id"]))
        guessed = f"agent-{self.agent['id']}-s-deadbeefdead"
        self.assertIsNone(resolve_or_create_session(self.control, guessed))

        again = self.store.create(self.agent["id"])
        self.assertTrue(self.control.delete_session(again["session_id"]))
        self.assertTrue(self.store.get(again["session_id"])["archived"])
        self.assertIsNone(resolve_or_create_session(self.control, again["source_chat_id"]))

    def test_archived_or_unknown_primary_does_not_fall_open(self) -> None:
        primary = f"agent-{self.agent['id']}"
        durable = self.agent["session_id"]
        active = resolve_or_create_session(self.control, durable)
        self.assertEqual(active[2], primary)
        self.assertEqual(resolve_or_create_session(self.control, primary)[2], primary)
        secondary = self.store.create(self.agent["id"])
        self.assertEqual(
            resolve_or_create_session(self.control, secondary["source_chat_id"])[2],
            secondary["source_chat_id"],
        )
        self.assertEqual(
            resolve_or_create_session(self.control, secondary["session_id"])[2],
            secondary["source_chat_id"],
        )
        free = resolve_or_create_session(self.control, "web-plainchat")
        self.assertEqual(free[0], "web")
        self.assertEqual(free[2], "web-plainchat")
        legacy = get_transcript_store(self.settings)
        legacy.append(
            "telegram:1:42", "user", "hi",
            channel="telegram", operator_id="1", source_chat_id="42",
        )
        self.assertEqual(resolve_or_create_session(self.control, "telegram:1:42")[:3], ("telegram", "1", "42"))

        AgentStore(self.settings).archive(self.agent["id"])
        self.assertIsNone(resolve_or_create_session(self.control, durable))
        self.assertIsNone(resolve_or_create_session(self.control, primary))
        self.assertIsNone(resolve_or_create_session(self.control, secondary["source_chat_id"]))
        self.assertEqual(resolve_or_create_session(self.control, "web-plainchat")[2], "web-plainchat")
        self.assertEqual(resolve_or_create_session(self.control, "telegram:1:42")[0], "telegram")

    def test_archived_primary_transcript_missing_primary_and_disabled_feature(self) -> None:
        primary = f"agent-{self.agent['id']}"
        durable = self.agent["session_id"]
        get_transcript_store(self.settings).append(
            durable, "user", "kept",
            channel="web", operator_id="web-console", source_chat_id=primary,
        )
        AgentStore(self.settings).archive(self.agent["id"])
        self.assertIsNone(resolve_or_create_session(self.control, durable))
        self.assertIsNone(resolve_or_create_session(self.control, primary))

        missing = "agent-notarealid"
        self.assertIsNone(resolve_or_create_session(self.control, missing))
        self.assertIsNone(resolve_or_create_session(self.control, f"web:web-console:{missing}"))

        disabled = replace(self.settings, agents_enabled=False)
        queue = JobQueue()
        queue.configure(disabled, runner=None, recover=False)
        control = WebControl(disabled, runner=None, queue=queue)
        live = AgentStore(self.settings).create({"name": "Beta", "instructions": "B"})
        self.assertIsNone(resolve_or_create_session(control, live["session_id"]))
        self.assertIsNone(resolve_or_create_session(control, f"agent-{live['id']}"))
        self.assertEqual(resolve_or_create_session(control, "web-plainchat")[2], "web-plainchat")


class ConfirmationScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_all_pending()
        self.addCleanup(clear_all_pending)
        one = tempfile.TemporaryDirectory()
        two = tempfile.TemporaryDirectory()
        self.addCleanup(one.cleanup)
        self.addCleanup(two.cleanup)
        self.left = _settings(Path(one.name))
        self.right = _settings(Path(two.name))

    def test_settings_scope_and_atomic_claim(self) -> None:
        action = create_pending("notes.add", "left-only", "op", "chat", "web", settings=self.left)
        self.assertEqual([item.token for item in list_pending(channel="web", settings=self.right)], [])
        self.assertEqual(list_pending(channel="web", settings=self.left)[0].arg, "left-only")
        self.assertIsNone(get_pending(action.token, settings=self.right))
        bare = create_pending("notes.add", "legacy", "op", "other", "telegram")
        self.assertEqual(get_pending(bare.token).arg, "legacy")

        first = pop_pending(action.token, settings=self.left)
        from handlers.tools import confirm as confirm_mod
        confirm_mod._pending[action.token] = action
        second = pop_pending(action.token, settings=self.left)
        self.assertEqual(first.arg, "left-only")
        self.assertIsNone(second)
        self.assertNotIn(action.token, confirm_mod._pending)


class FeishuDeliveryTests(unittest.TestCase):
    def test_business_code_and_failures_are_not_delivered(self) -> None:
        settings = _settings(Path(tempfile.mkdtemp()), lark_app_id="cli_app", lark_app_secret="secret-value")
        calls = []

        class Resp:
            def __init__(self, raw, status=200):
                self.status = status
                self._raw = raw

            def read(self):
                return self._raw

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def urlopen(req, timeout=10):
            calls.append(req)
            if "tenant_access_token" in req.full_url:
                return Resp(json.dumps({"code": 0, "tenant_access_token": "tenant-token", "expire": 10}).encode())
            return Resp(json.dumps({"code": 0, "msg": "success", "data": {"message_id": "om_1"}}).encode())

        with patch("urllib.request.urlopen", urlopen):
            self.assertTrue(_feishu_http_text(settings, "oc_physical", "hello"))
        self.assertIn(b"oc_physical", calls[-1].data)
        self.assertEqual(dict(calls[-1].header_items())["Authorization"], "Bearer tenant-token")

        def rejected(req, timeout=10):
            if "tenant_access_token" in req.full_url:
                return Resp(json.dumps({"code": 0, "tenant_access_token": "tenant-token"}).encode())
            return Resp(json.dumps({"code": 999, "msg": "no permission", "data": {}}).encode())

        with patch("urllib.request.urlopen", rejected):
            self.assertFalse(_feishu_http_text(settings, "oc_physical", "hello"))

        def malformed(req, timeout=10):
            if "tenant_access_token" in req.full_url:
                return Resp(json.dumps({"code": 0, "tenant_access_token": "tenant-token"}).encode())
            return Resp(b"{")

        with patch("urllib.request.urlopen", malformed):
            self.assertFalse(_feishu_http_text(settings, "oc_physical", "hello"))

        def timed_out(req, timeout=10):
            raise TimeoutError("slow")

        with patch("urllib.request.urlopen", timed_out), self.assertLogs("handlers.jobs", level="WARNING") as logs:
            self.assertFalse(_feishu_http_text(settings, "oc_physical", "hello"))
        blob = "\n".join(logs.output)
        self.assertNotIn("tenant-token", blob)
        self.assertNotIn("secret-value", blob)
        self.assertNotIn("oc_physical", blob)

        blank = replace(settings, lark_app_id=None, lark_app_secret=None)
        with patch("urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called"))) :
            self.assertFalse(_feishu_http_text(blank, "oc_physical", "hello"))


def _telegram_update(data: str, *, chat_id: int = 42, chat_type: str = "private"):
    query = SimpleNamespace(data=data, answer=AsyncMock())
    user = SimpleNamespace(id=1, username="op")
    chat = SimpleNamespace(id=chat_id, type=chat_type)
    message = SimpleNamespace(text="", caption=None, message_id=3, reply_text=AsyncMock())
    return SimpleNamespace(
        effective_user=user, effective_chat=chat, effective_message=message, callback_query=query,
    )


def _telegram_callbacks(settings):
    """Load Telegram callback handlers without importing bot.py.

    Importing bot.py loads settings, starts logging, and configures the global
    job queue. The real ``_guard`` plus the real inbound/outbound helpers keep
    the allowlist and chat context. ``runner`` is only an argument dispatch
    receives.
    """
    from channel.auth import is_allowed
    from channel.telegram import inbound_from_update, make_outbound
    from handlers.tools.runner import cancel_pending, execute_confirmed, parse_tool_callback

    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"_guard", "tool_callback", "deep_callback"}
    chunks = [
        ast.get_source_segment(source, node)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted
    ]
    if len([chunk for chunk in chunks if chunk]) != 3:
        raise AssertionError("telegram callback sources missing")
    namespace: dict = {
        "settings": settings,
        "logger": logging.getLogger("telegram-callback-test"),
        "is_allowed": is_allowed,
        "inbound_from_update": inbound_from_update,
        "make_outbound": make_outbound,
        "parse_tool_callback": parse_tool_callback,
        "execute_confirmed": execute_confirmed,
        "cancel_pending": cancel_pending,
        "replace": replace,
        "Update": object,
        "ContextTypes": SimpleNamespace(DEFAULT_TYPE=object),
        "runner": SimpleNamespace(),
        "dispatch": AsyncMock(),
    }
    exec("\n\n".join(chunk for chunk in chunks if chunk), namespace)
    return namespace


class LegacyCallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = _settings(self.root, lark_allowed_open_id="ou_user")
        self.agent = AgentStore(self.settings).create({"name": "Alpha", "instructions": "A"})
        self.store = WorkerSessionStore(self.settings)
        self.session = self.store.create(self.agent["id"], title="Side")
        clear_all_pending()
        self.addCleanup(clear_all_pending)
        self.callbacks = _telegram_callbacks(self.settings)

    def _select_raw_legacy(self, chat_id: str) -> None:
        """Register and select the original raw IM session for this chat and operator."""
        from agents import DEFAULT_AGENT_ID

        scoped = self.store
        legacy = scoped.register_legacy(
            DEFAULT_AGENT_ID,
            channel="telegram",
            operator_id="1",
            source_chat_id=chat_id,
            requester_operator="1",
            current_source=chat_id,
        )
        scoped.select("telegram", chat_id, "1", legacy["session_id"])

    async def test_old_deep_button_does_not_run_another_workers_session(self) -> None:
        from channel.telegram_identity import context_tag

        self.store.select("telegram", "42", "1", self.session["session_id"])
        seen = []

        async def capture(msg, *_args):
            seen.append((msg.channel, msg.chat_id))

        update = _telegram_update(f"deep:{context_tag('42')}")
        replies = []

        async def _reply(_update, text, reply_markup=None):
            replies.append(text)

        self.callbacks["dispatch"] = capture
        self.callbacks["_reply"] = _reply
        await self.callbacks["deep_callback"](update, SimpleNamespace())
        self.assertEqual(seen, [])
        self.assertTrue(replies and "之前" in replies[-1])

        # A cleared private chat is the canonical main, so the raw button stays rejected.
        self.store.clear("telegram", "42", "1")
        await self.callbacks["deep_callback"](update, SimpleNamespace())
        self.assertEqual(seen, [])
        self.assertIn("之前", replies[-1])

        self._select_raw_legacy("42")
        await self.callbacks["deep_callback"](update, SimpleNamespace())
        self.assertEqual(seen, [("telegram", "42")])

    async def test_old_tool_callback_does_not_confirm_under_another_session(self) -> None:
        pending = create_pending("notes.add", "legacy", "1", "42", "telegram", settings=self.settings)
        self.store.select("telegram", "42", "1", self.session["session_id"])
        update = _telegram_update(f"tool:confirm:{pending.token}")
        execute = AsyncMock()
        replies = []

        async def _reply(_update, text, reply_markup=None):
            replies.append(text)

        self.callbacks["execute_confirmed"] = execute
        self.callbacks["_reply"] = _reply
        await self.callbacks["tool_callback"](update, SimpleNamespace())
        execute.assert_not_awaited()
        self.assertIn("之前", replies[-1])
        self.assertIsNotNone(get_pending(pending.token, settings=self.settings))

        self.store.clear("telegram", "42", "1")
        await self.callbacks["tool_callback"](update, SimpleNamespace())
        execute.assert_not_awaited()
        self.assertIn("之前", replies[-1])
        self.assertIsNotNone(get_pending(pending.token, settings=self.settings))

        self._select_raw_legacy("42")
        await self.callbacks["tool_callback"](update, SimpleNamespace())
        execute.assert_awaited()
        inbound = execute.await_args.args[0]
        self.assertEqual((inbound.channel, inbound.chat_id), ("telegram", "42"))

    async def test_group_without_selection_still_accepts_old_callbacks(self) -> None:
        from channel.telegram_identity import context_tag

        chat = "-100"
        self.store.select("telegram", chat, "1", self.session["session_id"])
        seen = []

        async def capture(msg, *_args):
            seen.append((msg.channel, msg.chat_id))

        deep = _telegram_update(f"deep:{context_tag(chat)}", chat_id=-100, chat_type="group")
        replies = []

        async def _reply(_update, text, reply_markup=None):
            replies.append(text)

        self.callbacks["dispatch"] = capture
        self.callbacks["_reply"] = _reply
        await self.callbacks["deep_callback"](deep, SimpleNamespace())
        self.assertEqual(seen, [])
        self.assertIn("之前", replies[-1])

        self.store.clear("telegram", chat, "1")
        await self.callbacks["deep_callback"](deep, SimpleNamespace())
        self.assertEqual(seen, [("telegram", chat)])

        pending = create_pending("notes.add", "legacy", "1", chat, "telegram", settings=self.settings)
        self.store.select("telegram", chat, "1", self.session["session_id"])
        tool = _telegram_update(f"tool:confirm:{pending.token}", chat_id=-100, chat_type="group")
        execute = AsyncMock()
        self.callbacks["execute_confirmed"] = execute
        await self.callbacks["tool_callback"](tool, SimpleNamespace())
        execute.assert_not_awaited()
        self.assertIn("之前", replies[-1])
        self.assertIsNotNone(get_pending(pending.token, settings=self.settings))

        self.store.clear("telegram", chat, "1")
        await self.callbacks["tool_callback"](tool, SimpleNamespace())
        execute.assert_awaited()
        inbound = execute.await_args.args[0]
        self.assertEqual((inbound.channel, inbound.chat_id), ("telegram", chat))
        self.assertIsNone(self.store.selected("telegram", chat, "1"))

    async def test_old_feishu_cards_do_not_follow_a_new_workers_session(self) -> None:
        import ast
        import dataclasses
        import logging

        from channel.auth import is_allowed
        from channel.feishu_cards import action_to_command, extract_card_action

        source = (ROOT / "feishu_bot.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        wanted = {"_extract_card_action_event", "_handle_card_action"}
        chunks = [
            ast.get_source_segment(source, node)
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted
        ]
        self.assertEqual(len(chunks), 2)
        namespace: dict = {
            "settings": self.settings,
            "logger": logging.getLogger("feishu-card-test"),
            "is_allowed": is_allowed,
            "extract_card_action": extract_card_action,
            "action_to_command": action_to_command,
            "InboundMessage": InboundMessage,
            "dataclasses": dataclasses,
            "asyncio": asyncio,
            "Any": object,
            "runner": SimpleNamespace(),
        }
        exec("\n\n".join(chunk for chunk in chunks if chunk), namespace)
        handle = namespace["_handle_card_action"]

        self.store.select("feishu", "oc_room", "ou_user", self.session["session_id"])
        pending = create_pending("notes.add", "legacy", "ou_user", "oc_room", "feishu", settings=self.settings)
        port = SimpleNamespace(messages=[])

        async def send_new(_msg, text):
            port.messages.append(text)
            return "1"

        port.send_new = send_new
        namespace["FeishuOutbound"] = lambda _channel: port
        namespace["_get_channel"] = lambda: None
        confirm = {
            "event": {
                "operator": {"open_id": "ou_user"},
                "action": {"value": {"action": "confirm", "token": pending.token}},
                "context": {"open_chat_id": "oc_room"},
            }
        }
        execute = AsyncMock()
        namespace["execute_confirmed"] = execute
        namespace["cancel_pending"] = AsyncMock()
        await handle(confirm)
        execute.assert_not_awaited()
        self.assertIn("之前", port.messages[-1])

        dispatch = AsyncMock()
        namespace["dispatch"] = dispatch
        status = {
            "event": {
                "operator": {"open_id": "ou_user"},
                "action": {"value": {"action": "status"}},
                "context": {"open_chat_id": "oc_room"},
            }
        }
        await handle(status)
        dispatch.assert_not_awaited()

        queue = JobQueue()
        queue.configure(self.settings, runner=None, recover=False)
        self.addCleanup(reset_job_queue)
        ok, _text, job = await queue.enqueue(
            "run", "ship",
            InboundMessage("web", "web-console", self.session["source_chat_id"], None, "ship"),
            SimpleNamespace(), runner=SimpleNamespace(settings=self.settings),
        )
        self.assertTrue(ok)
        matched = {
            "event": {
                "operator": {"open_id": "ou_user"},
                "action": {"value": {"action": "diff", "job_id": job.id}},
                "context": {"open_chat_id": "oc_room"},
            }
        }
        await handle(matched)
        dispatch.assert_awaited()
        forwarded = dispatch.await_args.args[0]
        self.assertEqual(forwarded.text, "/diff")


class FrontendSelectionTests(unittest.TestCase):
    def test_worker_selection_helpers(self) -> None:
        subprocess.check_call(
            ["node", "--test", "web/src/workerSelection.test.mjs"],
            cwd=ROOT,
        )
