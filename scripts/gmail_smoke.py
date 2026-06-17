#!/usr/bin/env python3
"""gmail_smoke.py — P3.3 Gmail App Password MVP smoke tests.

Run: .venv/bin/python scripts/gmail_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "12345")
os.environ.setdefault("LARK_APP_ID", "cli_fake")
os.environ.setdefault("LARK_APP_SECRET", "fake")
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/codex-gmail-ws")
os.environ.setdefault("CODEX_TASK_ROOT", "/tmp/codex-gmail-task")
os.environ.setdefault("CODEX_BIN", "codex")
os.environ.setdefault("USER_TIMEZONE", "UTC")

from channel import InboundMessage
from config import Settings, load_settings
from handlers.commands import COMMAND_TABLE, run_command
from handlers.tools.confirm import clear_all_pending, get_pending_for_context
from handlers.tools.registry import TOOL_REGISTRY, DangerLevel
from handlers.tools.runner import _invoke_tool, run_tool
from personal_tools.email_smtp import parse_email_send_args
from personal_tools.registry import PERSONAL_TOOL_REGISTRY
from scripts.harness_common import CheckResult, print_results


@dataclass
class FakeOutbound:
    replies: list[str] = field(default_factory=list)
    supports_inline_buttons: bool = False

    async def reply(self, msg, text):
        self.replies.append(text)
        return None

    async def reply_with_buttons(self, msg, text, buttons):
        self.replies.append(text)
        return None


class FakeRunner:
    settings: Settings | None = None


def _msg(text: str = "", operator_id: str = "12345") -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        operator_id=operator_id,
        chat_id="chat-1",
        message_id="m-1",
        text=text,
    )


def _settings(tmp: Path, *, gmail_off: bool = False) -> Settings:
    base = load_settings()
    kwargs = dict(codex_memory_root=tmp, telegram_allowed_user_id=12345, user_timezone="UTC")
    if gmail_off:
        kwargs.update(
            gmail_backend=None,
            gmail_address=None,
            gmail_app_password=None,
        )
    return replace(base, **kwargs)


# ---- Test 1: config loads Gmail env placeholders ----

def test_config_gmail_fields() -> CheckResult:
    name = "config: Gmail fields have correct defaults"
    try:
        settings = load_settings()
        # Defaults should be present (imap/smtp hosts/ports always set)
        ok = (
            settings.gmail_imap_host == "imap.gmail.com"
            and settings.gmail_imap_port == 993
            and settings.gmail_smtp_host == "smtp.gmail.com"
            and settings.gmail_smtp_port == 587
        )
        return CheckResult(name, ok, f"backend={settings.gmail_backend} imap={settings.gmail_imap_host}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 2: gmail.status reports missing config gracefully ----

async def test_gmail_status_missing_config() -> CheckResult:
    name = "gmail.status: reports missing config gracefully"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td), gmail_off=True)
            result = await run_tool(settings, "gmail.status")
            has_warning = "未配置" in result or "未设置" in result
            ok = has_warning
            return CheckResult(name, ok, f"warning={has_warning} result={result[:100]}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 3: gmail.recent/search/read do not run network when config missing ----

async def test_gmail_no_network_without_config() -> CheckResult:
    name = "gmail.recent/search/read: no network when config missing"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td), gmail_off=True)
            recent = await run_tool(settings, "gmail.recent")
            search = await run_tool(settings, "gmail.search", "test")
            read = await run_tool(settings, "gmail.read", "123")
            # All should report config missing
            all_warn = all("未配置" in r or "未设置" in r for r in [recent, search, read])
            ok = all_warn
            return CheckResult(name, ok, f"all_warn={all_warn}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 4: email_send parse errors are clear ----

def test_email_send_parse_errors() -> CheckResult:
    name = "email.send: parse errors are clear"
    try:
        # Too few parts
        r1 = parse_email_send_args("test@example.com")
        r2 = parse_email_send_args("test@example.com | subject")
        r3 = parse_email_send_args(" | subject | body")
        r4 = parse_email_send_args("test@example.com | | body")
        ok = (
            r1 is None
            and r2 is None
            and r3 is None
            and r4 is None
        )
        return CheckResult(name, ok, f"r1={r1} r2={r2} r3={r3} r4={r4}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 5: email.send requires confirmation ----

async def test_email_send_needs_confirm() -> CheckResult:
    name = "email.send: requests confirmation"
    try:
        clear_all_pending()
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            port = FakeOutbound()
            await _invoke_tool(_msg(), port, settings, "email.send", "test@example.com | 测试 | 内容")
            has_confirm_text = any("需确认" in r or "确认" in r for r in port.replies)
            pending = get_pending_for_context("12345", "chat-1", "telegram")
            ok = has_confirm_text and pending is not None
            return CheckResult(name, ok, f"confirm_text={has_confirm_text} pending={pending is not None}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 6: app password is redacted in all outputs ----

def test_app_password_redacted() -> CheckResult:
    name = "security: app password redacted in outputs"
    try:
        # Simulate a config with password
        settings = _settings(Path("/tmp"))
        settings_with_pw = replace(settings, gmail_app_password="abcd1234efgh5678")
        # The password should not appear in repr or any output
        repr_str = repr(settings_with_pw)
        ok = "abcd1234efgh5678" not in repr_str
        return CheckResult(name, ok, f"password_in_repr={'abcd1234efgh5678' in repr_str}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 7: /help and /tools include Gmail/email commands ----

async def test_help_and_tools_include_gmail() -> CheckResult:
    name = "/help and /tools: include Gmail/email commands"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            port = FakeOutbound()
            runner = FakeRunner()
            runner.settings = settings

            # Test /help
            await run_command("help", _msg("/help"), port, runner, settings, "")
            help_text = "\n".join(port.replies)
            has_gmail_in_help = "/gmail_status" in help_text and "/email_send" in help_text

            # Test /tools
            port.replies.clear()
            await run_command("tools", _msg("/tools"), port, runner, settings, "")
            tools_text = "\n".join(port.replies)
            has_gmail_in_tools = "gmail.status" in tools_text and "email.send" in tools_text

            ok = has_gmail_in_help and has_gmail_in_tools
            return CheckResult(name, ok, f"help={has_gmail_in_help} tools={has_gmail_in_tools}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 8: registry has correct danger levels ----

def test_registry_danger_levels() -> CheckResult:
    name = "registry: Gmail/email tools have correct danger levels"
    try:
        gmail_status_spec = PERSONAL_TOOL_REGISTRY.get("gmail.status")
        gmail_recent_spec = PERSONAL_TOOL_REGISTRY.get("gmail.recent")
        gmail_search_spec = PERSONAL_TOOL_REGISTRY.get("gmail.search")
        gmail_read_spec = PERSONAL_TOOL_REGISTRY.get("gmail.read")
        email_send_spec = PERSONAL_TOOL_REGISTRY.get("email.send")

        ok_status = gmail_status_spec is not None and gmail_status_spec.danger == DangerLevel.READ
        ok_recent = gmail_recent_spec is not None and gmail_recent_spec.danger == DangerLevel.READ
        ok_search = gmail_search_spec is not None and gmail_search_spec.danger == DangerLevel.READ
        ok_read = gmail_read_spec is not None and gmail_read_spec.danger == DangerLevel.READ
        ok_send = email_send_spec is not None and email_send_spec.danger == DangerLevel.WRITE

        ok = ok_status and ok_recent and ok_search and ok_read and ok_send
        return CheckResult(name, ok, f"status={ok_status} recent={ok_recent} search={ok_search} read={ok_read} send={ok_send}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 9: commands are registered ----

def test_commands_registered() -> CheckResult:
    name = "COMMAND_TABLE: Gmail/email commands registered"
    try:
        has_gmail_status = "gmail_status" in COMMAND_TABLE
        has_gmail_recent = "gmail_recent" in COMMAND_TABLE
        has_gmail_search = "gmail_search" in COMMAND_TABLE
        has_gmail_read = "gmail_read" in COMMAND_TABLE
        has_email_send = "email_send" in COMMAND_TABLE
        ok = has_gmail_status and has_gmail_recent and has_gmail_search and has_gmail_read and has_email_send
        return CheckResult(name, ok, f"status={has_gmail_status} recent={has_gmail_recent} search={has_gmail_search} read={has_gmail_read} send={has_email_send}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 10: natural language intent routes to Gmail tools ----

def test_intent_routing() -> CheckResult:
    name = "intent: natural language routes to Gmail tools"
    try:
        from handlers.intent import route_intent

        recent = route_intent("帮我看一下收件箱")
        recent2 = route_intent("看看邮件")
        status = route_intent("邮箱状态")
        search = route_intent("搜索邮件 关于发票")
        send = route_intent("发邮件")

        ok_recent = recent.kind == "deterministic" and "gmail.recent" in recent.tools
        ok_recent2 = recent2.kind == "deterministic" and "gmail.recent" in recent2.tools
        ok_status = status.kind == "deterministic" and "gmail.status" in status.tools
        ok_search = search.kind == "deterministic" and "gmail.search" in search.tools and search.arg == "关于发票"
        ok_send = send.kind == "llm"  # Should prompt for details

        ok = ok_recent and ok_recent2 and ok_status and ok_search and ok_send
        return CheckResult(name, ok, f"recent={ok_recent} recent2={ok_recent2} status={ok_status} search={ok_search} send={ok_send}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- main ----

async def main() -> int:
    results = [
        test_config_gmail_fields(),
        test_registry_danger_levels(),
        test_commands_registered(),
        await test_gmail_status_missing_config(),
        await test_gmail_no_network_without_config(),
        test_email_send_parse_errors(),
        await test_email_send_needs_confirm(),
        test_app_password_redacted(),
        await test_help_and_tools_include_gmail(),
        test_intent_routing(),
    ]
    print_results(results)
    ok = all(r.ok for r in results)
    print("gmail smoke ok" if ok else "gmail smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
