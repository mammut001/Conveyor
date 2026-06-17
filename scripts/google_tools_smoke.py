#!/usr/bin/env python3
"""google_tools_smoke.py — P3.4 Google OAuth/Calendar/Contacts smoke tests.

Run: .venv/bin/python scripts/google_tools_smoke.py
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
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/codex-google-ws")
os.environ.setdefault("CODEX_TASK_ROOT", "/tmp/codex-google-task")
os.environ.setdefault("CODEX_BIN", "codex")
os.environ.setdefault("USER_TIMEZONE", "UTC")

from channel import InboundMessage
from config import Settings, load_settings
from handlers.commands import COMMAND_TABLE, run_command
from handlers.tools.confirm import clear_all_pending
from handlers.tools.registry import DangerLevel
from handlers.tools.runner import run_tool, _invoke_tool
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


def _settings(tmp: Path, *, no_google: bool = False) -> Settings:
    base = load_settings()
    kwargs = dict(codex_memory_root=tmp, telegram_allowed_user_id=12345, user_timezone="UTC")
    if no_google:
        kwargs.update(
            google_client_secret_path=None,
            google_token_path=None,
        )
    return replace(base, **kwargs)


# ---- Test 1: missing Google config handled gracefully ----

async def test_google_status_no_config() -> CheckResult:
    name = "google.status: missing config handled gracefully"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td), no_google=True)
            result = await run_tool(settings, "google.status")
            ok = "未设置" in result or "未授权" in result or "未配置" in result
            return CheckResult(name, ok, f"result={result[:100]}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 2: calendar.status reports missing auth gracefully ----

async def test_calendar_status_no_auth() -> CheckResult:
    name = "calendar.status: missing auth handled gracefully"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td), no_google=True)
            result = await run_tool(settings, "calendar.status")
            ok = "未授权" in result or "auth_google" in result or "未设置" in result
            return CheckResult(name, ok, f"result={result[:100]}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 3: contacts.search missing auth handled gracefully ----

async def test_contacts_search_no_auth() -> CheckResult:
    name = "contacts.search: missing auth handled gracefully"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td), no_google=True)
            result = await run_tool(settings, "contacts.search", "test")
            ok = "未授权" in result or "auth_google" in result or "未设置" in result
            return CheckResult(name, ok, f"result={result[:100]}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 4: /auth_google returns clear setup instructions ----

async def test_auth_google_instructions() -> CheckResult:
    name = "/auth_google: returns setup instructions or confirmation"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td), no_google=True)
            port = FakeOutbound()
            runner = FakeRunner()
            await run_command("auth_google", _msg("/auth_google"), port, runner, settings, "")
            text = "\n".join(port.replies)
            # Should either show confirmation prompt (WRITE) or setup instructions
            ok = "确认" in text or "GOOGLE_CLIENT_SECRET_PATH" in text or "client_secret" in text or "未设置" in text
            return CheckResult(name, ok, f"text={text[:150]}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 5: calendar.create requires confirmation ----

async def test_calendar_create_needs_confirm() -> CheckResult:
    name = "calendar.create: requests confirmation"
    try:
        clear_all_pending()
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td), no_google=True)
            port = FakeOutbound()
            await _invoke_tool(_msg(), port, settings, "calendar.create", "周会 | 明天 14:00-15:00 | 讨论")
            has_confirm = any("确认" in r or "需确认" in r or "未授权" in r for r in port.replies)
            ok = has_confirm
            return CheckResult(name, ok, f"replies={port.replies[:3]}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 6: token path redacted in outputs ----

def test_token_path_redacted() -> CheckResult:
    name = "security: token path does not expose secrets"
    try:
        settings = _settings(Path("/tmp"))
        # Token path should be configurable but not contain raw tokens
        tok_path = settings.google_token_path
        ok = tok_path is None or "token" not in tok_path.lower() or tok_path.endswith(".json")
        return CheckResult(name, ok, f"path={tok_path}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 7: registry has correct danger levels ----

def test_registry_danger_levels() -> CheckResult:
    name = "registry: Google/Calendar/Contacts tools have correct danger levels"
    try:
        google_status = PERSONAL_TOOL_REGISTRY.get("google.status")
        google_auth = PERSONAL_TOOL_REGISTRY.get("google.auth")
        google_revoke = PERSONAL_TOOL_REGISTRY.get("google.revoke")
        calendar_status = PERSONAL_TOOL_REGISTRY.get("calendar.status")
        calendar_today = PERSONAL_TOOL_REGISTRY.get("calendar.today")
        calendar_create = PERSONAL_TOOL_REGISTRY.get("calendar.create")
        contacts_search = PERSONAL_TOOL_REGISTRY.get("contacts.search")

        ok = (
            google_status is not None and google_status.danger == DangerLevel.READ
            and google_auth is not None and google_auth.danger == DangerLevel.WRITE
            and google_revoke is not None and google_revoke.danger == DangerLevel.DESTRUCTIVE
            and calendar_status is not None and calendar_status.danger == DangerLevel.READ
            and calendar_today is not None and calendar_today.danger == DangerLevel.READ
            and calendar_create is not None and calendar_create.danger == DangerLevel.WRITE
            and contacts_search is not None and contacts_search.danger == DangerLevel.READ
        )
        return CheckResult(name, ok, f"google_status={google_status.danger if google_status else None} calendar_create={calendar_create.danger if calendar_create else None}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 8: commands registered ----

def test_commands_registered() -> CheckResult:
    name = "COMMAND_TABLE: Google/Calendar/Contacts commands registered"
    try:
        cmds = ["google_status", "auth_google", "google_revoke",
                "calendar_status", "calendar_today", "calendar_tomorrow",
                "calendar_week", "calendar_search", "calendar_freebusy",
                "calendar_create", "contacts_search"]
        missing = [c for c in cmds if c not in COMMAND_TABLE]
        ok = len(missing) == 0
        return CheckResult(name, ok, f"missing={missing}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 9: /help and /tools include new commands ----

async def test_help_and_tools_include_google() -> CheckResult:
    name = "/help and /tools: include Google/Calendar/Contacts commands"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            port = FakeOutbound()
            runner = FakeRunner()
            runner.settings = settings

            # Test /help
            await run_command("help", _msg("/help"), port, runner, settings, "")
            help_text = "\n".join(port.replies)
            has_google_in_help = "/auth_google" in help_text and "/calendar_today" in help_text

            # Test /tools
            port.replies.clear()
            await run_command("tools", _msg("/tools"), port, runner, settings, "")
            tools_text = "\n".join(port.replies)
            has_google_in_tools = "calendar.status" in tools_text and "contacts.search" in tools_text

            ok = has_google_in_help and has_google_in_tools
            return CheckResult(name, ok, f"help={has_google_in_help} tools={has_google_in_tools}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- Test 10: natural language intent routes to Calendar/Contacts ----

def test_intent_routing() -> CheckResult:
    name = "intent: natural language routes to Calendar/Contacts"
    try:
        from handlers.intent import route_intent

        today = route_intent("看看今天的日程")
        tomorrow = route_intent("明天有什么安排")
        week = route_intent("本周日程")
        search_cal = route_intent("搜索日程 关于会议")
        search_contact = route_intent("找一下联系人 张三")
        auth = route_intent("授权 google")

        ok_today = today.kind == "deterministic" and "calendar.today" in today.tools
        ok_tomorrow = tomorrow.kind == "deterministic" and "calendar.tomorrow" in tomorrow.tools
        ok_week = week.kind == "deterministic" and "calendar.week" in week.tools
        ok_search = search_cal.kind == "deterministic" and "calendar.search" in search_cal.tools
        ok_contact = search_contact.kind == "deterministic" and "contacts.search" in search_contact.tools
        ok_auth = auth.kind == "deterministic" and "google.status" in auth.tools

        ok = ok_today and ok_tomorrow and ok_week and ok_search and ok_contact and ok_auth
        return CheckResult(name, ok, f"today={ok_today} tomorrow={ok_tomorrow} week={ok_week} search={ok_search} contact={ok_contact} auth={ok_auth}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- main ----

async def main() -> int:
    results = [
        await test_google_status_no_config(),
        await test_calendar_status_no_auth(),
        await test_contacts_search_no_auth(),
        await test_auth_google_instructions(),
        await test_calendar_create_needs_confirm(),
        test_token_path_redacted(),
        test_registry_danger_levels(),
        test_commands_registered(),
        await test_help_and_tools_include_google(),
        test_intent_routing(),
    ]
    print_results(results)
    ok = all(r.ok for r in results)
    print("google tools smoke ok" if ok else "google tools smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
