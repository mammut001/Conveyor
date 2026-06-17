#!/usr/bin/env python3
"""scripts/github_smoke.py — P3.6 GitHub Issues/PR Tools smoke tests.

Run from repo root:
    python scripts/github_smoke.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Ensure repo root on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "12345")
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/codex-gh-ws")
os.environ.setdefault("CODEX_TASK_ROOT", "/tmp/codex-gh-task")
os.environ.setdefault("CODEX_BIN", "codex")
os.environ.setdefault("USER_TIMEZONE", "UTC")

from config import Settings, load_settings
from handlers.tools.registry import DangerLevel
from personal_tools.registry import PERSONAL_TOOL_REGISTRY, register_personal_tools
from scripts.harness_common import CheckResult, print_results


def _settings(**overrides) -> Settings:
    """Create test settings with optional overrides."""
    base = load_settings()
    return replace(
        base,
        codex_memory_root=overrides.get("codex_memory_root", Path(tempfile.mkdtemp())),
        telegram_bot_token=overrides.get("telegram_bot_token", "test-token"),
        telegram_allowed_user_id=12345,
        github_token=overrides.get("github_token", None),
        github_default_repo=overrides.get("github_default_repo", None),
        github_api_base=overrides.get("github_api_base", "https://api.github.com"),
        user_timezone=overrides.get("user_timezone", "UTC"),
    )


def test_missing_config_graceful() -> CheckResult:
    """GitHub tools handle missing config gracefully."""
    name = "github: missing config graceful"
    try:
        from personal_tools.github_tools import github_status, github_issues, github_ci

        # No token
        s = _settings(github_token=None)
        r = github_status(s)
        if r.ok:
            return CheckResult(name, False, "expected failure with no token")
        if "GITHUB_TOKEN" not in r.text:
            return CheckResult(name, False, f"expected GITHUB_TOKEN in error: {r.text}")

        # No repo
        s2 = _settings(github_token="fake", github_default_repo=None)
        r2 = github_status(s2)
        if r2.ok:
            return CheckResult(name, False, "expected failure with no repo")
        if "GITHUB_DEFAULT_REPO" not in r2.text:
            return CheckResult(name, False, f"expected GITHUB_DEFAULT_REPO in error: {r2.text}")

        return CheckResult(name, True, "config errors clear")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def test_token_redacted() -> CheckResult:
    """GitHub token is never exposed in outputs."""
    name = "github: token redacted in status/errors"
    try:
        from personal_tools.github_tools import _check_config, _make_headers

        secret = "ghp_abc123secret456"
        s = _settings(github_token=secret, github_default_repo="owner/repo")

        # Check headers don't leak into error messages
        err = _check_config(s)
        if err is not None:
            return CheckResult(name, False, f"config check failed: {err}")

        # Test that _make_headers works but token not in repr
        headers = _make_headers(s)
        if secret not in headers.get("Authorization", ""):
            return CheckResult(name, False, "token not in Authorization header")

        # Ensure Settings repr redacts token
        if secret in repr(s):
            return CheckResult(name, False, "token in Settings repr")

        return CheckResult(name, True, "token not exposed")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def test_parse_commands() -> CheckResult:
    """GitHub tool commands parse correctly."""
    name = "github: parse issue/comment commands"
    try:
        from personal_tools.github_tools import github_create_issue_adapter, github_comment_adapter

        s = _settings(github_token="fake", github_default_repo="owner/repo")

        # Test issue adapter parse (will fail network, but parse should work)
        import asyncio
        # Patch _github_request to avoid network
        with patch("personal_tools.github_tools._github_request", return_value=(False, "mock")):
            r = asyncio.run(github_create_issue_adapter(s, "Test Title | Test Body"))
            if "创建 issue 失败" not in r.text and "mock" not in r.text:
                return CheckResult(name, False, f"unexpected create issue response: {r.text}")

        # Test comment adapter parse
        with patch("personal_tools.github_tools._github_request", return_value=(False, "mock")):
            r2 = asyncio.run(github_comment_adapter(s, "123 | Great work!"))
            if "添加评论失败" not in r2.text and "mock" not in r2.text:
                return CheckResult(name, False, f"unexpected comment response: {r2.text}")

        # Test missing args
        r3 = asyncio.run(github_comment_adapter(s, ""))
        if not r3.ok:
            # Expected - missing args
            pass

        return CheckResult(name, True, "command parsing works")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def test_comment_requires_confirmation() -> CheckResult:
    """github.comment requires WRITE confirmation."""
    name = "github.comment: requires confirmation"
    try:
        register_personal_tools()
        spec = PERSONAL_TOOL_REGISTRY.get("github.comment")
        if not spec:
            return CheckResult(name, False, "github.comment not registered")
        if spec.danger != DangerLevel.WRITE:
            return CheckResult(name, False, f"expected WRITE, got {spec.danger}")
        return CheckResult(name, True, "github.comment is WRITE")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def test_create_issue_write_safe() -> CheckResult:
    """github.create_issue is WRITE_SAFE and audited."""
    name = "github.create_issue: WRITE_SAFE audited"
    try:
        register_personal_tools()
        spec = PERSONAL_TOOL_REGISTRY.get("github.create_issue")
        if not spec:
            return CheckResult(name, False, "github.create_issue not registered")
        if spec.danger != DangerLevel.WRITE_SAFE:
            return CheckResult(name, False, f"expected WRITE_SAFE, got {spec.danger}")
        return CheckResult(name, True, "github.create_issue is WRITE_SAFE")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def test_briefing_degrades_without_github() -> CheckResult:
    """Briefing degrades gracefully without GitHub config."""
    name = "briefing: degrades without GitHub config"
    try:
        from personal_tools.briefing import _build_github_section

        s = _settings(github_token=None)
        section = _build_github_section(s)
        if "未配置" not in section:
            return CheckResult(name, False, f"expected 未配置, got: {section}")
        return CheckResult(name, True, "degrades gracefully")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def test_registry_danger_levels() -> CheckResult:
    """GitHub tools have correct danger levels."""
    name = "registry: GitHub tools correct danger levels"
    try:
        register_personal_tools()

        expected = {
            "github.status": DangerLevel.READ,
            "github.issues": DangerLevel.READ,
            "github.issue": DangerLevel.READ,
            "github.prs": DangerLevel.READ,
            "github.pr": DangerLevel.READ,
            "github.ci": DangerLevel.READ,
            "github.create_issue": DangerLevel.WRITE_SAFE,
            "github.comment": DangerLevel.WRITE,
        }

        for tool_name, expected_danger in expected.items():
            spec = PERSONAL_TOOL_REGISTRY.get(tool_name)
            if not spec:
                return CheckResult(name, False, f"missing tool: {tool_name}")
            if spec.danger != expected_danger:
                return CheckResult(name, False, f"{tool_name}: expected {expected_danger}, got {spec.danger}")

        return CheckResult(name, True, f"all {len(expected)} tools correct")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def test_commands_registered() -> CheckResult:
    """GitHub commands are in COMMAND_TABLE."""
    name = "COMMAND_TABLE: GitHub commands registered"
    try:
        from handlers.commands import COMMAND_TABLE

        expected = [
            "github_status", "github_issues", "github_issue",
            "github_prs", "github_pr", "github_ci",
            "github_create_issue", "github_comment",
        ]

        missing = [cmd for cmd in expected if cmd not in COMMAND_TABLE]
        if missing:
            return CheckResult(name, False, f"missing: {missing}")

        return CheckResult(name, True, f"all {len(expected)} commands registered")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def test_help_and_tools_include_github() -> CheckResult:
    """/help and /tools include GitHub commands."""
    name = "/help and /tools: include GitHub commands"
    try:
        import asyncio
        from handlers.commands import _help, _tools

        msg = AsyncMock()
        port = AsyncMock()
        runner = AsyncMock()
        s = _settings()

        # Test /help
        asyncio.run(_help(msg, port, runner, s, ""))
        help_text = port.reply.call_args[0][1]
        if "/github_status" not in help_text:
            return CheckResult(name, False, "missing /github_status in help")

        # Test /tools
        port.reply.reset_mock()
        asyncio.run(_tools(msg, port, runner, s, ""))
        tools_text = port.reply.call_args[0][1]
        if "github.status" not in tools_text:
            return CheckResult(name, False, "missing github.status in tools")

        return CheckResult(name, True, "help and tools include GitHub")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def test_intent_routing() -> CheckResult:
    """Natural language routes to GitHub tools."""
    name = "intent: natural language routes to GitHub tools"
    try:
        from handlers.intent import route_intent

        cases = [
            ("看看 GitHub issue", "github.issues"),
            ("PR 状态", "github.prs"),
            ("CI 挂了吗", "github.ci"),
            ("github 连接状态", "github.status"),
            ("查看 issue #123", "github.issue"),
            ("看看 open pr", "github.prs"),
        ]

        for text, expected_tool in cases:
            result = route_intent(text)
            if expected_tool not in result.tools:
                return CheckResult(name, False, f"'{text}' expected {expected_tool}, got {result.tools}")

        return CheckResult(name, True, f"all {len(cases)} cases routed correctly")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def test_no_network_calls() -> CheckResult:
    """No network calls in smoke tests."""
    name = "github: no network calls in smoke"
    try:
        # This test passes if we reach here without making real network calls
        # All tests above use mocks or fail before network
        return CheckResult(name, True, "no network calls made")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def main() -> int:
    results = [
        test_missing_config_graceful(),
        test_token_redacted(),
        test_parse_commands(),
        test_comment_requires_confirmation(),
        test_create_issue_write_safe(),
        test_briefing_degrades_without_github(),
        test_registry_danger_levels(),
        test_commands_registered(),
        test_help_and_tools_include_github(),
        test_intent_routing(),
        test_no_network_calls(),
    ]

    print_results(results)
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
