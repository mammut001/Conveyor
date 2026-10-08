"""Owned worker sessions: registry, selection, tokens, web routes, chat cache."""
from __future__ import annotations

import asyncio
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

import agents
from agents import AgentError, AgentStore
from handlers import chat, chat_memory
from handlers.chat import LastRequest
from transcript_store import get_transcript_store
from worker_sessions import WorkerSessionStore

from tests.test_agents import TOKEN, _settings


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = _settings(self.root)
        self.agents = AgentStore(self.settings)
        self.store = WorkerSessionStore(self.settings)
        self.agent = self.agents.create({
            "name": "Astra",
            "instructions": "Study coach.",
            "workspace_path": str(self.root / "repo"),
        })

    def test_canonical_sessions_persist_and_do_not_replace_history(self) -> None:
        listed = self.store.list(self.agent["id"])
        self.assertEqual([item["kind"] for item in listed], ["main"])
        self.assertEqual(listed[0]["session_id"], self.agent["session_id"])

        created = self.store.create(self.agent["id"], title="  Notes  ")
        self.assertEqual(created["kind"], "created")
        self.assertNotEqual(created["session_id"], self.agent["session_id"])
        self.assertEqual(created["agent_id"], self.agent["id"])
        again = WorkerSessionStore(self.settings).list(self.agent["id"])
        self.assertEqual({item["session_id"] for item in again}, {self.agent["session_id"], created["session_id"]})
        self.assertTrue(all(item["channel"] == "web" for item in again))

        transcript = get_transcript_store(self.settings)
        transcript.append(
            session_id=created["session_id"], role="user", content="keep me",
            channel="web", operator_id="web-console", source_chat_id=created["source_chat_id"],
        )
        reloaded = WorkerSessionStore(self.settings).get(created["session_id"])
        self.assertEqual(reloaded["source_chat_id"], created["source_chat_id"])
        saved = transcript.get_session(created["session_id"])
        self.assertEqual([message["content"] for message in saved["messages"]], ["keep me"])

        self.agents.archive(self.agent["id"])
        with self.assertRaises(AgentError):
            WorkerSessionStore(self.settings).create(self.agent["id"])

    def test_selection_is_per_topic_and_operator(self) -> None:
        first = self.store.create(self.agent["id"], title="One")
        second = self.store.create(self.agent["id"], title="Two")
        self.store.select("telegram", "10:topic:2", "7", first["session_id"])
        self.store.select("telegram", "10:topic:3", "7", second["session_id"])
        self.store.select("telegram", "10:topic:2", "8", second["session_id"])
        store = WorkerSessionStore(self.settings)
        self.assertEqual(store.selected("telegram", "10:topic:2:agent:default", "7")["session_id"], first["session_id"])
        self.assertEqual(store.selected("telegram", "10:topic:3", "7")["session_id"], second["session_id"])
        self.assertEqual(store.selected("telegram", "10:topic:2", "8")["session_id"], second["session_id"])
        self.assertIsNone(store.selected("telegram", "10", "7"))
        store.clear("telegram", "10:topic:2", "7")
        self.assertIsNone(WorkerSessionStore(self.settings).selected("telegram", "10:topic:2", "7"))
        self.assertIsNotNone(store.selected("telegram", "10:topic:3", "7"))
        with self.assertRaises(AgentError):
            store.select("telegram", "10", "7", "web:web-console:not-a-session")

    def test_legacy_registration_stays_on_the_requesting_chat(self) -> None:
        bound = self.store.register_legacy(
            self.agent["id"], channel="telegram", operator_id="7", source_chat_id="10:topic:4",
            requester_operator="7", current_source="10:topic:4", title="Old",
        )
        self.assertEqual(bound["kind"], "legacy")
        self.assertNotIn(bound["session_id"], {item["session_id"] for item in self.store.list(self.agent["id"])})
        with self.assertRaises(AgentError):
            self.store.register_legacy(
                self.agent["id"], channel="telegram", operator_id="9", source_chat_id="10:topic:4",
                requester_operator="7", current_source="10:topic:4",
            )
        with self.assertRaises(AgentError):
            self.store.register_legacy(
                self.agent["id"], channel="telegram", operator_id="7", source_chat_id="10:topic:5",
                requester_operator="7", current_source="10:topic:4",
            )
        feishu = self.store.register_legacy(
            self.agent["id"], channel="feishu", operator_id="7", source_chat_id="oc_room",
            requester_operator="7", current_source="oc_room",
        )
        self.assertEqual(feishu["source_chat_id"], "oc_room")
        with self.assertRaises(AgentError):
            self.store.register_legacy(
                self.agent["id"], channel="feishu", operator_id="7", source_chat_id="oc_other",
                requester_operator="7", current_source="oc_room",
            )

    def test_callback_token_rejects_expiry_and_forged_scope(self) -> None:
        session = self.store.create(self.agent["id"])
        token = self.store.issue_token(
            operator_id="7", channel="telegram", topic="2", agent_id=self.agent["id"],
            session_id=session["session_id"], action="continue", page=1, extra={"n": 1}, ttl=30, now=100.0,
        )
        resolved = self.store.resolve_token(
            token, operator_id="7", channel="telegram", topic="2", agent_id=self.agent["id"],
            session_id=session["session_id"], action="continue", page=1, now=120.0,
        )
        self.assertEqual(resolved["extra"], {"n": 1})
        with self.assertRaises(AgentError):
            self.store.resolve_token(
                token, operator_id="7", channel="telegram", topic="2", agent_id=self.agent["id"],
                session_id=session["session_id"], action="continue", now=131.0,
            )
        for kwargs in (
            {"operator_id": "8"},
            {"channel": "feishu"},
            {"topic": "9"},
            {"agent_id": "default"},
            {"session_id": self.agent["session_id"]},
            {"action": "tasks"},
            {"page": 2},
        ):
            fields = dict(
                operator_id="7", channel="telegram", topic="2", agent_id=self.agent["id"],
                session_id=session["session_id"], action="continue", page=1, now=110.0,
            )
            fields.update(kwargs)
            with self.assertRaises(AgentError):
                self.store.resolve_token(token, **fields)

    def test_secondary_web_chat_uses_the_owning_agent(self) -> None:
        session = self.store.create(self.agent["id"])
        found = agents.agent_for_chat(self.settings, "web", session["source_chat_id"])
        self.assertEqual(found["id"], self.agent["id"])
        self.assertEqual(agents.workspace_for_chat(self.settings, "web", session["source_chat_id"]), self.root / "repo")
        name, instructions = agents.instructions_for_chat(self.settings, "web", session["source_chat_id"])
        self.assertEqual(name, "Astra")
        self.assertIn("Study", instructions)
        self.assertIsNone(agents.agent_for_chat(self.settings, "web", "agent-abcdef-s-0123456789ab"))
        self.agents.archive(self.agent["id"])
        self.assertIsNone(agents.agent_for_chat(self.settings, "web", session["source_chat_id"]))
        self.assertEqual(agents.agent_for_chat(self.settings, "web", "webchat-abc")["id"], "default")


class HttpTests(Case):
    def setUp(self) -> None:
        super().setUp()
        from web_console import WebConsoleHandler, WebConsoleServer
        from web_control import WebControl

        import sqlite3
        conn = sqlite3.connect(str(self.root / "state" / "job_queue.sqlite3"))
        conn.execute("CREATE TABLE IF NOT EXISTS queued_jobs (id TEXT, channel TEXT, operator_id TEXT, chat_id TEXT)")
        conn.commit()
        conn.close()
        queue = mock.MagicMock()
        queue.list_jobs.return_value = []
        queue._db_path.return_value = self.root / "state" / "job_queue.sqlite3"
        self.control = WebControl(self.settings, runner=mock.MagicMock(), queue=queue)
        self.control.list_approvals = lambda: []  # type: ignore[method-assign]
        self.loop = asyncio.new_event_loop()
        thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        thread.start()
        self.server = WebConsoleServer(
            ("127.0.0.1", 0), WebConsoleHandler, control=self.control, loop=self.loop, token=TOKEN,
        )
        server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        server_thread.start()

        def stop() -> None:
            self.server.shutdown()
            self.server.server_close()
            self.loop.call_soon_threadsafe(self.loop.stop)
            thread.join(timeout=2)
            server_thread.join(timeout=2)

        self.addCleanup(stop)

    def call(self, method: str, path: str, body=None, authorized: bool = True):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        headers = {"Authorization": f"Bearer {TOKEN}"} if authorized else {}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, data

    def test_session_routes_require_auth_and_round_trip(self) -> None:
        path = f"/api/agents/{self.agent['id']}/sessions"
        self.assertEqual(self.call("GET", path, authorized=False)[0], 401)
        self.assertEqual(self.call("POST", path, {"title": "x"}, authorized=False)[0], 401)
        status, created = self.call("POST", path, {"title": "Side"})
        self.assertEqual(status, 201)
        self.assertEqual(created["agent_id"], self.agent["id"])
        status, listed = self.call("GET", path)
        self.assertEqual(status, 200)
        ids = {item["session_id"] for item in listed["sessions"]}
        self.assertIn(created["session_id"], ids)
        self.assertIn(self.agent["session_id"], ids)
        self.assertTrue(all(item["kind"] != "legacy" for item in listed["sessions"]))
        self.assertEqual(self.call("GET", "/api/agents/nope/sessions")[0], 404)

        identity = self.control.resolve_session_identity(created["session_id"])
        self.assertEqual(identity, ("web", "web-console", created["source_chat_id"]))
        fetched = self.call("GET", f"/api/sessions/{quote(created['session_id'], safe='')}")
        self.assertEqual(fetched[0], 200)
        self.assertEqual(fetched[1]["messages"], [])
        self.assertEqual(fetched[1]["source_chat_id"], created["source_chat_id"])

    def test_list_aggregates_owned_sessions(self) -> None:
        created = self.store.create(self.agent["id"], title="Side")
        transcript = get_transcript_store(self.settings)
        transcript.append(
            session_id=created["session_id"], role="assistant", content="from the side session",
            channel="web", operator_id="web-console", source_chat_id=created["source_chat_id"],
        )
        self.control.queue.list_jobs.return_value = [{
            "channel": "web", "operator_id": "web-console", "chat_id": created["source_chat_id"],
            "state": "running",
        }]
        status, payload = self.call("GET", "/api/agents")
        self.assertEqual(status, 200)
        row = next(item for item in payload["agents"] if item["id"] == self.agent["id"])
        self.assertEqual(row["status"], "working")
        self.assertIn("side session", row["last_message"])
        self.assertIn(created["session_id"], {item["id"] for item in row["sessions"]})
        self.control.list_approvals = lambda: [{"session_id": created["session_id"]}]  # type: ignore[method-assign]
        row = next(item for item in self.call("GET", "/api/agents")[1]["agents"] if item["id"] == self.agent["id"])
        self.assertEqual(row["status"], "waiting")


class HistoryCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = _settings(self.root)
        chat.reset()
        self.addCleanup(chat.reset)

    def test_settings_history_follows_storage_not_the_process_cache(self) -> None:
        key = "telegram:42"
        chat.remember(key, "stale-user", "stale-assistant", 6)
        chat.set_last(key, LastRequest("stale-prompt", False))
        chat_memory.add_turn(self.root, key, "fresh-user", "fresh-assistant", 6)
        chat_memory.save_last_request(self.root, key, chat_memory.StoredLastRequest("fresh-prompt", True))
        turns = chat.history(key, 6, settings=self.settings)
        self.assertEqual(turns[-1]["content"], "fresh-assistant")
        self.assertTrue(any(turn["content"] == "fresh-user" for turn in turns))
        self.assertNotIn("stale-assistant", {turn["content"] for turn in turns})
        last = chat.pop_last(key, settings=self.settings)
        self.assertIsNotNone(last)
        self.assertEqual(last.codex_prompt, "fresh-prompt")
        self.assertTrue(last.confirm)
        self.assertIsNone(chat.pop_last(key, settings=self.settings))

        chat.remember(key, "memory-user", "memory-assistant", 6)
        chat.set_last(key, LastRequest("memory-prompt", False))
        self.assertEqual(chat.history(key, 6)[-1]["content"], "memory-assistant")
        self.assertEqual(chat.pop_last(key).codex_prompt, "memory-prompt")


if __name__ == "__main__":
    unittest.main()
