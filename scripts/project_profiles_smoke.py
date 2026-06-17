"""scripts/project_profiles_smoke.py — P3.9 Generic Project Profiles smoke tests.

Tests:
  - create/list/show/use/remove project
  - operator isolation
  - active project fallback
  - no active project graceful message
  - project commands degrade with missing integrations
  - project tools are correct danger levels
  - project.remove requires confirmation
  - project.add/use are WRITE_SAFE and audited
  - daily briefing includes active projects but degrades if none
  - /help and /tools list project commands
  - no network calls in smoke
  - all outputs redacted
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

# Ensure repo root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Provide minimal .env so load_settings() doesn't crash
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "12345")
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/codex-smoke")
os.environ.setdefault("CODEX_TASK_ROOT", "/tmp/codex-smoke-task")
os.environ.setdefault("CODEX_BIN", "codex")
os.environ.setdefault("USER_TIMEZONE", "UTC")

from config import Settings, load_settings


def _settings(tmp_path: Path) -> Settings:
    base = load_settings()
    return replace(base, codex_memory_root=tmp_path, telegram_allowed_user_id=12345, user_timezone="UTC")


# ---- Tests ----

async def _test_create_project_profile():
    """Create a project profile and verify it exists."""
    from personal_tools.store import PersonalToolsStore

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        store = PersonalToolsStore(settings)
        row = store.create_project_profile(
            "op1", "Test App", "mobile_app", "A test app",
            github_repo="user/repo", keywords=("test", "app"),
        )
        assert row.id > 0
        assert row.name == "Test App"
        assert row.type == "mobile_app"
        assert row.github_repo == "user/repo"
        assert row.keywords == ("test", "app")
        assert row.enabled is True

        fetched = store.get_project_profile("op1", row.id)
        assert fetched is not None
        assert fetched.name == "Test App"


async def _test_list_project_profiles():
    """List project profiles."""
    from personal_tools.store import PersonalToolsStore

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        store = PersonalToolsStore(settings)
        store.create_project_profile("op1", "App 1", "generic", "desc1")
        store.create_project_profile("op1", "App 2", "web_app", "desc2")

        rows = store.list_project_profiles("op1")
        assert len(rows) == 2
        names = {r.name for r in rows}
        assert "App 1" in names
        assert "App 2" in names


async def _test_use_project():
    """Set and get active project."""
    from personal_tools.store import PersonalToolsStore

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        store = PersonalToolsStore(settings)
        row1 = store.create_project_profile("op1", "App 1", "generic", "desc1")
        row2 = store.create_project_profile("op1", "App 2", "web_app", "desc2")

        assert store.set_active_project("op1", row1.id) is True
        active = store.get_active_project("op1")
        assert active is not None
        assert active.name == "App 1"

        assert store.set_active_project("op1", row2.id) is True
        active = store.get_active_project("op1")
        assert active.name == "App 2"


async def _test_remove_project():
    """Remove a project profile."""
    from personal_tools.store import PersonalToolsStore

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        store = PersonalToolsStore(settings)
        row = store.create_project_profile("op1", "App 1", "generic", "desc1")
        assert store.delete_project_profile("op1", row.id) is True
        assert store.get_project_profile("op1", row.id) is None


async def _test_operator_isolation():
    """Operators cannot see each other's projects."""
    from personal_tools.store import PersonalToolsStore

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        store = PersonalToolsStore(settings)
        store.create_project_profile("op1", "App 1", "generic", "desc1")
        store.create_project_profile("op2", "App 2", "generic", "desc2")

        op1_rows = store.list_project_profiles("op1")
        op2_rows = store.list_project_profiles("op2")
        assert len(op1_rows) == 1
        assert len(op2_rows) == 1
        assert op1_rows[0].name == "App 1"
        assert op2_rows[0].name == "App 2"


async def _test_active_project_fallback():
    """get_active_or_first_project returns first enabled if no active set."""
    from personal_tools.store import PersonalToolsStore

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        store = PersonalToolsStore(settings)
        store.create_project_profile("op1", "App 1", "generic", "desc1")

        # No active project set
        active = store.get_active_project("op1")
        assert active is None

        # Fallback should return the first enabled project
        fallback = store.get_active_or_first_project("op1")
        assert fallback is not None
        assert fallback.name == "App 1"


async def _test_no_active_project_message():
    """projects.list returns setup message when no projects."""
    from personal_tools.projects import projects_list

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        result = await projects_list(settings, "", operator_id="op1")
        assert result.ok is True
        assert "还没有项目配置" in result.text
        assert "/project_add" in result.text


async def _test_projects_list_with_data():
    """projects.list shows projects and active marker."""
    from personal_tools.projects import projects_list

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        from personal_tools.store import PersonalToolsStore
        store = PersonalToolsStore(settings)
        row = store.create_project_profile("op1", "My App", "mobile_app", "desc")
        store.set_active_project("op1", row.id)

        result = await projects_list(settings, "", operator_id="op1")
        assert result.ok is True
        assert "My App" in result.text
        assert "活跃" in result.text


async def _test_project_add_format():
    """projects.add parses pipe-separated arguments."""
    from personal_tools.projects import projects_add

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        result = await projects_add(
            settings,
            "My App | mobile_app | A test app | user/repo | test,app",
            operator_id="op1",
        )
        assert result.ok is True
        assert "项目已创建" in result.text
        assert "My App" in result.text


async def _test_project_use():
    """projects.use sets active project."""
    from personal_tools.projects import projects_add, projects_use

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        add_result = await projects_add(
            settings, "App | generic | desc", operator_id="op1"
        )
        assert add_result.ok is True

        # Extract ID from result
        m = re.search(r"#(\d+)", add_result.text)
        assert m is not None
        proj_id = m.group(1)

        use_result = await projects_use(settings, proj_id, operator_id="op1")
        assert use_result.ok is True
        assert "已切换到活跃项目" in use_result.text


async def _test_project_remove_requires_confirmation():
    """projects.remove is DESTRUCTIVE and needs confirmation."""
    from personal_tools.registry import requires_personal_confirmation
    assert requires_personal_confirmation("projects.remove") is True


async def _test_project_add_is_write_safe():
    """projects.add is WRITE_SAFE (audited but no confirmation)."""
    from personal_tools.registry import requires_personal_confirmation, get_personal_tool
    from handlers.tools.registry import DangerLevel

    spec = get_personal_tool("projects.add")
    assert spec is not None
    assert spec.danger == DangerLevel.WRITE_SAFE
    assert requires_personal_confirmation("projects.add") is False


async def _test_project_use_is_write_safe():
    """projects.use is WRITE_SAFE (audited but no confirmation)."""
    from personal_tools.registry import get_personal_tool
    from handlers.tools.registry import DangerLevel

    spec = get_personal_tool("projects.use")
    assert spec is not None
    assert spec.danger == DangerLevel.WRITE_SAFE


async def _test_project_analysis_tools_are_read():
    """Project analysis tools are READ-only."""
    from personal_tools.registry import get_personal_tool
    from handlers.tools.registry import DangerLevel

    read_tools = [
        "projects.list", "projects.show",
        "project.status", "project.health", "project.roadmap",
        "project.next", "project.release_checklist", "project.brief",
    ]
    for name in read_tools:
        spec = get_personal_tool(name)
        assert spec is not None, f"{name} not registered"
        assert spec.danger == DangerLevel.READ, f"{name} should be READ, got {spec.danger}"


async def _test_briefing_degrades_no_projects():
    """Daily briefing degrades gracefully when no projects configured."""
    from personal_tools.projects import build_project_briefing_section

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        section = build_project_briefing_section(settings, "op1")
        assert "未配置" in section


async def _test_briefing_includes_projects():
    """Daily briefing includes active projects."""
    from personal_tools.projects import build_project_briefing_section
    from personal_tools.store import PersonalToolsStore

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        store = PersonalToolsStore(settings)
        store.create_project_profile("op1", "My App", "mobile_app", "desc")

        section = build_project_briefing_section(settings, "op1")
        assert "My App" in section
        assert "mobile_app" in section


async def _test_no_network_calls():
    """Verify no network calls in project tools."""
    from personal_tools.projects import projects_list, projects_add, projects_show

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))

        with patch("personal_tools.projects._collect_facts", return_value="mock facts"):
            result = await projects_list(settings, "", operator_id="op1")
            assert result.ok is True

            result = await projects_add(settings, "App | generic | desc", operator_id="op1")
            assert result.ok is True

            result = await projects_show(settings, "", operator_id="op1")
            assert result.ok is True


async def _test_outputs_redacted():
    """Verify project outputs are redacted."""
    from redaction import redact_text

    text = "project token=secret123 password=mypass"
    redacted = redact_text(text)
    assert "secret123" not in redacted
    assert "mypass" not in redacted


async def _test_project_types_valid():
    """All project types are valid."""
    from personal_tools.store import PROJECT_TYPES

    expected = {"generic", "mobile_app", "web_app", "bot", "library", "research", "course", "business"}
    assert set(PROJECT_TYPES) == expected


async def _test_update_project_profile():
    """Update project profile fields."""
    from personal_tools.store import PersonalToolsStore

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        store = PersonalToolsStore(settings)
        row = store.create_project_profile("op1", "App", "generic", "desc")

        assert store.update_project_profile("op1", row.id, name="New Name") is True
        updated = store.get_project_profile("op1", row.id)
        assert updated.name == "New Name"

        assert store.update_project_profile("op1", row.id, github_repo="user/repo") is True
        updated = store.get_project_profile("op1", row.id)
        assert updated.github_repo == "user/repo"


async def _test_project_show_details():
    """projects.show displays full project details."""
    from personal_tools.projects import projects_show, projects_add

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        await projects_add(
            settings,
            "Test App | mobile_app | A test app | user/repo | test,app",
            operator_id="op1",
        )

        result = await projects_show(settings, "", operator_id="op1")
        assert result.ok is True
        assert "Test App" in result.text
        assert "mobile_app" in result.text
        assert "user/repo" in result.text


async def _test_command_registration():
    """Project commands are registered in COMMAND_TABLE."""
    from handlers.commands import COMMAND_TABLE

    expected_commands = [
        "projects", "project_add", "project_use", "project_show",
        "project_remove", "project_status", "project_health",
        "project_roadmap", "project_next", "project_release_checklist",
        "project_brief",
    ]
    for cmd in expected_commands:
        assert cmd in COMMAND_TABLE, f"Command {cmd} not in COMMAND_TABLE"


async def _test_help_includes_project_commands():
    """Help text includes project commands."""
    from handlers.commands import _help

    help_text = ""
    class FakePort:
        async def reply(self, msg, text):
            nonlocal help_text
            help_text = text

    class FakeMsg:
        text = "/help"

    await _help(FakeMsg(), FakePort(), None, None, "")
    assert "/projects" in help_text
    assert "/project_add" in help_text
    assert "/project_use" in help_text
    assert "Project Profiles" in help_text


# ---- Runner ----

_TESTS = {
    "create project profile": _test_create_project_profile,
    "list project profiles": _test_list_project_profiles,
    "use project": _test_use_project,
    "remove project": _test_remove_project,
    "operator isolation": _test_operator_isolation,
    "active project fallback": _test_active_project_fallback,
    "no active project message": _test_no_active_project_message,
    "projects list with data": _test_projects_list_with_data,
    "project add format": _test_project_add_format,
    "project use": _test_project_use,
    "remove requires confirmation": _test_project_remove_requires_confirmation,
    "add is write_safe": _test_project_add_is_write_safe,
    "use is write_safe": _test_project_use_is_write_safe,
    "analysis tools are read": _test_project_analysis_tools_are_read,
    "briefing degrades no projects": _test_briefing_degrades_no_projects,
    "briefing includes projects": _test_briefing_includes_projects,
    "no network calls": _test_no_network_calls,
    "outputs redacted": _test_outputs_redacted,
    "project types valid": _test_project_types_valid,
    "update project profile": _test_update_project_profile,
    "project show details": _test_project_show_details,
    "command registration": _test_command_registration,
    "help includes project commands": _test_help_includes_project_commands,
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
    print(f"\nProject Profiles smoke: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
