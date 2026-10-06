"""Agents: one named agent per conversation (phase 1 — identity and prompts)."""
from __future__ import annotations

import asyncio
import http.client
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import agents
from agents import AgentError, AgentStore
from transcript_store import get_transcript_store

TOKEN = "t" * 40


def _settings(root: Path, **overrides):
    from config import Settings

    base = Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_id=1,
        codex_workspace_root=root,
        codex_bin="codex",
        codex_task_root=root / "tasks",
        codex_model=None,
        codex_timeout_seconds=30,
        codex_retry_429_delays_seconds=(),
        telegram_progress_seconds=1,
        codex_memory_root=root,
        user_timezone="UTC",
        agents_enabled=True,
    )
    return replace(base, **overrides)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = _settings(self.root)
        self.store = AgentStore(self.settings)


class StoreTests(Case):
    def test_default_agent_exists_and_cannot_be_removed(self) -> None:
        listed = self.store.list()
        self.assertEqual([a["id"] for a in listed], ["default"])
        self.assertTrue(listed[0]["is_default"])
        self.assertEqual(listed[0]["instructions"], "")
        with self.assertRaises(AgentError):
            self.store.archive("default")
        # Opening the store again must not duplicate or reset it.
        self.store.update("default", {"instructions": "be brief"})
        self.assertEqual(AgentStore(self.settings).get("default")["instructions"], "be brief")

    def test_create_update_archive(self) -> None:
        agent = self.store.create({"name": "  Astra  ", "instructions": "Study coach.", "workspace_path": "/srv/qwerty/"})
        self.assertEqual((agent["name"], agent["workspace_path"]), ("Astra", "/srv/qwerty"))
        self.assertEqual(agent["session_id"], f"web:web-console:agent-{agent['id']}")
        self.assertRegex(agent["color"], r"^#[0-9a-f]{6}$")

        updated = self.store.update(agent["id"], {"name": "Astra 2"})
        self.assertEqual((updated["name"], updated["instructions"]), ("Astra 2", "Study coach."))
        self.assertIsNone(self.store.update("nope", {"name": "x"}))

        self.assertTrue(self.store.archive(agent["id"]))
        self.assertEqual([a["id"] for a in self.store.list()], ["default"])
        self.assertTrue(self.store.get(agent["id"])["archived"])
        self.assertFalse(self.store.archive("nope"))

    def test_validation(self) -> None:
        for bad in (
            {"name": ""},
            {"name": "x" * 61},
            {"name": "ok", "instructions": "x" * 4001},
            {"name": "ok", "workspace_path": "relative/path"},
            {"name": "ok", "color": "red"},
        ):
            with self.assertRaises(AgentError, msg=repr(bad)):
                self.store.create(bad)
        for hostile in ("../../etc", "a b", "A", "x" * 40, ""):
            self.assertIsNone(self.store.get(hostile))


class RoutingTests(Case):
    def test_conversations_map_to_agents(self) -> None:
        astra = self.store.create({"name": "Astra", "instructions": "Study coach."})
        chat = agents.chat_id_for(astra["id"])
        self.assertEqual(agents.agent_for_chat(self.settings, "web", chat)["id"], astra["id"])
        # Everything that is not an agent's own web conversation is the default agent.
        for channel, chat_id in (("telegram", "12345"), ("feishu", "oc_x"), ("web", "webchat-abc"), ("web", "web-123")):
            self.assertEqual(agents.agent_for_chat(self.settings, channel, chat_id)["id"], "default")
        # A telegram chat that happens to be named like an agent chat is still default.
        self.assertEqual(agents.agent_for_chat(self.settings, "telegram", chat)["id"], "default")

    def test_archived_or_unknown_agent_has_no_say(self) -> None:
        astra = self.store.create({"name": "Astra", "instructions": "Study coach."})
        chat = agents.chat_id_for(astra["id"])
        self.store.archive(astra["id"])
        self.assertIsNone(agents.agent_for_chat(self.settings, "web", chat))
        self.assertEqual(agents.instructions_for_chat(self.settings, "web", chat), ("", ""))
        self.assertIsNone(agents.agent_for_chat(self.settings, "web", "agent-doesnotexist"))

    def test_off_means_no_agent_at_all(self) -> None:
        off = _settings(self.root, agents_enabled=False)
        self.store.update("default", {"instructions": "be brief"})
        self.assertIsNone(agents.agent_for_chat(off, "telegram", "1"))
        self.assertEqual(agents.instructions_for_chat(off, "telegram", "1"), ("", ""))
        self.assertFalse(agents.enabled(mock.MagicMock()))


class PromptTests(Case):
    def test_chat_prompt_carries_the_agents_instructions(self) -> None:
        from handlers.chat import system_prompt

        plain = system_prompt(self.settings, has_evidence=False)
        self.assertNotIn("standing instructions", plain)
        shaped = system_prompt(
            self.settings, has_evidence=False, agent_name="Astra", agent_instructions="Only talk about French exams.",
        )
        self.assertIn('you are the agent "Astra"', shaped)
        self.assertIn("Only talk about French exams.", shaped)
        # The safety rules still follow the agent section.
        self.assertLess(shaped.index("Only talk about French exams."), shaped.index("Rules:"))

    def test_execution_prompt_block(self) -> None:
        astra = self.store.create({"name": 'As"tra', "instructions": "Fix CI first."})
        block = agents.profile_block(
            *agents.instructions_for_chat(self.settings, "web", agents.chat_id_for(astra["id"]))
        )
        self.assertTrue(block.startswith("<agent-profile name=\"As'tra\""))
        self.assertIn("Fix CI first.", block)
        self.assertTrue(block.endswith("</agent-profile>\n\n"))
        # No instructions, no block: the default agent changes nothing.
        self.assertEqual(agents.profile_block(*agents.instructions_for_chat(self.settings, "telegram", "1")), "")


class HttpTests(Case):
    def setUp(self) -> None:
        super().setUp()
        from web_console import WebConsoleHandler, WebConsoleServer
        from web_control import WebControl

        # The session list joins against the queue table, which the real queue creates.
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
        loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        loop_thread.start()
        self.server = WebConsoleServer(
            ("127.0.0.1", 0), WebConsoleHandler, control=self.control, loop=self.loop, token=TOKEN,
        )
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            self.server.shutdown()
            self.server.server_close()
            self.loop.call_soon_threadsafe(self.loop.stop)
            loop_thread.join(timeout=2)
            thread.join(timeout=2)

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

    def test_requires_auth(self) -> None:
        self.assertEqual(self.call("GET", "/api/agents", authorized=False)[0], 401)
        self.assertEqual(self.call("POST", "/api/agents", {"name": "x"}, authorized=False)[0], 401)

    def test_crud_and_list_shows_preview_and_status(self) -> None:
        status, created = self.call("POST", "/api/agents", {"name": "Astra", "instructions": "Study coach."})
        self.assertEqual(status, 201)
        agent_id, session_id = created["id"], created["session_id"]

        store = get_transcript_store(self.settings)
        store.append(session_id=session_id, role="user", content="hello there", channel="web",
                     operator_id="web-console", source_chat_id=agents.chat_id_for(agent_id))
        store.append(session_id=session_id, role="assistant", content="修好了。  token=abcdef0123456789abcdef",
                     channel="web", operator_id="web-console", source_chat_id=agents.chat_id_for(agent_id))

        status, listed = self.call("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertTrue(listed["enabled"])
        by_id = {a["id"]: a for a in listed["agents"]}
        self.assertEqual(set(by_id), {"default", agent_id})
        astra = by_id[agent_id]
        self.assertEqual(astra["status"], "idle")
        self.assertEqual(astra["last_message_role"], "assistant")
        self.assertTrue(astra["last_message"].startswith("修好了。"))
        self.assertNotIn("abcdef0123456789abcdef", astra["last_message"])  # previews are redacted

        self.control.list_approvals = lambda: [{"session_id": session_id}]  # type: ignore[method-assign]
        waiting = {a["id"]: a for a in self.call("GET", "/api/agents")[1]["agents"]}
        self.assertEqual(waiting[agent_id]["status"], "waiting")
        self.assertEqual(waiting["default"]["status"], "idle")

        status, updated = self.call("PUT", f"/api/agents/{agent_id}", {"name": "Astra 2"})
        self.assertEqual((status, updated["name"]), (200, "Astra 2"))
        self.assertEqual(self.call("PUT", "/api/agents/nope", {"name": "x"})[0], 404)
        self.assertEqual(self.call("PUT", f"/api/agents/{agent_id}", {"name": ""})[0], 400)

        self.assertEqual(self.call("DELETE", "/api/agents/default")[0], 400)
        self.assertEqual(self.call("DELETE", f"/api/agents/{agent_id}")[0], 200)
        self.assertEqual(self.call("DELETE", f"/api/agents/{agent_id}")[0], 200)  # already archived: still exists
        self.assertEqual(self.call("DELETE", "/api/agents/nope")[0], 404)
        self.assertEqual([a["id"] for a in self.call("GET", "/api/agents")[1]["agents"]], ["default"])

    def test_new_agents_conversation_is_an_empty_session_not_a_404(self) -> None:
        _, created = self.call("POST", "/api/agents", {"name": "Astra"})
        from urllib.parse import quote
        status, session = self.call("GET", f"/api/sessions/{quote(created['session_id'], safe='')}")
        self.assertEqual(status, 200)
        self.assertEqual((session["messages"], session["jobs"], session["title"]), ([], [], "Astra"))
        self.assertEqual(self.call("GET", "/api/sessions/web%3Aweb-console%3Aagent-nope")[0], 404)

    def test_disabled_reports_and_refuses(self) -> None:
        self.control.settings = _settings(self.root, agents_enabled=False)
        status, listed = self.call("GET", "/api/agents")
        self.assertEqual((status, listed), (200, {"enabled": False, "agents": []}))
        self.assertEqual(self.call("POST", "/api/agents", {"name": "x"})[0], 409)
        self.assertEqual(self.call("PUT", "/api/agents/default", {"name": "x"})[0], 409)
        self.assertEqual(self.call("DELETE", "/api/agents/default")[0], 409)


if __name__ == "__main__":
    unittest.main()
