from __future__ import annotations

import asyncio
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers.job_queue import JobQueue
from handlers.tools.confirm import clear_all_pending, create_pending
from web_console import WebConsoleHandler, WebConsoleServer
from web_control import WebControl

TOKEN = "test-token-0123456789-abcdefghijklmnopqrstuvwxyz"


def parse_sse_events(raw_bytes: bytes) -> list[tuple[str, dict]]:
    text = raw_bytes.decode("utf-8")
    frames = text.split("\n\n")
    events = []
    for frame in frames:
        if not frame.strip():
            continue
        event_name = ""
        data_str = ""
        for line in frame.split("\n"):
            if line.startswith("event: "):
                event_name = line[len("event: "):].strip()
            elif line.startswith("data: "):
                data_str = line[len("data: "):]
        if event_name and data_str:
            try:
                events.append((event_name, json.loads(data_str)))
            except json.JSONDecodeError:
                pass
    return events


class WebChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        td = Path(cls.temp_dir.name)
        cls.settings = SimpleNamespace(
            codex_memory_root=td,
            codex_task_root=td,
            chat_mode="auto",
            chat_provider="custom",
            chat_model="test-model",
            chat_base_url="https://api.example.com/v1",
            chat_api_key="test-key",
            conveyor_chat_mode="auto",
            conveyor_chat_provider="custom",
            conveyor_chat_model="test-model",
            conveyor_chat_base_url="https://api.example.com/v1",
            conveyor_chat_api_key="test-key",
            chat_history_turns=5,
            chat_tools_enabled=True,
            chat_tool_max_steps=3,
            web_search_backend="disabled",
        )
        cls.queue = JobQueue()
        cls.queue.configure(cls.settings, runner=None, recover=False)
        cls.control = WebControl(cls.settings, runner=None, queue=cls.queue)

        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()

        cls.server = WebConsoleServer(
            ("127.0.0.1", 0), WebConsoleHandler,
            control=cls.control, loop=cls.loop, token=TOKEN,
        )
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.loop_thread.join(timeout=2)
        cls.server_thread.join(timeout=2)
        cls.temp_dir.cleanup()

    def setUp(self):
        clear_all_pending()
        self.settings.chat_mode = "auto"
        self.settings.chat_api_key = "test-key"
        self.settings.conveyor_chat_mode = "auto"
        self.settings.conveyor_chat_api_key = "test-key"
        self.settings.chat_tools_enabled = True

    def request(self, method, path, body=None, authorized=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = f"Bearer {TOKEN}"
        if method in ("POST", "PUT", "PATCH") and body is None:
            body = {}
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = raw
        return response.status, payload

    def request_sse(self, path, body, authorized=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = f"Bearer {TOKEN}"
        data = json.dumps(body).encode()
        conn.request("POST", path, body=data, headers=headers)
        response = conn.getresponse()
        events = []
        current_event = ""
        current_data = ""
        while True:
            line = response.readline().decode("utf-8")
            if not line:
                break
            line_str = line.rstrip("\r\n")
            if not line_str:
                if current_event and current_data:
                    try:
                        events.append((current_event, json.loads(current_data)))
                    except json.JSONDecodeError:
                        pass
                    if current_event == "done":
                        break
                    current_event = ""
                    current_data = ""
                continue
            if line_str.startswith("event: "):
                current_event = line_str[len("event: "):].strip()
            elif line_str.startswith("data: "):
                current_data = line_str[len("data: "):]
        conn.close()
        return response.status, events

    def test_auth_required(self):
        status, _ = self.request("POST", "/api/chat", {"message": "hi"}, authorized=False)
        self.assertEqual(status, 401)
        status, _ = self.request("GET", "/api/chat/history?session_id=foo", authorized=False)
        self.assertEqual(status, 401)
        status, _ = self.request("POST", "/api/approvals/test/approve", {}, authorized=False)
        self.assertEqual(status, 401)
        status, _ = self.request("POST", "/api/approvals/test/reject", {}, authorized=False)
        self.assertEqual(status, 401)

    def test_non_web_session_rejected(self):
        from unittest import mock
        with mock.patch.object(
            self.control, "resolve_session_identity",
            return_value=("telegram", "12345", "999"),
        ):
            status, body = self.request("POST", "/api/chat", {"message": "hi", "session_id": "tg-session"})
        self.assertEqual(status, 400)
        self.assertIn("web sessions", body.get("error", ""))

    def test_chat_disabled_409(self):
        self.settings.chat_mode = "off"
        self.settings.conveyor_chat_mode = "off"
        status, body = self.request("POST", "/api/chat", {"message": "hello"})
        self.assertEqual(status, 409)
        self.assertIn("chat tier is disabled", body.get("error", ""))

    def test_desktop_sentence_runs_the_computer_route(self):
        async def fake_handle(msg, port, runner, settings, route):
            await port.reply(msg, f"tool:{route.tools[0]}")

        ask = AsyncMock()
        with patch("handlers.tools.runner.handle_route", side_effect=fake_handle), patch(
            "handlers.chat.ask_chat", ask,
        ):
            status, events = self.request_sse("/api/chat", {"message": "打开计算器并点 1"})
        self.assertEqual(status, 200)
        ask.assert_not_called()
        messages = [data.get("text", "") for name, data in events if name == "message"]
        self.assertTrue(any("tool:computer.task" in text for text in messages))
        self.assertTrue(any(name == "done" and data.get("outcome") == "answered" for name, data in events))

    def test_plain_chat_sse_and_history(self):
        self.settings.chat_tools_enabled = False

        async def fake_stream_chat(_config, _messages):
            yield "Hello "
            yield "from "
            yield "Conveyor!"

        with patch("runner.chat_client.stream_chat", side_effect=fake_stream_chat):
            status, events = self.request_sse("/api/chat", {"message": "Hi there"})

        self.assertEqual(status, 200)
        self.assertTrue(len(events) >= 2)
        event_names = [e[0] for e in events]
        self.assertEqual(event_names[0], "session")
        session_id = events[0][1]["session_id"]
        self.assertTrue(session_id.startswith("web:web-console:webchat-"))

        self.assertEqual(event_names[-1], "done")
        self.assertEqual(events[-1][1]["outcome"], "answered")

        # Check message event
        msg_events = [e[1] for e in events if e[0] == "message"]
        self.assertTrue(len(msg_events) >= 1)
        self.assertEqual(msg_events[0]["text"], "Hello from Conveyor!")

        # Check history endpoint
        status, history_body = self.request("GET", f"/api/chat/history?session_id={session_id}")
        self.assertEqual(status, 200)
        self.assertEqual(history_body["session_id"], session_id)
        messages = history_body["messages"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["text"], "Hi there")
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertEqual(messages[1]["text"], "Hello from Conveyor!")

    def test_read_tool_roundtrip(self):
        self.settings.chat_tools_enabled = True

        responses = [
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "tc-1",
                    "type": "function",
                    "function": {
                        "name": "service_status",
                        "arguments": json.dumps({"arg": "conveyor"}),
                    },
                }],
            },
            {
                "role": "assistant",
                "content": "Conveyor service is running smoothly.",
            },
        ]

        async def fake_complete_chat(_config, _messages, tools=None):
            return responses.pop(0)

        async def fake_run_tool(_settings, tool_name, arg, **_kwargs):
            return "Active: active (running)"

        with (
            patch("handlers.chat_tools.complete_chat", side_effect=fake_complete_chat),
            patch("handlers.chat_tools.run_tool", side_effect=fake_run_tool),
        ):
            status, events = self.request_sse("/api/chat", {"message": "How is conveyor?"})

        self.assertEqual(status, 200)
        event_names = [e[0] for e in events]
        self.assertEqual(event_names[0], "session")
        self.assertEqual(event_names[-1], "done")
        self.assertEqual(events[-1][1]["outcome"], "answered")

        messages = [e[1] for e in events if e[0] == "message"]
        self.assertTrue(len(messages) >= 1)
        self.assertEqual(messages[0]["text"], "Conveyor service is running smoothly.")

    def test_write_tool_approval_lifecycle(self):
        self.settings.chat_tools_enabled = True

        tool_resp = {
            "role": "assistant",
            "tool_calls": [{
                "id": "tc-write",
                "type": "function",
                "function": {
                    "name": "service_restart",
                    "arguments": json.dumps({"arg": "conveyor"}),
                },
            }],
        }

        async def fake_complete_chat(_config, _messages, tools=None):
            return tool_resp

        with patch("handlers.chat_tools.complete_chat", side_effect=fake_complete_chat):
            status, events = self.request_sse("/api/chat", {"message": "Restart conveyor"})

        self.assertEqual(status, 200)
        approval_events = [e[1] for e in events if e[0] == "approval"]
        self.assertEqual(len(approval_events), 1)
        token = approval_events[0]["id"]
        self.assertEqual(approval_events[0]["tool_name"], "service_restart")
        self.assertEqual(approval_events[0]["arg"], "conveyor")

        # GET /api/approvals lists it with kind tool
        status, apprs_body = self.request("GET", "/api/approvals")
        self.assertEqual(status, 200)
        tool_apprs = [a for a in apprs_body["approvals"] if a.get("id") == token]
        self.assertEqual(len(tool_apprs), 1)
        self.assertEqual(tool_apprs[0]["kind"], "tool")
        self.assertEqual(tool_apprs[0]["tool_name"], "service_restart")
        self.assertEqual(tool_apprs[0]["status"], "pending")

        # Approve executes the tool exactly once
        execute_mock = AsyncMock(return_value="Restarted conveyor unit.")
        with patch("handlers.tools.runner.run_tool", side_effect=execute_mock):
            status, result = self.request("POST", f"/api/approvals/{token}/approve")

        self.assertEqual(status, 200)
        self.assertEqual(result["kind"], "tool")
        self.assertEqual(result["status"], "accepted")
        self.assertIn("Restarted conveyor unit.", result["result"])
        execute_mock.assert_called_once()

        # Second approve returns 404 / expired
        status, _ = self.request("POST", f"/api/approvals/{token}/approve")
        self.assertEqual(status, 404)

        # Reject path
        token2_action = create_pending("service_restart", "conveyor", "web-console", "web-ch2", "web")
        token2 = token2_action.token

        run_mock = AsyncMock()
        with patch("handlers.tools.runner.run_tool", side_effect=run_mock):
            status, reject_result = self.request("POST", f"/api/approvals/{token2}/reject")

        self.assertEqual(status, 200)
        self.assertEqual(reject_result["kind"], "tool")
        self.assertEqual(reject_result["status"], "rejected")
        self.assertIn("已取消", reject_result["result"])
        # Executor must NOT run on reject
        run_mock.assert_not_called()

    def test_history_unknown_session_is_empty_200(self):
        status, body = self.request("GET", "/api/chat/history?session_id=web%3Aweb-console%3Awebchat-doesnotexist")
        self.assertEqual(status, 200)
        self.assertEqual(body["messages"], [])

    def test_unpersisted_durable_session_id_is_accepted(self):
        self.settings.chat_tools_enabled = False

        async def fake_stream_chat(_config, _messages):
            yield "ok"

        sid = "web:web-console:webchat-abc123def456"
        with patch("runner.chat_client.stream_chat", side_effect=fake_stream_chat):
            status, events = self.request_sse("/api/chat", {"message": "hi", "session_id": sid})
        self.assertEqual(status, 200)
        self.assertEqual(events[0][1]["session_id"], sid)

    def test_history_resolves_approval_prompts(self):
        self.settings.chat_tools_enabled = True
        tool_resp = {
            "role": "assistant",
            "tool_calls": [{"id": "tc-h", "type": "function", "function": {
                "name": "service_restart", "arguments": json.dumps({"arg": "conveyor"}),
            }}],
        }

        async def fake_complete_chat(_config, _messages, tools=None):
            return tool_resp

        def ask(sid=None):
            body = {"message": "Restart conveyor"}
            if sid:
                body["session_id"] = sid
            with patch("handlers.chat_tools.complete_chat", side_effect=fake_complete_chat):
                _, events = self.request_sse("/api/chat", body)
            names = [e[0] for e in events]
            self.assertNotIn("message", names)  # redundant "已请求确认" note suppressed
            return events[0][1]["session_id"], next(e[1]["id"] for e in events if e[0] == "approval")

        sid, t1 = ask()
        _, t2 = ask(sid)
        _, t3 = ask(sid)

        def approvals():
            _, body = self.request("GET", f"/api/chat/history?session_id={sid}")
            return [m["approval"] for m in body["messages"] if m.get("approval")]

        self.assertEqual([a["status"] for a in approvals()], ["pending", "pending", "pending"])
        with patch("handlers.tools.runner.run_tool", AsyncMock(return_value="Restarted.")):
            self.assertEqual(self.request("POST", f"/api/approvals/{t1}/approve")[0], 200)
        self.assertEqual(self.request("POST", f"/api/approvals/{t2}/reject")[0], 200)
        from handlers.tools.confirm import pop_pending
        pop_pending(t3)  # simulate TTL expiry / restart
        result = approvals()
        self.assertEqual([a["status"] for a in result], ["approved", "denied", "expired"])
        self.assertEqual({a["tool_name"] for a in result}, {"service_restart"})

    def test_history_legacy_prompt_without_metadata_is_closed(self):
        from web_chat import build_history
        session = {"messages": [
            {"role": "user", "content": "add note", "kind": "chat", "metadata": {}},
            {"role": "assistant", "content": "⚠️ 危险操作需确认\n\n工具: notes.add\n\n确认执行？", "kind": "chat", "metadata": {}},
        ]}
        out = build_history(session)
        self.assertNotIn("approval", out[0])
        self.assertEqual(out[1]["approval"]["status"], "closed")
        self.assertEqual(build_history(None), [])

    def test_plain_console_takeover_status_unavailable(self):
        status, body = self.request("GET", "/api/takeover/status")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"available": False, "enabled": False})
        status, _ = self.request("GET", "/api/takeover/status", authorized=False)
        self.assertEqual(status, 401)

    def test_telegram_token_rejected_from_web(self):
        tg_action = create_pending("service_restart", "nginx", "user1", "tg1", "telegram")
        token = tg_action.token

        status, _ = self.request("POST", f"/api/approvals/{token}/approve")
        self.assertEqual(status, 404)

        status, _ = self.request("POST", f"/api/approvals/{token}/reject")
        self.assertEqual(status, 404)

    def test_existing_job_approvals_have_kind_job(self):
        # Insert a job approval directly into web_approvals
        conn = self.control._connect()
        try:
            with conn:
                conn.execute(
                    """INSERT INTO web_approvals (id, job_id, action, status, created_at, expires_at)
                       VALUES ('job-appr-1', 'job-123', 'apply', 'pending', '2026-01-01T00:00:00Z', ?)""",
                    (time.time() + 300,),
                )
        finally:
            conn.close()

        status, body = self.request("GET", "/api/approvals")
        self.assertEqual(status, 200)
        job_apprs = [a for a in body["approvals"] if a.get("id") == "job-appr-1"]
        self.assertEqual(len(job_apprs), 1)
        self.assertEqual(job_apprs[0]["kind"], "job")
        self.assertEqual(job_apprs[0]["job_id"], "job-123")
        self.assertEqual(job_apprs[0]["action"], "apply")


if __name__ == "__main__":
    unittest.main()
