"""A failed model call keeps the owner's question and says why, in Chinese.

Reproduces the onboarding failure: 提问 with an unreachable provider used to
return 202 with a session_id that was never stored, so the question vanished on
reload. The request is now persisted before the model call and the provider
failure is stored as a plain Chinese message in the same session.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import quote

import httpx

from channel.types import InboundMessage
from handlers.job_queue import JobQueue
from runner.chat_client import ChatError, owner_chat_error_text
from conveyor.entrypoints.web_console import WebConsoleHandler, WebConsoleServer, WebOutbound
from conveyor.web.web_control import WebControl

TOKEN = "test-token-0123456789-abcdefghijklmnopqrstuvwxyz"
PROMPT = "请用一句话说明这台电脑现在是否正常。"


class TaskProviderFailureTests(unittest.TestCase):
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
            chat_base_url="https://qa-nonexistent.invalid/v1",
            chat_api_key="test-key",
            conveyor_chat_mode="auto",
            conveyor_chat_provider="custom",
            conveyor_chat_model="test-model",
            conveyor_chat_base_url="https://qa-nonexistent.invalid/v1",
            conveyor_chat_api_key="test-key",
            chat_history_turns=5,
            chat_tools_enabled=False,
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

    def request(self, method, path, body=None, authorized=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
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

    # -- the failure text ---------------------------------------------------

    def test_dns_failure_text_is_chinese_and_names_the_setting(self) -> None:
        # Exactly what httpx raises for a host that does not resolve.
        text = owner_chat_error_text(str(httpx.ConnectError("Cannot connect to host qa-nonexistent.invalid")))
        self.assertIn("连不上模型服务", text)
        self.assertIn("接口地址", text)

    # -- the 提问 request ---------------------------------------------------

    def test_unreachable_provider_keeps_the_question_and_the_reason(self) -> None:
        async def unreachable(config, messages):
            # The exception a DNS failure raises inside the provider client.
            raise httpx.ConnectError("Cannot connect to host qa-nonexistent.invalid:443")
            yield  # pragma: no cover - unreachable, keeps this an async generator

        with patch("runner.chat_client.stream_chat", new=unreachable):
            status, body = self.request("POST", "/api/tasks", {"prompt": PROMPT, "mode": "run"})

        self.assertEqual(status, 202, body)
        session_id = body["session_id"]
        self.assertTrue(session_id)

        status, detail = self.request("GET", f"/api/sessions/{quote(session_id, safe='')}")
        self.assertEqual(status, 200, detail)
        messages = detail["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
        self.assertEqual(messages[0]["content"], PROMPT)
        self.assertIn("连不上模型服务", messages[1]["content"])
        # The session is listed, so it survives a reload.
        _, listed = self.request("GET", "/api/sessions")
        self.assertIn(session_id, [item["id"] for item in listed["sessions"]])

    def test_retry_after_a_failure_stores_both_turns(self) -> None:
        async def unreachable(config, messages):
            raise httpx.ConnectError("Cannot connect to host qa-nonexistent.invalid:443")
            yield  # pragma: no cover

        with patch("runner.chat_client.stream_chat", new=unreachable):
            status, first = self.request("POST", "/api/tasks", {"prompt": PROMPT, "mode": "run"})
        self.assertEqual(status, 202, first)

        async def answering(config, messages):
            yield "这台电脑正常。"
            yield "[[CONFIDENCE: high]]"

        with patch("runner.chat_client.stream_chat", new=answering):
            status, second = self.request(
                "POST", "/api/tasks",
                {"prompt": "再说明一次", "mode": "run", "session_id": first["session_id"]},
            )
        self.assertEqual(status, 202, second)
        self.assertEqual(second["session_id"], first["session_id"])

        _, detail = self.request("GET", f"/api/sessions/{quote(first['session_id'], safe='')}")
        messages = detail["messages"]
        self.assertEqual(
            [m["role"] for m in messages], ["user", "assistant", "user", "assistant"],
        )
        self.assertEqual(messages[0]["content"], PROMPT)
        self.assertIn("连不上模型服务", messages[1]["content"])
        self.assertEqual(messages[2]["content"], "再说明一次")
        self.assertIn("正常", messages[3]["content"])
        # One stored question per turn: no duplicates from the pre-persist.
        self.assertEqual(sum(1 for m in messages if m["role"] == "user"), 2)

    # -- the port contract --------------------------------------------------

    def test_port_records_the_request_and_the_failure(self) -> None:
        port = WebOutbound(self.settings, "web:web-console:port-failure", prompt=PROMPT)
        msg = InboundMessage(
            channel="web", operator_id="web-console", chat_id="port-failure",
            message_id=None, text=PROMPT,
        )
        # Storing the request twice (accepted, then failed) keeps one row.
        port.persist_request(msg)
        port.note_provider_failure(msg, "连不上模型服务，请检查「设置 → 模型设置」里的接口地址，或稍后再试。")

        from conveyor.core.transcript_store import get_transcript_store
        detail = get_transcript_store(self.settings).get_session("web:web-console:port-failure")
        self.assertEqual([m["role"] for m in detail["messages"]], ["user", "assistant"])
        self.assertEqual(detail["messages"][0]["content"], PROMPT)
        self.assertIn("连不上模型服务", detail["messages"][1]["content"])

        # One failure sentence is all the owner needs: the turn is over, so a
        # later answer in the same turn is not written and the question is never
        # stored twice.
        asyncio.run(port.reply(msg, "这台电脑正常。"))
        detail = get_transcript_store(self.settings).get_session("web:web-console:port-failure")
        self.assertEqual([m["role"] for m in detail["messages"]], ["user", "assistant"])
        self.assertEqual(sum(1 for m in detail["messages"] if m["role"] == "user"), 1)

    def test_chat_tier_failure_tells_a_port_that_owns_its_transcript(self) -> None:
        from handlers.chat import _chat_tier_failed

        port = SimpleNamespace(
            handles_transcript_directly=True,
            note_provider_failure=Mock(),
            edit_progress=Mock(),
        )
        outcome, checked = asyncio.run(_chat_tier_failed(
            InboundMessage(channel="web", operator_id="web-console", chat_id="c",
                           message_id=None, text=PROMPT),
            port, "placeholder", ChatError("Cannot connect to host qa-nonexistent.invalid"),
        ))
        self.assertEqual(outcome, "error")
        self.assertIn("连不上模型服务", checked.body)
        port.note_provider_failure.assert_called_once()
        self.assertIn("连不上模型服务", port.note_provider_failure.call_args.args[1])
        port.edit_progress.assert_not_called()


if __name__ == "__main__":
    unittest.main()
