"""tests/test_mcp.py — Unit and integration tests for MCP connectors in chat tier.

Covers:
1. Config parsing & validation (rules, limits, secrets, MCP_ prefix, state file override).
2. Child process environment isolation (PATH, HOME mode 0700, no Conveyor secrets leaked).
3. Transports: stdio and streamable HTTP (SSE, pagination, ping handling, timeouts, caps, redaction).
4. Chat tool integration (schemas, danger levels, auto-run READ, confirm WRITE, runtime re-validation).
5. Web console HTTP routes (GET /api/mcp/servers, POST refresh, PUT enable/disable, 401, 404, 409).
"""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from channel.types import InboundMessage
from config import Settings
from handlers.chat_tools import (
    REVERSE_TOOL_MAP,
    build_tool_schemas,
    func_to_tool_name,
    run_tool_loop,
    tool_to_func_name,
)
from handlers.tools.confirm import clear_all_pending, get_pending_for_context
from handlers.tools.registry import DangerLevel
from handlers.tools.runner import execute_confirmed, run_tool
from mcp_client.config import (
    clean_target,
    load_disabled_servers,
    load_mcp_servers,
    save_disabled_servers,
    validate_server,
)
from mcp_client.manager import MCPManager, get_mcp_manager
from mcp_client.types import MCPToolSpec, ServerConfig
from runner.chat_client import ChatConfig
from tests.fixtures.mcp_test_server import create_http_server
from web_console import WebConsoleHandler, WebConsoleServer


def validate_server_config(name: str, raw: dict) -> tuple[ServerConfig, str | None]:
    srv = validate_server(name, raw, index=0)
    return srv, srv.validation_error


def load_mcp_config(settings: Settings) -> dict[str, ServerConfig]:
    servers, _ = load_mcp_servers(settings.mcp_config_path)
    return servers


def save_state(settings: Settings, data: dict) -> None:
    disabled = set(data.get("disabled", []))
    save_disabled_servers(settings.codex_memory_root, disabled)


def load_state(settings: Settings) -> dict:
    disabled = load_disabled_servers(settings.codex_memory_root)
    return {"disabled": sorted(disabled)}


FIXTURE_SERVER = str(Path(__file__).resolve().parent / "fixtures" / "mcp_test_server.py")


def _settings(tmp: Path, **overrides) -> Settings:
    mem = tmp / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    defaults = {
        "telegram_bot_token": "fake-tg-token",
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
        "mcp_enabled": True,
        "mcp_config_path": mem / "mcp_servers.json",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _msg(text: str = "hello", chat_id: str = "chat-1", operator_id: str = "op-1") -> InboundMessage:
    return InboundMessage(
        channel="web",
        operator_id=operator_id,
        chat_id=chat_id,
        message_id="m1",
        text=text,
        chat_type="p2p",
    )


class FakeOutboundPort:
    def __init__(self) -> None:
        self.replies: list[tuple[InboundMessage, str]] = []
        self.buttons: list[tuple[InboundMessage, str, list[list[dict]]]] = []
        self.supports_inline_buttons: bool = True
        self.supports_attachments: bool = False

    async def reply(self, msg: InboundMessage, text: str) -> str:
        self.replies.append((msg, text))
        return f"ph-{len(self.replies)}"

    async def send_new(self, msg: InboundMessage, text: str) -> str:
        self.replies.append((msg, text))
        return f"new-{len(self.replies)}"

    async def edit_progress(self, msg: InboundMessage, placeholder_id: str, text: str) -> bool:
        return True

    async def reply_with_buttons(
        self, msg: InboundMessage, text: str, buttons: list[list[dict]]
    ) -> str:
        self.buttons.append((msg, text, buttons))
        return f"btn-{len(self.buttons)}"


class TestMCPConfigValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_flag_defaults_off(self) -> None:
        field = Settings.__dataclass_fields__["mcp_enabled"]
        self.assertFalse(field.default)
        root = Path(__file__).resolve().parents[1]
        self.assertIn('CONVEYOR_MCP_ENABLED", "false"', (root / "config.py").read_text(encoding="utf-8"))
        self.assertIn("CONVEYOR_MCP_ENABLED=false", (root / ".env.example").read_text(encoding="utf-8"))

    def test_missing_config_returns_empty(self) -> None:
        cfg = load_mcp_config(self.settings)
        self.assertEqual(cfg, {})

    def test_invalid_json_returns_empty_and_does_not_crash(self) -> None:
        path = self.settings.mcp_config_path
        path.write_text("{invalid json", encoding="utf-8")
        cfg = load_mcp_config(self.settings)
        self.assertEqual(cfg, {})

    def test_valid_stdio_and_http_configs(self) -> None:
        config_data = {
            "servers": {
                "notes": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [FIXTURE_SERVER, "--stdio"],
                    "allow_tools": ["echo", "add"],
                    "read_only_tools": ["echo", "add"],
                    "trust_read_only_hint": True,
                    "timeout_seconds": 20,
                    "max_output_chars": 3000,
                    "enabled": True,
                },
                "docs": {
                    "transport": "http",
                    "url": "http://127.0.0.1:9000/mcp",
                    "allow_tools": ["*"],
                    "read_only_tools": [],
                },
            }
        }
        self.settings.mcp_config_path.write_text(json.dumps(config_data), encoding="utf-8")
        loaded = load_mcp_config(self.settings)
        self.assertIn("notes", loaded)
        self.assertIn("docs", loaded)
        self.assertIsNone(loaded["notes"].validation_error)
        self.assertEqual(loaded["notes"].timeout_seconds, 20)
        self.assertEqual(loaded["notes"].max_output_chars, 3000)
        self.assertEqual(loaded["docs"].allow_tools, ["*"])

    def test_server_name_validation(self) -> None:
        # Valid names
        for valid in ("a", "notes", "notes-1", "my_srv_2"):
            srv, err = validate_server_config(
                valid,
                {"transport": "http", "url": "http://localhost/mcp", "allow_tools": ["*"]},
            )
            self.assertIsNone(err, f"Expected {valid} to be valid")
            self.assertIsNotNone(srv)
            self.assertIsNone(srv.validation_error)

        # Invalid names
        for invalid in ("", "Upper", "-start", "_start", "with.dot", "toolong" * 10):
            srv, err = validate_server_config(
                invalid,
                {"transport": "http", "url": "http://localhost/mcp", "allow_tools": ["*"]},
            )
            self.assertIsNotNone(err)
            self.assertIsNotNone(srv.validation_error)

    def test_stdio_requires_absolute_command(self) -> None:
        srv, err = validate_server_config(
            "relcmd",
            {"transport": "stdio", "command": "python3", "allow_tools": ["*"]},
        )
        self.assertIsNotNone(srv.validation_error)
        self.assertIn("absolute", (err or "").lower())

    def test_http_requires_valid_url(self) -> None:
        srv, err = validate_server_config(
            "badurl",
            {"transport": "http", "url": "ftp://example.com", "allow_tools": ["*"]},
        )
        self.assertIsNotNone(srv.validation_error)
        self.assertIn("http://", err or "")

    def test_allow_tools_and_read_only_tools_rules(self) -> None:
        # allow_tools required list
        srv, err = validate_server_config(
            "srv1",
            {"transport": "http", "url": "http://localhost/mcp"},
        )
        self.assertIsNotNone(srv.validation_error)
        self.assertIn("allow_tools", (err or "").lower())

        # read_only_tools cannot contain "*"
        srv, err = validate_server_config(
            "srv2",
            {"transport": "http", "url": "http://localhost/mcp", "allow_tools": ["*"], "read_only_tools": ["*"]},
        )
        self.assertIsNotNone(srv.validation_error)
        self.assertIn("cannot contain '*'", err or "")

    def test_timeout_and_max_output_bounds(self) -> None:
        # timeout bounds 1-120
        srv, _ = validate_server_config(
            "t1",
            {"transport": "http", "url": "http://localhost/mcp", "allow_tools": ["*"], "timeout_seconds": 0},
        )
        self.assertIsNotNone(srv.validation_error)
        srv, _ = validate_server_config(
            "t2",
            {"transport": "http", "url": "http://localhost/mcp", "allow_tools": ["*"], "timeout_seconds": 121},
        )
        self.assertIsNotNone(srv.validation_error)

        # max_output_chars bounds 200-20000
        srv, _ = validate_server_config(
            "m1",
            {"transport": "http", "url": "http://localhost/mcp", "allow_tools": ["*"], "max_output_chars": 199},
        )
        self.assertIsNotNone(srv.validation_error)
        srv, _ = validate_server_config(
            "m2",
            {"transport": "http", "url": "http://localhost/mcp", "allow_tools": ["*"], "max_output_chars": 20001},
        )
        self.assertIsNotNone(srv.validation_error)

    def test_secrets_isolation_env_from_prefix(self) -> None:
        # env_from referencing non-MCP_ prefix must error
        srv, err = validate_server_config(
            "badenv",
            {
                "transport": "stdio",
                "command": sys.executable,
                "allow_tools": ["*"],
                "env_from": {"SECRET": "TELEGRAM_BOT_TOKEN"},
            },
        )
        self.assertIsNotNone(srv.validation_error)
        self.assertIn("MCP_", err or "")

        # headers_from referencing non-MCP_ prefix must error
        srv, err = validate_server_config(
            "badhead",
            {
                "transport": "http",
                "url": "http://localhost/mcp",
                "allow_tools": ["*"],
                "headers_from": {"Authorization": "CONVEYOR_SECRET"},
            },
        )
        self.assertIsNotNone(srv.validation_error)
        self.assertIn("MCP_", err or "")

        # Valid env_from with MCP_ prefix
        srv, err = validate_server_config(
            "goodenv",
            {
                "transport": "stdio",
                "command": sys.executable,
                "allow_tools": ["*"],
                "env_from": {"SECRET": "MCP_MY_TOKEN"},
            },
        )
        self.assertIsNone(err)
        self.assertIsNone(srv.validation_error)

    def test_max_20_servers_limit(self) -> None:
        servers = {}
        for i in range(25):
            servers[f"srv{i}"] = {
                "transport": "http",
                "url": f"http://localhost:{9000 + i}/mcp",
                "allow_tools": ["*"],
            }
        self.settings.mcp_config_path.write_text(json.dumps({"servers": servers}), encoding="utf-8")
        loaded = load_mcp_config(self.settings)
        # First 20 have validation_error None, 21st onwards have validation_error set
        valid_count = sum(1 for s in loaded.values() if s.validation_error is None)
        self.assertEqual(valid_count, 20)
        self.assertEqual(len(loaded), 25)

    def test_state_file_and_permissions(self) -> None:
        state_path = self.settings.codex_memory_root / "mcp_state.json"
        save_state(self.settings, {"disabled": ["srv1", "srv2"]})
        self.assertTrue(state_path.exists())
        # Check permissions 0600
        mode = stat.S_IMODE(state_path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        loaded_state = load_state(self.settings)
        self.assertEqual(loaded_state["disabled"], ["srv1", "srv2"])

    def test_clean_target_strips_secrets_and_paths(self) -> None:
        stdio_srv, _ = validate_server_config(
            "s1",
            {"transport": "stdio", "command": "/usr/local/bin/python3", "args": ["a", "b"], "allow_tools": ["*"]},
        )
        self.assertEqual(clean_target(stdio_srv), "python3 (2 args)")

        http_srv, _ = validate_server_config(
            "s2",
            {"transport": "http", "url": "http://user:secret@example.com:8080/mcp?token=xyz", "allow_tools": ["*"]},
        )
        self.assertEqual(clean_target(http_srv), "http://example.com:8080/mcp")


class TestMCPEarlyChildEnvAndIsolation(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_stdio_child_environment_isolation(self) -> None:
        # Set dummy host secrets in os.environ (restored afterwards, values included)
        fake_env = {"TELEGRAM_BOT_TOKEN": "tg-super-secret", "CONVEYOR_WEB_TOKEN": "web-super-secret",
                    "OPENAI_API_KEY": "openai-super-secret", "MCP_ALLOWED_TOKEN": "mcp-probe-token"}
        with patch.dict(os.environ, fake_env):
            config_data = {
                "servers": {
                    "probe": {
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [FIXTURE_SERVER, "--stdio"],
                        "env": {"CUSTOM_KEY": "custom_val"},
                        "env_from": {"PROBE_KEY": "MCP_ALLOWED_TOKEN"},
                        "allow_tools": ["*"],
                        "read_only_tools": ["env_probe"],
                    }
                }
            }
            self.settings.mcp_config_path.write_text(json.dumps(config_data), encoding="utf-8")
            mgr = MCPManager()
            await mgr.refresh_server(self.settings, "probe")

            # Call env_probe
            result = await mgr.call_tool(
                self.settings,
                "mcp.probe.env_probe",
                json.dumps({"vars": ["TELEGRAM_BOT_TOKEN", "CONVEYOR_WEB_TOKEN", "OPENAI_API_KEY", "CUSTOM_KEY", "PROBE_KEY", "HOME"]}),
            )
            data = json.loads(result)
            # Host secrets MUST NOT be present
            self.assertNotIn("TELEGRAM_BOT_TOKEN", data)
            self.assertNotIn("CONVEYOR_WEB_TOKEN", data)
            self.assertNotIn("OPENAI_API_KEY", data)
            # Literal env and MCP_ env_from MUST be present
            self.assertEqual(data.get("CUSTOM_KEY"), "custom_val")
            self.assertEqual(data.get("PROBE_KEY"), "mcp-probe-token")
            # Private HOME directory check
            expected_home = str(self.settings.codex_memory_root / "mcp_home" / "probe")
            self.assertEqual(data.get("HOME"), expected_home)
            home_path = Path(expected_home)
            self.assertTrue(home_path.exists())
            self.assertEqual(stat.S_IMODE(home_path.stat().st_mode), 0o700)


class TestMCPTransportsAndProtocols(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_stdio_list_call_ping_and_caps(self) -> None:
        config_data = {
            "servers": {
                "teststdio": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [FIXTURE_SERVER, "--stdio"],
                    "allow_tools": ["echo", "add", "big", "fail"],
                    "read_only_tools": ["echo", "add", "big", "fail"],
                    "timeout_seconds": 10,
                    "max_output_chars": 500,
                }
            }
        }
        self.settings.mcp_config_path.write_text(json.dumps(config_data), encoding="utf-8")
        mgr = MCPManager()

        # 1. tools/list via refresh_server
        item, status = await mgr.refresh_server(self.settings, "teststdio")
        self.assertEqual(status, 200)
        tool_names = [t["name"] for t in item["tools"]]
        self.assertIn("echo", tool_names)
        self.assertIn("add", tool_names)

        # 2. tools/call (note: fixture sends ping during tools/call to test server->client ping)
        add_result = await mgr.call_tool(self.settings, "mcp.teststdio.add", json.dumps({"a": 12, "b": 30}))
        self.assertEqual(add_result.strip(), "42")

        # 3. fail tool
        fail_result = await mgr.call_tool(self.settings, "mcp.teststdio.fail", "{}")
        self.assertTrue(fail_result.startswith("MCP tool error: ") or "fail" in fail_result)

        # 4. big tool (output capping)
        big_result = await mgr.call_tool(self.settings, "mcp.teststdio.big", json.dumps({"chars": 2000}))
        self.assertIn("[truncated 1500 chars]", big_result)
        self.assertLessEqual(len(big_result), 500 + 40)

    async def test_stdio_large_single_line_message(self) -> None:
        # A tools/call response far above asyncio's default 64 KiB readline limit must work.
        config_data = {"servers": {"bigstdio": {
            "transport": "stdio", "command": sys.executable, "args": [FIXTURE_SERVER, "--stdio"],
            "allow_tools": ["big"], "read_only_tools": ["big"], "timeout_seconds": 10, "max_output_chars": 20000,
        }}}
        self.settings.mcp_config_path.write_text(json.dumps(config_data), encoding="utf-8")
        mgr = MCPManager()
        res = await mgr.call_tool(self.settings, "mcp.bigstdio.big", json.dumps({"chars": 300000}))
        self.assertIn("[truncated 280000 chars]", res)
        self.assertNotIn("执行失败", res)

    async def test_stdio_timeout_terminates_child(self) -> None:
        config_data = {
            "servers": {
                "slowstdio": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [FIXTURE_SERVER, "--stdio"],
                    "allow_tools": ["slow"],
                    "read_only_tools": ["slow"],
                    "timeout_seconds": 1,
                }
            }
        }
        self.settings.mcp_config_path.write_text(json.dumps(config_data), encoding="utf-8")
        mgr = MCPManager()
        await mgr.refresh_server(self.settings, "slowstdio")

        res = await mgr.call_tool(self.settings, "mcp.slowstdio.slow", json.dumps({"seconds": 5}))
        self.assertIn("timed out", res.lower())

    async def test_output_redaction(self) -> None:
        config_data = {
            "servers": {
                "redactsrv": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [FIXTURE_SERVER, "--stdio"],
                    "allow_tools": ["echo"],
                    "read_only_tools": ["echo"],
                }
            }
        }
        self.settings.mcp_config_path.write_text(json.dumps(config_data), encoding="utf-8")
        mgr = MCPManager()
        await mgr.refresh_server(self.settings, "redactsrv")

        # Test token matching secret pattern
        result = await mgr.call_tool(self.settings, "mcp.redactsrv.echo", json.dumps({"text": "api_key: sk-123456789012345678901234567890"}))
        self.assertNotIn("sk-123456789012345678901234567890", result)
        self.assertIn("[REDACTED]", result)

    async def test_http_transport_json_and_sse_and_pagination(self) -> None:
        # Start in-process test HTTP server with pagination and SSE
        http_server = create_http_server(port=0, sse=True, paginate=True)
        thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        port = http_server.server_address[1]

        try:
            config_data = {
                "servers": {
                    "testhttp": {
                        "transport": "http",
                        "url": f"http://127.0.0.1:{port}/mcp",
                        "allow_tools": ["*"],
                        "read_only_tools": ["echo", "add"],
                        "timeout_seconds": 10,
                    }
                }
            }
            self.settings.mcp_config_path.write_text(json.dumps(config_data), encoding="utf-8")
            mgr = MCPManager()

            # 1. tools/list with pagination across 2 pages
            item, status = await mgr.refresh_server(self.settings, "testhttp")
            self.assertEqual(status, 200)
            tool_names = [t["name"] for t in item["tools"]]
            # Since paginate split at 3 tools, and ALL_TOOLS has 7 tools, all 7 should be received!
            self.assertGreaterEqual(len(tool_names), 7)
            self.assertIn("echo", tool_names)
            self.assertIn("add", tool_names)
            self.assertIn("slow", tool_names)

            # 2. tools/call over HTTP SSE
            res = await mgr.call_tool(self.settings, "mcp.testhttp.echo", json.dumps({"text": "hello from http"}))
            self.assertEqual(res.strip(), "hello from http")
        finally:
            http_server.shutdown()
            http_server.server_close()


class TestMCPChatToolIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self.tmp.name))
        self.port = FakeOutboundPort()
        self.msg = _msg("echo hello")
        self.cfg = ChatConfig(base_url="http://fake", api_key="fake-secret", model="m")
        clear_all_pending()

    def tearDown(self) -> None:
        clear_all_pending()
        self.tmp.cleanup()

    async def _setup_server_config(self, notes_file: str | None = None) -> MCPManager:
        args = [FIXTURE_SERVER, "--stdio"]
        if notes_file:
            args.extend(["--notes", notes_file])
        config_data = {
            "servers": {
                "notes": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": args,
                    "allow_tools": ["echo", "write_note"],
                    "read_only_tools": ["echo"],
                    "trust_read_only_hint": False,
                }
            }
        }
        self.settings.mcp_config_path.write_text(json.dumps(config_data), encoding="utf-8")
        mgr = MCPManager()
        # Preload cache
        await mgr.refresh_server(self.settings, "notes")
        return mgr

    async def test_schemas_and_reverse_map(self) -> None:
        mgr = await self._setup_server_config()
        with patch("mcp_client.get_mcp_manager", return_value=mgr):
            schemas = build_tool_schemas(self.settings)
            tool_funcs = [s["function"]["name"] for s in schemas]
            # mcp.notes.echo -> mcp__notes__echo
            expected_func = tool_to_func_name("mcp.notes.echo")
            self.assertIn(expected_func, tool_funcs)
            self.assertEqual(func_to_tool_name(expected_func), "mcp.notes.echo")

            echo_schema = next(s for s in schemas if s["function"]["name"] == expected_func)
            desc = echo_schema["function"]["description"]
            self.assertTrue(desc.startswith("[MCP notes]"))
            self.assertIn("[read]", desc)
            # inputSchema preserved
            props = echo_schema["function"]["parameters"]["properties"]
            self.assertIn("text", props)

    async def test_read_tool_auto_runs_in_chat_loop(self) -> None:
        mgr = await self._setup_server_config()
        func_name = tool_to_func_name("mcp.notes.echo")

        round1 = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": func_name,
                    "arguments": json.dumps({"text": "testing 123"}),
                },
            }],
        }
        round2 = {
            "role": "assistant",
            "content": "Result was echoed back.\n[[CONFIDENCE: high]]",
        }

        mock_complete = AsyncMock(side_effect=[round1, round2])
        with patch("handlers.chat_tools.complete_chat", mock_complete), patch("mcp_client.get_mcp_manager", return_value=mgr):
            messages = [{"role": "user", "content": "echo test"}]
            result = await run_tool_loop(self.msg, self.port, self.settings, messages, self.cfg)

            self.assertFalse(result.confirmation_requested)
            self.assertIn("Result was echoed back.", result.text)
            self.assertEqual(result.tools_called, ["mcp.notes.echo"])

            # Check tool result wrapper
            second_call_messages = mock_complete.call_args_list[1][0][1]
            tool_msg = second_call_messages[-1]
            self.assertEqual(tool_msg["role"], "tool")
            self.assertIn('<tool-result name="mcp.notes.echo" untrusted="true">', tool_msg["content"])
            self.assertIn("testing 123", tool_msg["content"])

    async def test_tool_loop_discovers_tools_without_manual_refresh(self) -> None:
        # A fresh process (e.g. the Telegram bot) has an empty cache: the loop must list tools itself.
        await self._setup_server_config()
        fresh = MCPManager()
        final = {"role": "assistant", "content": "ok\n[[CONFIDENCE: high]]"}
        mock_complete = AsyncMock(side_effect=[final])
        with patch("handlers.chat_tools.complete_chat", mock_complete), patch("mcp_client.get_mcp_manager", return_value=fresh):
            await run_tool_loop(self.msg, self.port, self.settings, [{"role": "user", "content": "hi"}], self.cfg)
        tools = mock_complete.call_args_list[0][1]["tools"]
        names = [t["function"]["name"] for t in tools]
        self.assertIn(tool_to_func_name("mcp.notes.echo"), names)
        self.assertIn(tool_to_func_name("mcp.notes.write_note"), names)

    async def test_ensure_fresh_skips_disabled_servers(self) -> None:
        await self._setup_server_config()
        from mcp_client.config import save_disabled_servers
        save_disabled_servers(self.settings.codex_memory_root, {"notes"})
        fresh = MCPManager()
        await fresh.ensure_fresh(self.settings)
        self.assertNotIn("notes", fresh._cache)

    async def test_invalid_arguments_handling(self) -> None:
        mgr = await self._setup_server_config()
        func_name = tool_to_func_name("mcp.notes.echo")

        # Invalid arguments: not a json object (e.g. array or primitive or bad JSON)
        round1 = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_bad",
                "type": "function",
                "function": {
                    "name": func_name,
                    "arguments": "not json",
                },
            }],
        }
        round2 = {
            "role": "assistant",
            "content": "Saw invalid args.\n[[CONFIDENCE: high]]",
        }

        mock_complete = AsyncMock(side_effect=[round1, round2])
        with patch("handlers.chat_tools.complete_chat", mock_complete), patch("mcp_client.get_mcp_manager", return_value=mgr):
            messages = [{"role": "user", "content": "echo test"}]
            result = await run_tool_loop(self.msg, self.port, self.settings, messages, self.cfg)

            second_call_messages = mock_complete.call_args_list[1][0][1]
            tool_msg = second_call_messages[-1]
            self.assertIn("invalid arguments", tool_msg["content"])

    async def test_write_tool_requires_confirmation_and_executes(self) -> None:
        notes_file = str(Path(self.tmp.name) / "notes.txt")
        mgr = await self._setup_server_config(notes_file=notes_file)
        func_name = tool_to_func_name("mcp.notes.write_note")

        try:
            write_call = {
                "role": "assistant",
                "content": "I will write this note.",
                "tool_calls": [{
                    "id": "call_w",
                    "type": "function",
                    "function": {
                        "name": func_name,
                        "arguments": json.dumps({"text": "important meeting at 3pm"}),
                    },
                }],
            }

            mock_complete = AsyncMock(return_value=write_call)
            with patch("handlers.chat_tools.complete_chat", mock_complete), patch("mcp_client.get_mcp_manager", return_value=mgr):
                messages = [{"role": "user", "content": "write a note"}]
                result = await run_tool_loop(self.msg, self.port, self.settings, messages, self.cfg)

                self.assertTrue(result.confirmation_requested)
                self.assertEqual(len(self.port.buttons), 1)

                # Check pending action
                pending = get_pending_for_context("op-1", "chat-1", "web")
                self.assertIsNotNone(pending)
                self.assertEqual(pending.tool_name, "mcp.notes.write_note")
                self.assertEqual(json.loads(pending.arg), {"text": "important meeting at 3pm"})

                # Execute confirmed
                exec_port = FakeOutboundPort()
                handled = await execute_confirmed(
                    self.msg,
                    exec_port,
                    self.settings,
                    pending.token,
                )
                self.assertTrue(handled)
                self.assertIn("saved", exec_port.replies[-1][1])
                self.assertTrue(Path(notes_file).exists())
                self.assertIn("important meeting at 3pm", Path(notes_file).read_text(encoding="utf-8"))
        finally:
            os.environ.pop("MCP_TEST_NOTES_FILE", None)

    async def test_execution_time_revalidation(self) -> None:
        mgr = await self._setup_server_config()
        # Pending action created earlier
        from handlers.tools.confirm import create_pending
        pending = create_pending(
            "mcp.notes.write_note",
            json.dumps({"text": "test"}),
            operator_id="op-1",
            chat_id="chat-1",
            channel="web",
        )

        # Disable server via state file
        save_state(self.settings, {"disabled": ["notes"]})

        with patch("mcp_client.get_mcp_manager", return_value=mgr):
            exec_port = FakeOutboundPort()
            handled = await execute_confirmed(
                self.msg,
                exec_port,
                self.settings,
                pending.token,
            )
            self.assertTrue(handled)
            self.assertIn("disabled", exec_port.replies[-1][1].lower())


class TestMCPWebConsoleApi(unittest.TestCase):
    TOKEN = "test-mcp-web-token-12345"

    @classmethod
    def setUpClass(cls) -> None:
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
        type(self).settings = dataclasses.replace(self.settings, mcp_enabled=value)
        self.control.settings = type(self).settings

    def setUp(self) -> None:
        self._set_enabled(True)
        config_data = {
            "servers": {
                "notes": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [FIXTURE_SERVER, "--stdio"],
                    "allow_tools": ["echo", "add"],
                    "read_only_tools": ["echo"],
                    "env": {"SECRET_NOT_LEAKED": "mcp-env-value-9f3c1a"},
                }
            }
        }
        self.settings.mcp_config_path.write_text(json.dumps(config_data), encoding="utf-8")
        # Clear state file
        state_file = self.settings.codex_memory_root / "mcp_state.json"
        if state_file.exists():
            state_file.unlink()

    def request(self, method: str, path: str, body: dict | None = None, *, auth: bool = True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if auth:
            headers["Authorization"] = f"Bearer {self.TOKEN}"
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        try:
            return res.status, json.loads(data.decode("utf-8") or "{}")
        except Exception:
            return res.status, data

    def test_auth_and_flag_disabled(self) -> None:
        # 401 when unauthenticated
        self.assertEqual(self.request("GET", "/api/mcp/servers", auth=False)[0], 401)
        self.assertEqual(self.request("POST", "/api/mcp/servers/notes/refresh", auth=False)[0], 401)
        self.assertEqual(self.request("PUT", "/api/mcp/servers/notes", {"enabled": False}, auth=False)[0], 401)

        # 409 when flag is disabled
        self._set_enabled(False)
        status, data = self.request("GET", "/api/mcp/servers")
        self.assertEqual(status, 409)
        self.assertIn("CONVEYOR_MCP_ENABLED", data.get("error", ""))

    def test_get_servers_list_and_secret_redaction(self) -> None:
        status, data = self.request("GET", "/api/mcp/servers")
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 1)
        item = data["items"][0]
        self.assertEqual(item["name"], "notes")
        self.assertEqual(item["transport"], "stdio")
        self.assertTrue(item["enabled"])
        # Target must be cleaned
        self.assertIn(Path(sys.executable).name, item["target"])
        # Secrets / env values MUST NOT be present in item
        item_str = json.dumps(item)
        self.assertNotIn("SECRET_NOT_LEAKED", item_str)
        self.assertNotIn("mcp-env-value-9f3c1a", item_str)

    def test_refresh_and_toggle_endpoints(self) -> None:
        # 1. Refresh server (lists tools)
        status, item = self.request("POST", "/api/mcp/servers/notes/refresh")
        self.assertEqual(status, 200)
        self.assertEqual(item["status"], "ok")
        self.assertGreaterEqual(item["tool_count"], 2)
        tool_names = [t["name"] for t in item["tools"]]
        self.assertIn("echo", tool_names)

        # 2. Toggle enabled to False
        status, item = self.request("PUT", "/api/mcp/servers/notes", {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(item["enabled"])
        self.assertEqual(item["status"], "disabled")

        # Verify state file written with 0600 mode
        state_file = self.settings.codex_memory_root / "mcp_state.json"
        self.assertTrue(state_file.exists())
        self.assertEqual(stat.S_IMODE(state_file.stat().st_mode), 0o600)

        # 3. Toggle back to True
        status, item = self.request("PUT", "/api/mcp/servers/notes", {"enabled": True})
        self.assertEqual(status, 200)
        self.assertTrue(item["enabled"])

        # 4. Unknown server returns 404
        status, _ = self.request("PUT", "/api/mcp/servers/nonexistent", {"enabled": False})
        self.assertEqual(status, 404)

        status, _ = self.request("POST", "/api/mcp/servers/nonexistent/refresh")
        self.assertEqual(status, 404)
