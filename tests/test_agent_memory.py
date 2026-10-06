"""Each agent keeps its own long-term memory (phase 4)."""
from __future__ import annotations

import asyncio
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import agents
from agents import AgentStore
from personal_tools import long_term_memory as ltm

TOKEN = "t" * 40


def _settings(root: Path, **overrides):
    from config import Settings

    base = Settings(
        telegram_bot_token="test-token", telegram_allowed_user_id=1, codex_workspace_root=root,
        codex_bin="codex", codex_task_root=root / "tasks", codex_model=None, codex_timeout_seconds=30,
        codex_retry_429_delays_seconds=(), telegram_progress_seconds=1, codex_memory_root=root,
        user_timezone="UTC", agents_enabled=True, long_term_memory_enabled=True,
    )
    return replace(base, **overrides)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = _settings(self.root)
        self.store = AgentStore(self.settings)
        self.a = self.store.create({"name": "Astra"})
        self.b = self.store.create({"name": "Jobs"})
        self.chat_a, self.chat_b = agents.chat_id_for(self.a["id"]), agents.chat_id_for(self.b["id"])

    def run_tool(self, tool, arg, *, channel, chat_id, operator_id="web-console"):
        return asyncio.run(tool(self.settings, arg, operator_id=operator_id, channel=channel, chat_id=chat_id))


class OwnerTests(Case):
    def test_owner_of_each_kind_of_conversation(self) -> None:
        own = lambda channel, chat: ltm.owner_for_chat(self.settings, "op", channel, chat)  # noqa: E731
        self.assertEqual(own("web", self.chat_a), f"agent:{self.a['id']}")
        self.assertEqual(own("web", self.chat_b), f"agent:{self.b['id']}")
        # The default agent, Telegram, Feishu and older web sessions: the operator's store.
        for channel, chat in (("telegram", "42"), ("feishu", "oc_x"), ("web", "webchat-1"), ("web", "agent-default")):
            self.assertEqual(own(channel, chat), "op")
        off = _settings(self.root, agents_enabled=False)
        self.assertEqual(ltm.owner_for_chat(off, "op", "web", self.chat_a), "op")
        self.store.archive(self.a["id"])
        self.assertEqual(own("web", self.chat_a), "op")

    def test_agent_owner_survives_shared_mode(self) -> None:
        self.assertTrue(ltm.shared(self.settings))
        self.assertEqual(ltm._operator("someone", self.settings), ltm.SHARED_OWNER)
        self.assertEqual(ltm._operator(ltm.agent_owner("x1"), self.settings), "agent:x1")


class IsolationTests(Case):
    def test_chat_tools_keep_agents_apart(self) -> None:
        self.assertTrue(self.run_tool(ltm.memory_remember, "记住：考试在周五", channel="web", chat_id=self.chat_a).ok)
        self.assertTrue(self.run_tool(ltm.memory_remember, "记住：面试在周一", channel="web", chat_id=self.chat_b).ok)
        self.assertTrue(self.run_tool(ltm.memory_remember, "记住：我喜欢简短回答", channel="telegram", chat_id="42").ok)

        listed_a = self.run_tool(ltm.memory_list, "", channel="web", chat_id=self.chat_a).text
        listed_b = self.run_tool(ltm.memory_list, "", channel="web", chat_id=self.chat_b).text
        listed_default = self.run_tool(ltm.memory_list, "", channel="feishu", chat_id="oc_x").text
        self.assertIn("考试在周五", listed_a)
        self.assertNotIn("面试", listed_a)
        self.assertNotIn("简短回答", listed_a)
        self.assertIn("面试在周一", listed_b)
        self.assertNotIn("考试", listed_b)
        # Telegram and Feishu share the operator's store, as before agents existed.
        self.assertIn("简短回答", listed_default)
        self.assertNotIn("考试", listed_default)

        found = self.run_tool(ltm.memory_search, "面试", channel="web", chat_id=self.chat_a).text
        self.assertNotIn("周一", found)

    def test_prompt_block_carries_only_that_agents_facts(self) -> None:
        self.run_tool(ltm.memory_remember, "记住：考试在周五", channel="web", chat_id=self.chat_a)
        self.run_tool(ltm.memory_remember, "记住：面试在周一", channel="web", chat_id=self.chat_b)
        block_a = ltm.prompt_block(self.settings, ltm.owner_for_chat(self.settings, "web-console", "web", self.chat_a))
        block_default = ltm.prompt_block(self.settings, ltm.owner_for_chat(self.settings, "1", "telegram", "42"))
        self.assertIn("考试在周五", block_a)
        self.assertNotIn("面试", block_a)
        self.assertNotIn("考试", block_default)
        self.assertNotIn("面试", block_default)

    def test_forget_cannot_reach_another_agents_fact(self) -> None:
        self.run_tool(ltm.memory_remember, "记住：面试在周一", channel="web", chat_id=self.chat_b)
        row = ltm.list_facts(self.settings, ltm.agent_owner(self.b["id"]))[0]
        self.run_tool(ltm.memory_forget, f"#{row['id']}", channel="web", chat_id=self.chat_a)
        self.assertEqual(len(ltm.list_facts(self.settings, ltm.agent_owner(self.b["id"]))), 1)
        self.run_tool(ltm.memory_forget, f"#{row['id']}", channel="web", chat_id=self.chat_b)
        self.assertEqual(ltm.list_facts(self.settings, ltm.agent_owner(self.b["id"])), [])


class RoutineTests(Case):
    """A routine created in an agent's conversation is that agent's own check."""

    def setUp(self) -> None:
        super().setUp()
        import routines

        self.routines = routines
        self.settings = _settings(self.root, routines_enabled=True)
        self.seen: list = []

        async def fake_ask_chat(msg, port, settings, *, question, runner=None):
            self.seen.append(msg)
            port.last_text = "三套题已出好，听力 2 套、阅读 1 套。"
            return "answered", None

        patcher = mock.patch("handlers.chat.ask_chat", fake_ask_chat)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_routine(self, **create) -> dict:
        routine = self.routines.create_routine(self.settings, name="每日检查", schedule="0 9 * * *",
                                               prompt="检查学习计划", **create)
        return asyncio.run(self.routines.run_single_routine(self.settings, None, routine))

    def test_agents_routine_runs_as_the_agent_and_reports_into_its_conversation(self) -> None:
        from transcript_store import get_transcript_store

        run = self.run_routine(origin_chat_id=self.chat_a)
        self.assertEqual(run["status"], "ok")
        # It ran in the agent's own conversation, so instructions and memory are the agent's.
        self.assertEqual((self.seen[0].channel, self.seen[0].chat_id), ("web", self.chat_a))
        self.assertEqual(ltm.owner_for_chat(self.settings, "web-console", "web", self.seen[0].chat_id),
                         ltm.agent_owner(self.a["id"]))
        last = get_transcript_store(self.settings).last_message(self.a["session_id"])
        self.assertEqual(last["role"], "assistant")
        self.assertIn("每日检查", last["content"])
        self.assertIn("三套题已出好", last["content"])
        # The other agent's conversation is untouched.
        self.assertIsNone(get_transcript_store(self.settings).last_message(self.b["session_id"]))
        listed = self.routines.list_routines(self.settings)[0]
        self.assertEqual(listed["agent_id"], self.a["id"])

    def test_ordinary_routines_are_unchanged(self) -> None:
        from transcript_store import get_transcript_store

        self.run_routine()
        self.assertTrue(self.seen[0].chat_id.startswith("routine-"))
        self.assertIsNone(self.routines.list_routines(self.settings)[0]["agent_id"])
        self.assertIsNone(get_transcript_store(self.settings).last_message(self.a["session_id"]))
        # Neither does a routine of the default agent, or of an agent that is gone.
        self.run_routine(origin_chat_id="agent-default")
        self.assertTrue(self.seen[1].chat_id.startswith("routine-"))
        self.store.archive(self.b["id"])
        self.run_routine(origin_chat_id=self.chat_b)
        self.assertTrue(self.seen[2].chat_id.startswith("routine-"))


class HttpTests(Case):
    def setUp(self) -> None:
        super().setUp()
        from web_console import WebConsoleHandler, WebConsoleServer
        from web_control import WebControl

        conn = sqlite3.connect(str(self.root / "state" / "job_queue.sqlite3"))
        conn.execute("CREATE TABLE IF NOT EXISTS queued_jobs (id TEXT, channel TEXT, operator_id TEXT, chat_id TEXT)")
        conn.commit(); conn.close()
        queue = mock.MagicMock()
        queue.list_jobs.return_value = []
        queue._db_path.return_value = self.root / "state" / "job_queue.sqlite3"
        control = WebControl(self.settings, runner=mock.MagicMock(), queue=queue)
        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
        loop_thread.start()
        self.server = WebConsoleServer(("127.0.0.1", 0), WebConsoleHandler, control=control, loop=loop, token=TOKEN)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            self.server.shutdown(); self.server.server_close()
            loop.call_soon_threadsafe(loop.stop); loop_thread.join(timeout=2); thread.join(timeout=2)

        self.addCleanup(stop)

    def call(self, method: str, path: str, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        headers = {"Authorization": f"Bearer {TOKEN}"}
        payload = None
        if body is not None:
            payload = json.dumps(body); headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, data

    def test_console_memory_is_per_agent(self) -> None:
        a = self.a["id"]
        status, saved = self.call("POST", "/api/memory", {"text": "考试在周五", "agent": a})
        self.assertEqual(status, 201, saved)
        self.call("POST", "/api/memory", {"text": "我喜欢简短回答"})

        texts = lambda path: [item["text"] for item in self.call("GET", path)[1]["items"]]  # noqa: E731
        self.assertEqual(texts(f"/api/memory?agent={a}"), ["考试在周五"])
        self.assertEqual(texts(f"/api/memory?agent={self.b['id']}"), [])
        self.assertEqual(texts("/api/memory"), ["我喜欢简短回答"])
        self.assertEqual(texts("/api/memory?agent=default"), ["我喜欢简短回答"])   # default agent = operator's store

        self.assertEqual(self.call("GET", "/api/memory?agent=nope")[0], 404)
        self.assertEqual(self.call("POST", "/api/memory", {"text": "x", "agent": "nope"})[0], 404)

        # Deleting through the wrong owner does nothing.
        self.assertEqual(self.call("DELETE", f"/api/memory/{saved['id']}")[0], 404)
        self.assertEqual(self.call("DELETE", f"/api/memory/{saved['id']}?agent={self.b['id']}")[0], 404)
        self.assertEqual(self.call("DELETE", f"/api/memory/{saved['id']}?agent={a}")[0], 200)
        self.assertEqual(texts(f"/api/memory?agent={a}"), [])


    def test_library_collects_what_belongs_to_the_agent(self) -> None:
        import routines
        from desktop_screenshot import resolve_screenshot_dir

        a, b = self.a["id"], self.b["id"]
        self.server.control.settings = self.settings = _settings(
            self.root, routines_enabled=True, agent_desktops_enabled=True,
        )
        self.store.ensure_display(a)
        self.store.ensure_display(b)
        self.call("POST", "/api/memory", {"text": "考试在周五", "agent": a})
        status, created = self.call("POST", "/api/routines", {
            "name": "每日检查", "schedule": "0 9 * * *", "prompt": "检查学习计划", "agent": a,
        })
        self.assertEqual(status, 201, created)
        self.assertEqual(created["agent_id"], a)
        self.call("POST", "/api/routines", {"name": "别人的", "schedule": "0 9 * * *", "prompt": "x", "agent": b})
        self.assertEqual(self.call("POST", "/api/routines", {"name": "x", "schedule": "0 9 * * *", "prompt": "x",
                                                             "agent": "nope"})[0], 404)

        shots = resolve_screenshot_dir(self.settings)
        shots.mkdir(parents=True, exist_ok=True)
        for name, node in (("shot-a", f"x11:agent:{a}"), ("shot-b", f"x11:agent:{b}"), ("shot-host", "macbook-payton")):
            (shots / f"{name}.png").write_bytes(b"\x89PNG fake")
            (shots / f"{name}.json").write_text(json.dumps({"node_id": node, "width": 1, "height": 1}), encoding="utf-8")

        status, library = self.call("GET", f"/api/agents/{a}/library")
        self.assertEqual(status, 200)
        self.assertEqual(library["memory"]["profile"] + library["memory"]["log"], 1)
        self.assertEqual([r["name"] for r in library["routines"]], ["每日检查"])
        self.assertEqual([shot["id"] for shot in library["screenshots"]], ["shot-a"])
        self.assertEqual(self.call("GET", "/api/agents/nope/library")[0], 404)

        def png(agent: str, shot: str) -> int:
            conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
            conn.request("GET", f"/api/agents/{agent}/screenshots/{shot}", headers={"Authorization": f"Bearer {TOKEN}"})
            response = conn.getresponse()
            response.read()
            conn.close()
            return response.status

        self.assertEqual(png(a, "shot-a"), 200)
        # Another agent's screenshot, the host's, a missing one and a traversal attempt are all refused.
        for shot in ("shot-b", "shot-host", "missing", "..%2Fshot-a"):
            self.assertEqual(png(a, shot), 404, shot)


if __name__ == "__main__":
    unittest.main()
