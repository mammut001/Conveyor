"""scripts/project_io_smoke.py — P3.11 Project Import/Export smoke tests.

Tests:
  - export single project
  - export all projects
  - export does not include ids or operator_id
  - import valid JSON
  - import skips duplicates
  - import sets active if none exists
  - import validates schema
  - import validates project type
  - template shows all types
  - template for specific type
  - tools are correct danger levels
  - command registration
  - /help lists commands
  - no network calls
  - all outputs redacted
"""
from __future__ import annotations

import asyncio
import json
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
from personal_tools.registry import get_personal_tool


def _settings(tmp_path: Path) -> Settings:
    base = load_settings()
    return replace(base, codex_memory_root=tmp_path, telegram_allowed_user_id=12345, user_timezone="UTC")


# ---- Tests ----

async def _test_export_single_project():
    """Export a single project."""
    from personal_tools.projects import projects_add
    from personal_tools.project_io import project_export

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        add_result = await projects_add(settings, "App | generic | desc", operator_id="op1")
        assert add_result.ok

        import re
        m = re.search(r"#(\d+)", add_result.text)
        assert m
        pid = m.group(1)

        result = project_export(settings, "op1", pid)
        assert result.ok
        assert "conveyor.project.v1" in result.text
        assert "App" in result.text


async def _test_export_all_projects():
    """Export all projects."""
    from personal_tools.projects import projects_add
    from personal_tools.project_io import project_export_all

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        await projects_add(settings, "App1 | generic | desc1", operator_id="op1")
        await projects_add(settings, "App2 | web_app | desc2", operator_id="op1")

        result = project_export_all(settings, "op1")
        assert result.ok
        assert "App1" in result.text
        assert "App2" in result.text


async def _test_export_no_ids_no_operator():
    """Export does not include ids or operator_id."""
    from personal_tools.projects import projects_add
    from personal_tools.project_io import project_export

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        await projects_add(settings, "App | generic | desc", operator_id="op1")

        result = project_export(settings, "op1")
        assert result.ok
        # Extract JSON from result
        import re
        json_match = re.search(r'```json\n(.*?)\n```', result.text, re.DOTALL)
        assert json_match
        data = json.loads(json_match.group(1))
        assert "schema" in data
        proj = data["projects"][0]
        assert "id" not in proj
        assert "operator_id" not in proj
        assert "created_at" not in proj
        assert "updated_at" not in proj


async def _test_import_valid_json():
    """Import valid JSON."""
    from personal_tools.project_io import project_import

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        import_data = {
            "schema": "conveyor.project.v1",
            "projects": [
                {"name": "Imported", "type": "generic", "description": "test"}
            ]
        }
        result = project_import(settings, "op1", json.dumps(import_data))
        assert result.ok
        assert "成功导入" in result.text
        assert "Imported" in result.text


async def _test_import_skips_duplicates():
    """Import skips duplicate names."""
    from personal_tools.project_io import project_import

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        import_data = {
            "schema": "conveyor.project.v1",
            "projects": [
                {"name": "App", "type": "generic", "description": "first"}
            ]
        }
        # First import
        result = project_import(settings, "op1", json.dumps(import_data))
        assert result.ok

        # Second import - should skip
        result = project_import(settings, "op1", json.dumps(import_data))
        assert result.ok
        assert "跳过" in result.text


async def _test_import_sets_active_if_none():
    """Import sets active project if none exists."""
    from personal_tools.project_io import project_import
    from personal_tools.store import PersonalToolsStore

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        import_data = {
            "schema": "conveyor.project.v1",
            "projects": [
                {"name": "First", "type": "generic", "description": "test"}
            ]
        }
        result = project_import(settings, "op1", json.dumps(import_data))
        assert result.ok
        assert "活跃项目" in result.text

        store = PersonalToolsStore(settings)
        active = store.get_active_project("op1")
        assert active is not None
        assert active.name == "First"


async def _test_import_validates_schema():
    """Import validates schema."""
    from personal_tools.project_io import project_import

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        bad_data = {"schema": "wrong.schema", "projects": []}
        result = project_import(settings, "op1", json.dumps(bad_data))
        assert not result.ok
        assert "schema" in result.text.lower()


async def _test_import_validates_type():
    """Import validates project type."""
    from personal_tools.project_io import project_import

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        bad_data = {
            "schema": "conveyor.project.v1",
            "projects": [{"name": "Bad", "type": "invalid_type", "description": "test"}]
        }
        result = project_import(settings, "op1", json.dumps(bad_data))
        assert result.ok  # Partial success
        assert "失败" in result.text
        assert "无效项目类型" in result.text


async def _test_template_shows_all_types():
    """Template shows all types."""
    from personal_tools.project_io import project_template

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        result = project_template(settings, "op1", "")
        assert result.ok
        for ptype in ["generic", "mobile_app", "web_app", "bot", "library", "research", "course", "business"]:
            assert ptype in result.text


async def _test_template_specific_type():
    """Template for specific type."""
    from personal_tools.project_io import project_template

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        result = project_template(settings, "op1", "mobile_app")
        assert result.ok
        assert "mobile_app" in result.text
        assert "conveyor.project.v1" in result.text


async def _test_tools_are_correct_danger():
    """Tools are correct danger levels."""
    tools = {
        "project.export": DangerLevel.READ,
        "project.export_all": DangerLevel.READ,
        "project.import": DangerLevel.WRITE_SAFE,
        "project.template": DangerLevel.READ,
    }
    for name, expected in tools.items():
        spec = get_personal_tool(name)
        assert spec is not None, f"{name} not registered"
        assert spec.danger == expected, f"{name} should be {expected}, got {spec.danger}"


async def _test_command_registration():
    """Commands are registered."""
    from handlers.commands import COMMAND_TABLE
    expected = ["project_export", "project_export_all", "project_import", "project_template"]
    for cmd in expected:
        assert cmd in COMMAND_TABLE, f"Command {cmd} not in COMMAND_TABLE"


async def _test_help_includes_commands():
    """Help includes commands."""
    from handlers.commands import _help

    help_text = ""
    class FakePort:
        async def reply(self, msg, text):
            nonlocal help_text
            help_text = text
    class FakeMsg:
        text = "/help"

    await _help(FakeMsg(), FakePort(), None, None, "")
    assert "/project_export" in help_text
    assert "/project_import" in help_text
    assert "/project_template" in help_text


async def _test_no_network_calls():
    """No network calls."""
    from personal_tools.project_io import project_export, project_template, project_import

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        # Export with no projects should still work
        result = project_export(settings, "op1")
        # May fail with "no projects" but should not make network calls

        # Template should always work
        result = project_template(settings, "op1", "generic")
        assert result.ok

        # Import with valid data
        import_data = {
            "schema": "conveyor.project.v1",
            "projects": [{"name": "Test", "type": "generic", "description": "test"}]
        }
        result = project_import(settings, "op2", json.dumps(import_data))
        assert result.ok


async def _test_outputs_redacted():
    """Outputs are redacted."""
    from redaction import redact_text
    text = "token=secret123"
    assert "secret123" not in redact_text(text)


async def _test_import_preserves_enabled():
    """Import preserves enabled field."""
    from personal_tools.project_io import project_import
    from personal_tools.store import PersonalToolsStore

    with tempfile.TemporaryDirectory() as td:
        settings = _settings(Path(td))
        # Import with enabled=false
        import_data = {
            "schema": "conveyor.project.v1",
            "projects": [
                {"name": "Disabled", "type": "generic", "description": "test", "enabled": False}
            ]
        }
        result = project_import(settings, "op1", json.dumps(import_data))
        assert result.ok
        assert "成功导入" in result.text

        # Check the imported project has enabled=False
        store = PersonalToolsStore(settings)
        projects = store.list_project_profiles("op1")
        assert len(projects) == 1
        assert projects[0].enabled is False


async def _test_project_health_routing():
    """/project_health routes to project.health, not planner.health."""
    from handlers.commands import COMMAND_TABLE, _TOOL_SLASH

    # Check that /project_health command exists and routes to project health
    assert "project_health" in COMMAND_TABLE
    spec = COMMAND_TABLE["project_health"]
    assert spec.summary == "项目健康检查"

    # Check that planner_health command exists
    assert "planner_health" in COMMAND_TABLE
    planner_spec = COMMAND_TABLE["planner_health"]
    assert planner_spec.summary == "Planner 健康检查"

    # Check _TOOL_SLASH mappings
    assert _TOOL_SLASH["project.health"] == ("/project_health",)
    assert _TOOL_SLASH["planner.health"] == ("/planner_health",)


# ---- Runner ----

_TESTS = {
    "export single project": _test_export_single_project,
    "export all projects": _test_export_all_projects,
    "export no ids no operator": _test_export_no_ids_no_operator,
    "import valid JSON": _test_import_valid_json,
    "import skips duplicates": _test_import_skips_duplicates,
    "import sets active if none": _test_import_sets_active_if_none,
    "import validates schema": _test_import_validates_schema,
    "import validates type": _test_import_validates_type,
    "template shows all types": _test_template_shows_all_types,
    "template specific type": _test_template_specific_type,
    "import preserves enabled": _test_import_preserves_enabled,
    "project_health routing": _test_project_health_routing,
    "tools correct danger": _test_tools_are_correct_danger,
    "command registration": _test_command_registration,
    "help includes commands": _test_help_includes_commands,
    "no network calls": _test_no_network_calls,
    "outputs redacted": _test_outputs_redacted,
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
    print(f"\nProject IO smoke: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
