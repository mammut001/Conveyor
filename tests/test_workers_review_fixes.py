"""Regressions for the workers-card review: fail closed, claims, legacy callbacks."""
from __future__ import annotations

import asyncio
import json
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


def _telegram_update(data: str):
    query = SimpleNamespace(data=data, answer=AsyncMock())
    user = SimpleNamespace(id=1, username="op")
    chat = SimpleNamespace(id=42, type="private")
    message = SimpleNamespace(text="", caption=None, message_id=3, reply_text=AsyncMock())
    return SimpleNamespace(
        effective_user=user, effective_chat=chat, effective_message=message, callback_query=query,
    )


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

    async def test_old_deep_button_does_not_run_another_workers_session(self) -> None:
        import bot
        from channel.telegram_identity import context_tag

        self.store.select("telegram", "42", "1", self.session["session_id"])
        seen = []

        async def capture(msg, *_args):
            seen.append((msg.channel, msg.chat_id))

        update = _telegram_update(f"deep:{context_tag('42')}")
        replies = []

        async def _reply(_update, text, reply_markup=None):
            replies.append(text)

        with patch("bot.settings", self.settings), patch("bot.dispatch", capture), patch("bot._reply", _reply):
            await bot.deep_callback(update, SimpleNamespace())
        self.assertEqual(seen, [])
        self.assertTrue(replies and "之前" in replies[-1])

        self.store.clear("telegram", "42", "1")
        with patch("bot.settings", self.settings), patch("bot.dispatch", capture), patch("bot._reply", _reply):
            await bot.deep_callback(update, SimpleNamespace())
        self.assertEqual(seen, [("telegram", "42")])

    async def test_old_tool_callback_does_not_confirm_under_another_session(self) -> None:
        import bot

        pending = create_pending("notes.add", "legacy", "1", "42", "telegram", settings=self.settings)
        self.store.select("telegram", "42", "1", self.session["session_id"])
        update = _telegram_update(f"tool:confirm:{pending.token}")
        execute = AsyncMock()
        replies = []

        async def _reply(_update, text, reply_markup=None):
            replies.append(text)

        with patch("bot.settings", self.settings), patch("bot.execute_confirmed", execute), patch("bot._reply", _reply):
            await bot.tool_callback(update, SimpleNamespace())
        execute.assert_not_awaited()
        self.assertIn("之前", replies[-1])
        self.assertIsNotNone(get_pending(pending.token, settings=self.settings))

        self.store.clear("telegram", "42", "1")
        with patch("bot.settings", self.settings), patch("bot.execute_confirmed", execute), patch("bot._reply", _reply):
            await bot.tool_callback(update, SimpleNamespace())
        execute.assert_awaited()

    async def test_old_feishu_cards_do_not_follow_a_new_workers_session(self) -> None:
        import feishu_bot

        self.store.select("feishu", "oc_room", "ou_user", self.session["session_id"])
        pending = create_pending("notes.add", "legacy", "ou_user", "oc_room", "feishu", settings=self.settings)
        port = SimpleNamespace(messages=[])

        async def send_new(_msg, text):
            port.messages.append(text)
            return "1"

        port.send_new = send_new
        confirm = {
            "event": {
                "operator": {"open_id": "ou_user"},
                "action": {"value": {"action": "confirm", "token": pending.token}},
                "context": {"open_chat_id": "oc_room"},
            }
        }
        execute = AsyncMock()
        with patch("feishu_bot.settings", self.settings), patch("feishu_bot.FeishuOutbound", return_value=port), \
                patch("feishu_bot.execute_confirmed", execute):
            await feishu_bot._handle_card_action(confirm)
        execute.assert_not_awaited()
        self.assertIn("之前", port.messages[-1])

        dispatch = AsyncMock()
        status = {
            "event": {
                "operator": {"open_id": "ou_user"},
                "action": {"value": {"action": "status"}},
                "context": {"open_chat_id": "oc_room"},
            }
        }
        with patch("feishu_bot.settings", self.settings), patch("feishu_bot.FeishuOutbound", return_value=port), \
                patch("feishu_bot.dispatch", dispatch):
            await feishu_bot._handle_card_action(status)
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
        with patch("feishu_bot.settings", self.settings), patch("feishu_bot.FeishuOutbound", return_value=port), \
                patch("feishu_bot.dispatch", dispatch):
            await feishu_bot._handle_card_action(matched)
        dispatch.assert_awaited()
        forwarded = dispatch.await_args.args[0]
        self.assertEqual(forwarded.text, "/diff")


class FrontendSelectionTests(unittest.TestCase):
    def test_worker_selection_helpers(self) -> None:
        subprocess.check_call(
            ["node", "--test", "web/src/workerSelection.test.mjs"],
            cwd=ROOT,
        )
