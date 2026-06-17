#!/usr/bin/env python3
"""scripts/briefing_smoke.py — smoke tests for P3.5 Daily Briefing.

Run from repo root:
    python scripts/briefing_smoke.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

# Ensure repo root on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = 0
FAIL = 0


def _ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS  {label}")


def _fail(label: str, detail: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL  {label}: {detail}")


def _settings(**overrides):
    """Create a test Settings with sensible defaults."""
    from dataclasses import replace
    from config import load_settings
    base = load_settings()
    return replace(
        base,
        codex_memory_root=overrides.get("codex_memory_root", Path(tempfile.mkdtemp())),
        codex_task_root=overrides.get("codex_task_root", Path(tempfile.mkdtemp())),
        telegram_bot_token=overrides.get("telegram_bot_token", "test-token"),
        telegram_allowed_user_id=12345,
        gmail_backend=overrides.get("gmail_backend", None),
        gmail_address=overrides.get("gmail_address", None),
        gmail_app_password=overrides.get("gmail_app_password", None),
        google_client_secret_path=overrides.get("google_client_secret_path", None),
        google_token_path=overrides.get("google_token_path", None),
        google_oauth_scopes=overrides.get("google_oauth_scopes", ""),
        user_timezone=overrides.get("user_timezone", "America/Toronto"),
    )


# --- Tests -------------------------------------------------------------------

def test_store_briefing_settings_crud():
    """Briefing settings create/read/update."""
    from personal_tools.store import PersonalToolsStore
    settings = _settings()
    store = PersonalToolsStore(settings)
    op = "test-user"

    # Initially no settings
    bs = store.get_briefing_settings(op)
    if bs is not None:
        _fail("store_briefing_settings_crud", "expected None for missing settings")
        return

    # Enable
    store.update_briefing_settings(
        op, enabled=True, local_time="09:00", channel="telegram", chat_id="12345"
    )
    bs = store.get_briefing_settings(op)
    if not bs:
        _fail("store_briefing_settings_crud", "expected settings after enable")
        return
    if not bs.enabled or bs.local_time != "09:00":
        _fail("store_briefing_settings_crud", f"unexpected settings: enabled={bs.enabled}, time={bs.local_time}")
        return

    # Update time
    store.update_briefing_settings(
        op, enabled=True, local_time="10:30", channel="telegram", chat_id="12345"
    )
    bs = store.get_briefing_settings(op)
    if bs.local_time != "10:30":
        _fail("store_briefing_settings_crud", f"expected time 10:30, got {bs.local_time}")
        return

    # Disable
    store.update_briefing_settings(op, enabled=False)
    bs = store.get_briefing_settings(op)
    if bs.enabled:
        _fail("store_briefing_settings_crud", "expected disabled")
        return

    _ok("store_briefing_settings_crud")


def test_store_briefing_runs():
    """Briefing runs record and deduplicate."""
    from personal_tools.store import PersonalToolsStore
    settings = _settings()
    store = PersonalToolsStore(settings)
    op = "test-user"

    # No runs initially
    if store.has_briefing_run_for_date(op, "2026-06-15"):
        _fail("store_briefing_runs", "expected no run initially")
        return

    # Record a run
    store.record_briefing_run(op, "2026-06-15", status="sent")
    if not store.has_briefing_run_for_date(op, "2026-06-15"):
        _fail("store_briefing_runs", "expected run after record")
        return

    # Duplicate update (should not error)
    store.record_briefing_run(op, "2026-06-15", status="sent")
    if not store.has_briefing_run_for_date(op, "2026-06-15"):
        _fail("store_briefing_runs", "expected run still present after duplicate")
        return

    # Different date
    if store.has_briefing_run_for_date(op, "2026-06-16"):
        _fail("store_briefing_runs", "expected no run for different date")
        return

    _ok("store_briefing_runs")


def test_list_enabled_briefings():
    """Only enabled briefings are listed."""
    from personal_tools.store import PersonalToolsStore
    settings = _settings()
    store = PersonalToolsStore(settings)

    store.update_briefing_settings(
        "user1", enabled=True, local_time="09:00", channel="telegram", chat_id="111"
    )
    store.update_briefing_settings(
        "user2", enabled=False, local_time="09:00", channel="telegram", chat_id="222"
    )
    store.update_briefing_settings(
        "user3", enabled=True, local_time="10:00", channel="telegram", chat_id="333"
    )

    enabled = store.list_enabled_briefings()
    if len(enabled) != 2:
        _fail("list_enabled_briefings", f"expected 2 enabled, got {len(enabled)}")
        return
    op_ids = {b.operator_id for b in enabled}
    if op_ids != {"user1", "user3"}:
        _fail("list_enabled_briefings", f"expected user1+user3, got {op_ids}")
        return

    _ok("list_enabled_briefings")


def test_briefing_build_graceful_degradation():
    """Briefing build degrades gracefully with missing Gmail/Google config."""
    from personal_tools.briefing import briefing_build
    settings = _settings()  # No Gmail, no Google
    op = "test-user"

    result = briefing_build(settings, op)
    if not result.ok:
        _fail("briefing_build_graceful_degradation", f"expected ok=True, got {result.text}")
        return

    # Should contain fallback messages
    if "未配置" not in result.text and "跳过" not in result.text:
        _fail("briefing_build_graceful_degradation", "expected fallback for missing config")
        return

    _ok("briefing_build_graceful_degradation")


def test_briefing_enable_disable():
    """Briefing enable and disable work."""
    from personal_tools.briefing import briefing_enable, briefing_disable, briefing_status
    from personal_tools.store import PersonalToolsStore
    settings = _settings()
    store = PersonalToolsStore(settings)
    op = "test-user"

    # Enable
    result = briefing_enable(settings, op, "telegram", "12345", "10:00")
    if not result.ok:
        _fail("briefing_enable_disable", f"enable failed: {result.text}")
        return
    if "已启用" not in result.text:
        _fail("briefing_enable_disable", "expected enable confirmation")
        return

    # Status should show enabled
    result = briefing_status(settings, op)
    if "已启用" not in result.text:
        _fail("briefing_enable_disable", f"expected enabled status, got: {result.text}")
        return

    # Disable
    result = briefing_disable(settings, op)
    if not result.ok:
        _fail("briefing_enable_disable", f"disable failed: {result.text}")
        return
    if "已禁用" not in result.text:
        _fail("briefing_enable_disable", "expected disable confirmation")
        return

    # Status should show not enabled
    result = briefing_status(settings, op)
    if "未启用" not in result.text:
        _fail("briefing_enable_disable", f"expected disabled status, got: {result.text}")
        return

    _ok("briefing_enable_disable")


def test_briefing_enable_invalid_time():
    """Briefing enable rejects invalid time format."""
    from personal_tools.briefing import briefing_enable
    settings = _settings()
    op = "test-user"

    result = briefing_enable(settings, op, "telegram", "12345", "25:00")
    if result.ok:
        _fail("briefing_enable_invalid_time", "expected failure for invalid time")
        return
    if "无效" not in result.text:
        _fail("briefing_enable_invalid_time", f"expected error about invalid time, got: {result.text}")
        return

    _ok("briefing_enable_invalid_time")


def test_briefing_probe_dry_run():
    """Briefing probe should build but not send."""
    from personal_tools.briefing import briefing_probe
    settings = _settings()
    op = "test-user"

    result = briefing_probe(settings, op)
    if not result.ok:
        _fail("briefing_probe_dry_run", f"expected ok, got {result.text}")
        return
    if "探针" not in result.text and "dry-run" not in result.text:
        _fail("briefing_probe_dry_run", f"expected probe indicator, got: {result.text}")
        return

    _ok("briefing_probe_dry_run")


def test_registry_danger_levels():
    """Briefing tools have correct danger levels."""
    from handlers.tools.registry import DangerLevel
    from personal_tools.registry import PERSONAL_TOOL_REGISTRY, register_personal_tools
    register_personal_tools()

    expected = {
        "briefing.status": DangerLevel.READ,
        "briefing.today": DangerLevel.READ,
        "briefing.tomorrow": DangerLevel.READ,
        "briefing.enable": DangerLevel.WRITE_SAFE,
        "briefing.disable": DangerLevel.WRITE,
        "briefing.probe": DangerLevel.READ,
    }

    for name, expected_danger in expected.items():
        spec = PERSONAL_TOOL_REGISTRY.get(name)
        if not spec:
            _fail("registry_danger_levels", f"missing tool: {name}")
            return
        if spec.danger != expected_danger:
            _fail("registry_danger_levels", f"{name}: expected {expected_danger}, got {spec.danger}")
            return

    _ok("registry_danger_levels")


def test_registry_count():
    """All briefing tools are registered."""
    from personal_tools.registry import PERSONAL_TOOL_REGISTRY, register_personal_tools
    register_personal_tools()

    briefing_tools = [n for n in PERSONAL_TOOL_REGISTRY if n.startswith("briefing.")]
    if len(briefing_tools) != 6:
        _fail("registry_count", f"expected 6 briefing tools, got {len(briefing_tools)}")
        return

    _ok("registry_count")


def test_commands_registered():
    """Briefing slash commands are in COMMAND_TABLE."""
    from handlers.commands import COMMAND_TABLE

    expected = [
        "brief_today", "brief_tomorrow", "brief_settings",
        "brief_enable", "brief_disable", "brief_probe",
    ]
    for cmd in expected:
        if cmd not in COMMAND_TABLE:
            _fail("commands_registered", f"missing command: {cmd}")
            return

    _ok("commands_registered")


def test_help_includes_briefing():
    """/help output includes briefing commands."""
    import asyncio
    from handlers.commands import _help
    from unittest.mock import AsyncMock

    msg = AsyncMock()
    port = AsyncMock()
    runner = AsyncMock()
    settings = _settings()

    asyncio.run(_help(msg, port, runner, settings, ""))

    # Check port.reply was called
    if not port.reply.called:
        _fail("help_includes_briefing", "port.reply not called")
        return

    text = port.reply.call_args[0][1]
    if "/brief_today" not in text:
        _fail("help_includes_briefing", "/brief_today not in help")
        return
    if "/brief_enable" not in text:
        _fail("help_includes_briefing", "/brief_enable not in help")
        return

    _ok("help_includes_briefing")


def test_tools_includes_briefing():
    """/tools output includes briefing tools."""
    import asyncio
    from handlers.commands import _tools
    from unittest.mock import AsyncMock

    msg = AsyncMock()
    port = AsyncMock()
    runner = AsyncMock()
    settings = _settings()

    asyncio.run(_tools(msg, port, runner, settings, ""))

    text = port.reply.call_args[0][1]
    if "briefing.status" not in text:
        _fail("tools_includes_briefing", "briefing.status not in /tools output")
        return

    _ok("tools_includes_briefing")


def test_intent_routing():
    """Natural language phrases route to briefing tools."""
    from handlers.intent import route_intent

    cases = [
        ("今日简报", "briefing.today"),
        ("看看今天的简报", "briefing.today"),
        ("明天简报", "briefing.tomorrow"),
        ("简报设置", "briefing.status"),
        ("启用每日简报", "briefing.enable"),
        ("关闭简报", "briefing.disable"),
    ]

    for text, expected_tool in cases:
        result = route_intent(text)
        if expected_tool not in result.tools:
            _fail("intent_routing", f"'{text}' expected {expected_tool}, got {result.tools}")
            return

    _ok("intent_routing")


def test_no_duplicate_briefing_for_date():
    """Scheduler does not send duplicate briefing for same local date."""
    from personal_tools.store import PersonalToolsStore
    settings = _settings()
    store = PersonalToolsStore(settings)
    op = "test-user"

    # Record a run
    store.record_briefing_run(op, "2026-06-15", status="sent")

    # Check duplicate detection
    if not store.has_briefing_run_for_date(op, "2026-06-15"):
        _fail("no_duplicate_briefing", "expected duplicate detected")
        return

    # Different date should not be detected
    if store.has_briefing_run_for_date(op, "2026-06-16"):
        _fail("no_duplicate_briefing", "different date should not match")
        return

    _ok("no_duplicate_briefing")


def test_redaction_in_output():
    """Briefing output does not contain sensitive info."""
    from personal_tools.briefing import briefing_build
    settings = _settings(
        gmail_app_password="secret-password-1234",
        telegram_bot_token="bot-token-secret",
    )
    op = "test-user"

    result = briefing_build(settings, op)
    if "secret-password-1234" in result.text:
        _fail("redaction_in_output", "Gmail password found in output")
        return
    if "bot-token-secret" in result.text:
        _fail("redaction_in_output", "Bot token found in output")
        return

    _ok("redaction_in_output")


# --- Main --------------------------------------------------------------------

def main() -> int:
    global PASS, FAIL

    tests = [
        test_store_briefing_settings_crud,
        test_store_briefing_runs,
        test_list_enabled_briefings,
        test_briefing_build_graceful_degradation,
        test_briefing_enable_disable,
        test_briefing_enable_invalid_time,
        test_briefing_probe_dry_run,
        test_registry_danger_levels,
        test_registry_count,
        test_commands_registered,
        test_help_includes_briefing,
        test_tools_includes_briefing,
        test_intent_routing,
        test_no_duplicate_briefing_for_date,
        test_redaction_in_output,
    ]

    print(f"Running {len(tests)} briefing smoke tests...\n")
    for test in tests:
        try:
            test()
        except Exception as exc:
            _fail(test.__name__, f"unhandled exception: {type(exc).__name__}: {exc}")

    print(f"\n{'='*50}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
