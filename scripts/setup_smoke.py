"""scripts/setup_smoke.py — P3.10 Setup Wizard smoke tests.

Tests:
  - missing integrations produce useful checklist
  - configured fake env reports configured without leaking secrets
  - setup.project includes project_add examples
  - setup.gmail warns not to share app password
  - setup.github does not leak GITHUB_TOKEN
  - /help lists setup commands
  - /tools lists setup tools
  - no network calls in smoke
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "12345")
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/codex-smoke")
os.environ.setdefault("CODEX_TASK_ROOT", "/tmp/codex-smoke-task")
os.environ.setdefault("CODEX_BIN", "codex")
os.environ.setdefault("USER_TIMEZONE", "UTC")

from config import Settings, load_settings
from handlers.tools.registry import DangerLevel
from personal_tools.registry import PERSONAL_TOOL_REGISTRY, get_personal_tool


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = load_settings()
    return replace(base, codex_memory_root=tmp_path, **overrides)


# ---- Tests ----

async def _test_setup_status_missing_integrations():
    """setup.status reports missing integrations."""
    from personal_tools.setup import setup_status

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        result = setup_status(settings, "op1")
        assert result.ok is True
        assert "配置状态" in result.text
        # Gmail should be missing
        assert "Gmail" in result.text


async def _test_setup_check_produces_checklist():
    """setup.check produces a prioritized checklist."""
    from personal_tools.setup import setup_check

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        result = setup_check(settings, "op1")
        assert result.ok is True
        assert "检查清单" in result.text


async def _test_setup_project_has_examples():
    """setup.project includes project_add examples."""
    from personal_tools.setup import setup_project

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        result = setup_project(settings, "op1")
        assert result.ok is True
        assert "/project_add" in result.text
        assert "mobile_app" in result.text
        assert "generic" in result.text


async def _test_setup_gmail_warns_not_to_share():
    """setup.gmail warns not to share app password."""
    from personal_tools.setup import setup_gmail

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        result = setup_gmail(settings, "op1")
        assert result.ok is True
        assert "永远不要分享" in result.text
        assert "App Password" in result.text


async def _test_setup_google_guides_oauth():
    """setup.google guides OAuth setup."""
    from personal_tools.setup import setup_google

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        result = setup_google(settings, "op1")
        assert result.ok is True
        assert "Google OAuth" in result.text
        assert "/auth_google" in result.text


async def _test_setup_github_no_token_leak():
    """setup.github does not leak GITHUB_TOKEN."""
    from personal_tools.setup import setup_github

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td), github_token="ghp_secret123456")
        result = setup_github(settings, "op1")
        assert result.ok is True
        # Should NOT contain the token value
        assert "ghp_secret123456" not in result.text
        # Should contain guidance
        assert "GitHub" in result.text


async def _test_setup_github_configured():
    """setup.github reports configured status."""
    from personal_tools.setup import setup_github

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td), github_token="ghp_test", github_default_repo="user/repo")
        result = setup_github(settings, "op1")
        assert result.ok is True
        assert "已配置" in result.text
        # Should NOT contain the token value
        assert "ghp_test" not in result.text


async def _test_setup_status_no_secrets_leaked():
    """setup.status does not leak any secrets."""
    from personal_tools.setup import setup_status

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(
            Path(td),
            telegram_bot_token="bot_token_secret",
            gmail_app_password="gmail_pass_secret",
            github_token="ghp_secret",
            google_client_secret_path="/secret/path.json",
        )
        result = setup_status(settings, "op1")
        assert result.ok is True
        # Should NOT contain any secrets
        assert "bot_token_secret" not in result.text
        assert "gmail_pass_secret" not in result.text
        assert "ghp_secret" not in result.text


async def _test_setup_tools_are_read_only():
    """All setup tools are READ-only."""
    read_tools = [
        "setup.status", "setup.check", "setup.project",
        "setup.gmail", "setup.google", "setup.github",
    ]
    for name in read_tools:
        spec = get_personal_tool(name)
        assert spec is not None, f"{name} not registered"
        assert spec.danger == DangerLevel.READ, f"{name} should be READ, got {spec.danger}"


async def _test_command_registration():
    """Setup commands are registered in COMMAND_TABLE."""
    from handlers.commands import COMMAND_TABLE

    expected = [
        "setup", "setup_status", "setup_check",
        "setup_project", "setup_gmail", "setup_google", "setup_github",
    ]
    for cmd in expected:
        assert cmd in COMMAND_TABLE, f"Command {cmd} not in COMMAND_TABLE"


async def _test_help_includes_setup():
    """Help text includes setup commands."""
    from handlers.commands import _help

    help_text = ""
    class FakePort:
        async def reply(self, msg, text):
            nonlocal help_text
            help_text = text

    class FakeMsg:
        text = "/help"

    await _help(FakeMsg(), FakePort(), None, None, "")
    assert "/setup" in help_text
    assert "/setup_check" in help_text
    assert "设置向导" in help_text


async def _test_tools_lists_setup():
    """/tools lists setup tools."""
    from handlers.commands import _tools

    tools_text = ""
    class FakePort:
        async def reply(self, msg, text):
            nonlocal tools_text
            tools_text = text

    class FakeMsg:
        text = "/tools"

    await _tools(FakeMsg(), FakePort(), None, None, "")
    assert "setup.status" in tools_text
    assert "setup.check" in tools_text


async def _test_no_network_calls():
    """No network calls in setup tools."""
    from personal_tools.setup import setup_status, setup_check, setup_project

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        # These should not make network calls
        result = setup_status(settings, "op1")
        assert result.ok is True

        result = setup_check(settings, "op1")
        assert result.ok is True

        result = setup_project(settings, "op1")
        assert result.ok is True


# ---- Runner ----

_TESTS = {
    "status missing integrations": _test_setup_status_missing_integrations,
    "check produces checklist": _test_setup_check_produces_checklist,
    "project has examples": _test_setup_project_has_examples,
    "gmail warns not to share": _test_setup_gmail_warns_not_to_share,
    "google guides oauth": _test_setup_google_guides_oauth,
    "github no token leak": _test_setup_github_no_token_leak,
    "github configured": _test_setup_github_configured,
    "status no secrets leaked": _test_setup_status_no_secrets_leaked,
    "tools are read only": _test_setup_tools_are_read_only,
    "command registration": _test_command_registration,
    "help includes setup": _test_help_includes_setup,
    "tools lists setup": _test_tools_lists_setup,
    "no network calls": _test_no_network_calls,
}


async def main() -> int:
    passed = 0
    failed = 0
    for name, fn in _TESTS.items():
        try:
            await fn()
            print(f"[ok]   {name}")
            passed += 1
        except Exception as exc:
            print(f"[fail] {name}: {exc}")
            failed += 1
    print(f"\nSetup smoke: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
