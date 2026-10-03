"""tests/test_subagents.py — unit and integration tests for parallel read-only subagents.

Tests:
1. flag off -> tool not in schemas, execution refused
2. schema/arg validation (too many tasks, empty prompt, non-object, oversized fields)
3. subagents get only READ tools and never agents.parallel (inspect tools passed to complete_chat)
4. a subagent requesting a WRITE tool gets "not allowed" and nothing executes / no pending confirmation created
5. recursion guard (depth contextvar prevents recursion at schema and execution levels)
6. parallelism actually happens (total time ~ max not sum, and MAX_PARALLEL respected via concurrency counter)
7. timeout of one subagent while others succeed
8. exception in one subagent (only exception class name shown, secrets never leaked)
9. output caps + truncation marker
10. redaction of a fake secret in a subagent answer / tool result
11. audit events written (both overall event and per-subagent event in tools.log)
12. SSE subagent events emitted by WebChatPort and not persisted in transcript
13. features.subagents in WebControl.system_status()
14. end-to-end through run_tool_loop with a parent model that calls agents.parallel then answers
"""
from __future__ import annotations

import asyncio
import json
import queue
import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, patch

from channel.types import InboundMessage, OutboundPort
from config import Settings, is_subagents_enabled
from handlers.chat_tools import (
    build_tool_schemas,
    func_to_tool_name,
    run_tool_loop,
    tool_to_func_name,
)
from handlers.subagents import (
    PARALLEL_TOOL_NAME,
    _SUBAGENT_DEPTH,
    execute_parallel_subagents,
    get_subagent_semaphore,
    get_subagent_tool_spec,
    is_in_subagent,
)
from handlers.tools.confirm import get_pending_for_context
from handlers.tools.registry import DangerLevel, get_tool
from handlers.tools.runner import run_tool
from runner.chat_client import ChatConfig
from web_chat import WebChatPort


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


def _make_msg(text: str = "compare projects") -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        operator_id="test-op",
        chat_id="chat-subagents-test",
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
        "subagents_enabled": True,
        "subagents_max_tasks": 4,
        "subagents_max_parallel": 3,
        "subagents_timeout_seconds": 90,
        "subagents_max_steps": 3,
        "subagents_max_output_chars": 2500,
    }
    defaults.update(overrides)
    return Settings(**defaults)


class TestSubagentsSuite(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.settings = _make_settings(self.tmp_path)
        self.msg = _make_msg()
        self.port = FakeOutboundPort()
        self.cfg = ChatConfig(
            base_url="http://127.0.0.1:9999/v1",
            api_key="fake-secret-key-12345",
            model="test-chat-model",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_flag_off_tool_not_in_schemas_and_execution_refused(self) -> None:
        off_settings = _make_settings(self.tmp_path, subagents_enabled=False)
        self.assertFalse(is_subagents_enabled(off_settings))

        # 1. Not in schemas
        schemas = build_tool_schemas(off_settings)
        tool_names = [func_to_tool_name(s["function"]["name"]) for s in schemas]
        self.assertNotIn("agents.parallel", tool_names)

        # 2. Spec is None
        spec = get_subagent_tool_spec(off_settings)
        self.assertIsNone(spec)

        # 3. Execution via run_tool refused
        arg = json.dumps({"tasks": [{"title": "task1", "prompt": "check status"}]})
        result = await run_tool(off_settings, "agents.parallel", arg)
        self.assertIn("agents.parallel is disabled", result)

    async def test_schema_and_argument_validation(self) -> None:
        # Invalid: not a JSON dict
        res = await execute_parallel_subagents(self.settings, "not-json", config=self.cfg)
        self.assertIn("invalid arguments", res)

        # Invalid: empty string
        res = await execute_parallel_subagents(self.settings, "", config=self.cfg)
        self.assertIn("invalid arguments", res)

        # Invalid: tasks not a list
        res = await execute_parallel_subagents(self.settings, json.dumps({"tasks": "bad"}), config=self.cfg)
        self.assertIn("invalid arguments", res)

        # Invalid: empty tasks
        res = await execute_parallel_subagents(self.settings, json.dumps({"tasks": []}), config=self.cfg)
        self.assertIn("invalid arguments: 'tasks' must be a non-empty list", res)

        # Invalid: too many tasks (> max_tasks 4)
        too_many = [{"title": f"t{i}", "prompt": f"p{i}"} for i in range(5)]
        res = await execute_parallel_subagents(self.settings, json.dumps({"tasks": too_many}), config=self.cfg)
        self.assertIn("too many tasks (5). Maximum allowed is 4", res)

        # Invalid: missing title
        res = await execute_parallel_subagents(
            self.settings,
            json.dumps({"tasks": [{"prompt": "foo"}]}),
            config=self.cfg,
        )
        self.assertIn("task 1 'title' must be a non-empty string", res)

        # Invalid: oversized title (>60 chars)
        res = await execute_parallel_subagents(
            self.settings,
            json.dumps({"tasks": [{"title": "x" * 65, "prompt": "foo"}]}),
            config=self.cfg,
        )
        self.assertIn("task 1 'title' exceeds 60 characters", res)

        # Invalid: missing prompt
        res = await execute_parallel_subagents(
            self.settings,
            json.dumps({"tasks": [{"title": "t1", "prompt": "   "}]}),
            config=self.cfg,
        )
        self.assertIn("task 1 'prompt' must be a non-empty string", res)

        # Invalid: oversized prompt (>2000 chars)
        res = await execute_parallel_subagents(
            self.settings,
            json.dumps({"tasks": [{"title": "t1", "prompt": "y" * 2005}]}),
            config=self.cfg,
        )
        self.assertIn("task 1 'prompt' exceeds 2000 characters", res)

        # Invalid: oversized context (>2000 chars)
        res = await execute_parallel_subagents(
            self.settings,
            json.dumps({
                "tasks": [{"title": "t1", "prompt": "p1"}],
                "context": "c" * 2005,
            }),
            config=self.cfg,
        )
        self.assertIn("'context' exceeds 2000 characters", res)

    async def test_subagent_tool_set_read_only_and_no_agents_parallel(self) -> None:
        captured_tools: list[list[dict]] = []

        async def fake_complete(config, messages, tools=None):
            if tools is not None:
                captured_tools.append(tools)
            return {"role": "assistant", "content": "Subagent findings: all good"}

        arg = json.dumps({"tasks": [{"title": "Inspect system", "prompt": "Check load"}]})
        with patch("handlers.subagents.complete_chat", side_effect=fake_complete):
            res = await execute_parallel_subagents(self.settings, arg, config=self.cfg)

        self.assertIn("### [1] Inspect system — ok", res)
        self.assertTrue(len(captured_tools) > 0)
        tools = captured_tools[0]
        tool_names = [func_to_tool_name(t["function"]["name"]) for t in tools]

        # Verify: agents.parallel is NOT in the subagent tools
        self.assertNotIn("agents.parallel", tool_names)

        # Verify: all tools in subagent tool set are READ danger
        for tname in tool_names:
            spec = get_tool(tname)
            if spec is not None:
                self.assertEqual(spec.danger, DangerLevel.READ, f"{tname} should be READ only")

    async def test_subagent_requesting_write_tool_refused_no_confirmation(self) -> None:
        call_count = 0

        async def fake_complete(config, messages, tools=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Subagent tries to call a write tool (e.g. service_restart)
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "tc-write-1",
                        "type": "function",
                        "function": {
                            "name": tool_to_func_name("service_restart"),
                            "arguments": json.dumps({"arg": "nginx"}),
                        },
                    }],
                }
            # Second turn: subagent sees refusal and answers
            tool_msg = messages[-1]
            self.assertEqual(tool_msg.get("role"), "tool")
            self.assertEqual(tool_msg.get("content"), "not allowed for subagents")
            return {"role": "assistant", "content": "I cannot restart services as I am read-only."}

        arg = json.dumps({"tasks": [{"title": "Restart service", "prompt": "Restart nginx"}]})
        with patch("handlers.subagents.complete_chat", side_effect=fake_complete):
            res = await execute_parallel_subagents(self.settings, arg, config=self.cfg, msg=self.msg)

        self.assertIn("I cannot restart services as I am read-only", res)
        # Verify no pending tool confirmation was created
        pending = get_pending_for_context("test-op", "telegram", "chat-subagents-test")
        self.assertIsNone(pending)

    async def test_recursion_guard_blocks_recursive_call(self) -> None:
        self.assertFalse(is_in_subagent())

        # Simulate being inside a subagent
        token = _SUBAGENT_DEPTH.set(1)
        try:
            self.assertTrue(is_in_subagent())

            # 1. Spec is None inside a subagent
            spec = get_subagent_tool_spec(self.settings)
            self.assertIsNone(spec)

            # 2. Not in schemas
            schemas = build_tool_schemas(self.settings)
            tool_names = [func_to_tool_name(s["function"]["name"]) for s in schemas]
            self.assertNotIn("agents.parallel", tool_names)

            # 3. Execution directly refused
            arg = json.dumps({"tasks": [{"title": "rec", "prompt": "recursive"}]})
            res = await execute_parallel_subagents(self.settings, arg, config=self.cfg)
            self.assertIn("cannot be called recursively", res)
        finally:
            _SUBAGENT_DEPTH.reset(token)

        self.assertFalse(is_in_subagent())

    async def test_parallelism_and_max_parallel_concurrency_limit(self) -> None:
        active_count = 0
        max_seen = 0

        async def fake_complete(config, messages, tools=None):
            nonlocal active_count, max_seen
            active_count += 1
            max_seen = max(max_seen, active_count)
            await asyncio.sleep(0.08)
            active_count -= 1
            return {"role": "assistant", "content": "done"}

        # 3 tasks with max_parallel = 3
        settings = _make_settings(self.tmp_path, subagents_max_parallel=3)
        arg = json.dumps({
            "tasks": [
                {"title": "Task 1", "prompt": "P1"},
                {"title": "Task 2", "prompt": "P2"},
                {"title": "Task 3", "prompt": "P3"},
            ]
        })

        t0 = time.monotonic()
        with patch("handlers.subagents.complete_chat", side_effect=fake_complete):
            res = await execute_parallel_subagents(settings, arg, config=self.cfg)
        elapsed = time.monotonic() - t0

        self.assertEqual(max_seen, 3)
        # Since they ran in parallel, elapsed should be close to 0.08s, well under 3 * 0.08 = 0.24s
        self.assertLess(elapsed, 0.20)
        self.assertIn("Task 1 — ok", res)
        self.assertIn("Task 2 — ok", res)
        self.assertIn("Task 3 — ok", res)

        # Now test with max_parallel = 2 on 4 tasks -> max concurrency should never exceed 2
        active_count = 0
        max_seen = 0
        settings2 = _make_settings(self.tmp_path, subagents_max_parallel=2)
        arg2 = json.dumps({
            "tasks": [
                {"title": "Task A", "prompt": "PA"},
                {"title": "Task B", "prompt": "PB"},
                {"title": "Task C", "prompt": "PC"},
                {"title": "Task D", "prompt": "PD"},
            ]
        })
        with patch("handlers.subagents.complete_chat", side_effect=fake_complete):
            await execute_parallel_subagents(settings2, arg2, config=self.cfg)
        self.assertEqual(max_seen, 2)

    async def test_timeout_of_one_subagent_while_others_succeed(self) -> None:
        async def fake_complete(config, messages, tools=None):
            # Inspect prompt to see which task this is
            prompt = str(messages[-1].get("content") or "")
            if "slow" in prompt:
                await asyncio.sleep(0.3)
                return {"role": "assistant", "content": "slow finished"}
            await asyncio.sleep(0.01)
            return {"role": "assistant", "content": "fast finished"}

        settings = _make_settings(self.tmp_path, subagents_timeout_seconds=10)  # clamped, will override in test
        arg = json.dumps({
            "tasks": [
                {"title": "Fast Task 1", "prompt": "fast one"},
                {"title": "Slow Task 2", "prompt": "slow one"},
                {"title": "Fast Task 3", "prompt": "fast two"},
            ]
        })

        real_wait_for = asyncio.wait_for

        async def custom_wait_for(coro, timeout):
            # Use small 0.05s timeout: 0.01s tasks succeed, 0.3s task times out
            return await real_wait_for(coro, timeout=0.05)

        with patch("handlers.subagents.complete_chat", side_effect=fake_complete):
            with patch("handlers.subagents.asyncio.wait_for", side_effect=custom_wait_for):
                res = await execute_parallel_subagents(settings, arg, config=self.cfg)

        self.assertIn("### [1] Fast Task 1 — ok", res)
        self.assertIn("fast finished", res)
        self.assertIn("### [2] Slow Task 2 — timeout", res)
        self.assertIn("Subagent error: TimeoutError", res)
        self.assertIn("### [3] Fast Task 3 — ok", res)

    async def test_exception_in_one_subagent_does_not_leak_secrets(self) -> None:
        async def fake_complete(config, messages, tools=None):
            prompt = str(messages[-1].get("content") or "")
            if "failing" in prompt:
                raise RuntimeError("database crash with secret_key_super_secret_123")
            return {"role": "assistant", "content": "success result"}

        arg = json.dumps({
            "tasks": [
                {"title": "Failing Task", "prompt": "failing query"},
                {"title": "Passing Task", "prompt": "passing query"},
            ]
        })

        with patch("handlers.subagents.complete_chat", side_effect=fake_complete):
            res = await execute_parallel_subagents(self.settings, arg, config=self.cfg)

        self.assertIn("### [1] Failing Task — error", res)
        self.assertIn("Subagent error: RuntimeError", res)
        # Assert the secret details were not leaked in the error output
        self.assertNotIn("secret_key_super_secret_123", res)
        self.assertIn("### [2] Passing Task — ok", res)
        self.assertIn("success result", res)

    async def test_output_caps_and_truncation_marker(self) -> None:
        long_text = "A" * 800
        settings = _make_settings(self.tmp_path, subagents_max_output_chars=500)

        async def fake_complete(config, messages, tools=None):
            return {"role": "assistant", "content": long_text}

        arg = json.dumps({"tasks": [{"title": "Long output", "prompt": "give me lots of text"}]})

        with patch("handlers.subagents.complete_chat", side_effect=fake_complete):
            res = await execute_parallel_subagents(settings, arg, config=self.cfg)

        self.assertIn("### [1] Long output — ok", res)
        self.assertIn("[... truncated ...]", res)
        # Capped to 500 chars + marker
        output_part = res.split("tools: none)\n")[1]
        self.assertTrue(output_part.startswith("A" * 500))
        self.assertTrue(output_part.endswith("[... truncated ...]"))

    async def test_redaction_of_fake_secret_in_subagent_output(self) -> None:
        fake_secret = "ghp_FAKESECRETTOKEN1234567890123456"

        async def fake_complete(config, messages, tools=None):
            return {"role": "assistant", "content": f"Found sensitive token: {fake_secret}"}

        arg = json.dumps({"tasks": [{"title": "Check config", "prompt": "Find tokens"}]})

        with patch("handlers.subagents.complete_chat", side_effect=fake_complete):
            raw_res = await execute_parallel_subagents(self.settings, arg, config=self.cfg)

        from redaction import redact_text
        redacted = redact_text(raw_res)
        self.assertNotIn(fake_secret, redacted)
        self.assertIn("[REDACTED]", redacted)

    async def test_audit_events_written_per_subagent(self) -> None:
        async def fake_complete(config, messages, tools=None):
            return {"role": "assistant", "content": "Done"}

        arg = json.dumps({
            "tasks": [
                {"title": "Audit Task 1", "prompt": "Check 1"},
                {"title": "Audit Task 2", "prompt": "Check 2"},
            ]
        })

        with patch("handlers.subagents.complete_chat", side_effect=fake_complete):
            await execute_parallel_subagents(self.settings, arg, config=self.cfg, msg=self.msg)

        audit_file = self.tmp_path / "memory" / "audit" / "tools.log"
        self.assertTrue(audit_file.exists())
        lines = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines() if line.strip()]

        # Expect 1 overall executed event + 2 subagent events
        subagent_events = [l for l in lines if l.get("action") == "subagent"]
        self.assertEqual(len(subagent_events), 2)
        self.assertEqual(subagent_events[0]["tool_name"], "agents.parallel")
        self.assertEqual(subagent_events[0]["arg"], "Audit Task 1")
        self.assertIn("status=ok", subagent_events[0]["result_preview"])
        self.assertEqual(subagent_events[1]["arg"], "Audit Task 2")

        overall_events = [l for l in lines if l.get("action") == "executed" and l.get("tool_name") == "agents.parallel"]
        self.assertEqual(len(overall_events), 1)

    async def test_sse_subagent_events_emitted_and_not_in_transcript(self) -> None:
        event_q: queue.Queue = queue.Queue()
        session_id = "test-session-sse"
        port = WebChatPort(event_q, self.settings, session_id, prompt="run subagents")

        async def fake_complete(config, messages, tools=None):
            return {"role": "assistant", "content": "SSE done"}

        arg = json.dumps({"tasks": [{"title": "Web subagent", "prompt": "do research"}]})

        with patch("handlers.subagents.complete_chat", side_effect=fake_complete):
            await execute_parallel_subagents(self.settings, arg, config=self.cfg, port=port, msg=self.msg)

        # Collect events from queue
        events = []
        while not event_q.empty():
            events.append(event_q.get_nowait())

        subagent_events = [data for name, data in events if name == "subagent"]
        self.assertTrue(len(subagent_events) >= 2)
        # queued -> running -> ok
        self.assertEqual([e["status"] for e in subagent_events], ["queued", "running", "ok"])
        self.assertEqual(subagent_events[0]["title"], "Web subagent")
        # Second event is ok
        self.assertEqual(subagent_events[-1]["status"], "ok")
        self.assertEqual(subagent_events[-1]["title"], "Web subagent")

        # Verify no transcript entry was added by subagent progress
        from transcript_store import get_transcript_store
        transcript = get_transcript_store(self.settings).get_session(session_id)
        # Should be None since WebChatPort only persists final turns, not subagent progress
        self.assertIsNone(transcript)

    async def test_queue_wait_does_not_count_against_timeout(self) -> None:
        # max_parallel=1, three tasks of 0.05s each with a 0.08s timeout: each
        # run fits, though the last one waits ~0.1s for its slot.
        async def fake_complete(config, messages, tools=None):
            await asyncio.sleep(0.05)
            return {"role": "assistant", "content": "done"}

        settings = _make_settings(self.tmp_path, subagents_max_parallel=1)
        arg = json.dumps({"tasks": [{"title": f"T{i}", "prompt": f"p{i}"} for i in range(3)]})
        real_wait_for = asyncio.wait_for

        async def custom_wait_for(coro, timeout):
            return await real_wait_for(coro, timeout=0.08)

        with patch("handlers.subagents.complete_chat", side_effect=fake_complete):
            with patch("handlers.subagents.asyncio.wait_for", side_effect=custom_wait_for):
                res = await execute_parallel_subagents(settings, arg, config=self.cfg)
        self.assertEqual(res.count(" — ok "), 3, res)

    async def test_web_port_gets_no_plain_progress_edits(self) -> None:
        event_q: queue.Queue = queue.Queue()
        port = WebChatPort(event_q, self.settings, "s-progress", prompt="x")

        async def fake_complete(config, messages, tools=None):
            return {"role": "assistant", "content": "ok"}

        arg = json.dumps({"tasks": [{"title": "A", "prompt": "a"}, {"title": "B", "prompt": "b"}]})
        with patch("handlers.subagents.complete_chat", side_effect=fake_complete):
            await execute_parallel_subagents(
                self.settings, arg, config=self.cfg, port=port, msg=self.msg, placeholder="ph"
            )
        names = []
        while not event_q.empty():
            names.append(event_q.get_nowait()[0])
        self.assertNotIn("message", names)
        self.assertIn("subagent", names)

    def test_features_subagents_system_status(self) -> None:
        from unittest.mock import MagicMock
        from web_control import WebControl

        # Flag ON (both subagents and chat_tools)
        control = WebControl(self.settings, runner=MagicMock(), queue=MagicMock())
        status = control.system_status()
        self.assertTrue(status["features"]["subagents"])

        # Flag OFF: subagents_enabled = False
        s_off = _make_settings(self.tmp_path, subagents_enabled=False)
        control_off = WebControl(s_off, runner=MagicMock(), queue=MagicMock())
        self.assertFalse(control_off.system_status()["features"]["subagents"])

        # Flag OFF: chat_tools_enabled = False
        s_no_chat = _make_settings(self.tmp_path, subagents_enabled=True, chat_tools_enabled=False)
        control_no_chat = WebControl(s_no_chat, runner=MagicMock(), queue=MagicMock())
        self.assertFalse(control_no_chat.system_status()["features"]["subagents"])

    async def test_end_to_end_through_run_tool_loop(self) -> None:
        turn = 0

        async def fake_complete_top(config, messages, tools=None):
            nonlocal turn
            turn += 1
            if turn == 1:
                # Top model decides to call agents__parallel
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "tc-parallel-1",
                        "type": "function",
                        "function": {
                            "name": "agents__parallel",
                            "arguments": {
                                "tasks": [
                                    {"title": "Check A", "prompt": "Check load"},
                                    {"title": "Check B", "prompt": "Check ps"},
                                ],
                                "context": "System health inspection",
                            },
                        },
                    }],
                }
            elif turn == 2:
                # Top model receives the synthesized result
                tool_msg = messages[-1]
                self.assertEqual(tool_msg["role"], "tool")
                content = tool_msg["content"]
                self.assertIn('<tool-result name="agents.parallel" untrusted="true">', content)
                self.assertIn("Check A — ok", content)
                self.assertIn("Check B — ok", content)
                return {"role": "assistant", "content": "Both A and B systems are performing normally."}
            else:
                return {"role": "assistant", "content": "Subagent response"}

        # When subagents run, their complete_chat will be called
        subagent_turn = 0
        async def fake_complete_router(config, messages, tools=None):
            if is_in_subagent():
                nonlocal subagent_turn
                subagent_turn += 1
                return {"role": "assistant", "content": f"Subagent findings for turn {subagent_turn}"}
            return await fake_complete_top(config, messages, tools)

        messages = [{"role": "user", "content": "Check systems A and B"}]
        with patch("handlers.chat_tools.complete_chat", side_effect=fake_complete_router):
            with patch("handlers.subagents.complete_chat", side_effect=fake_complete_router):
                result = await run_tool_loop(self.msg, self.port, self.settings, messages, self.cfg)

        self.assertEqual(result.text, "Both A and B systems are performing normally.")
        self.assertEqual(result.tools_called, ["agents.parallel"])
        self.assertFalse(result.confirmation_requested)
