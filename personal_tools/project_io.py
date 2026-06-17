"""personal_tools/project_io.py — Project Import/Export/Template for Conveyor (P3.11).

Makes Generic Project Profiles portable and easier to set up.
All operations are READ-only except import which is WRITE_SAFE.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from config import Settings
from personal_tools.base import ToolResult
from personal_tools.store import PersonalToolsStore, PROJECT_TYPES, ProjectProfileRow
from redaction import redact_text, truncate

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

EXPORT_SCHEMA = "conveyor.project.v1"

# Project templates by type
_PROJECT_TEMPLATES: dict[str, dict] = {
    "generic": {
        "name": "My Project",
        "type": "generic",
        "description": "A generic project",
        "github_repo": "",
        "appstore_url": "",
        "keywords": [],
        "notes_query": "",
        "gmail_query": "",
        "default_branch": "",
        "enabled": True,
    },
    "mobile_app": {
        "name": "My Mobile App",
        "type": "mobile_app",
        "description": "iOS/Android mobile application",
        "github_repo": "owner/repo",
        "appstore_url": "",
        "keywords": ["mobile", "ios", "android"],
        "notes_query": "app",
        "gmail_query": "app release",
        "default_branch": "main",
        "enabled": True,
    },
    "web_app": {
        "name": "My Web App",
        "type": "web_app",
        "description": "Web application",
        "github_repo": "owner/repo",
        "appstore_url": "",
        "keywords": ["web", "frontend", "backend"],
        "notes_query": "webapp",
        "gmail_query": "deploy",
        "default_branch": "main",
        "enabled": True,
    },
    "bot": {
        "name": "My Bot",
        "type": "bot",
        "description": "Telegram/Discord/Slack bot",
        "github_repo": "owner/bot-repo",
        "appstore_url": "",
        "keywords": ["bot", "telegram", "automation"],
        "notes_query": "bot",
        "gmail_query": "bot",
        "default_branch": "main",
        "enabled": True,
    },
    "library": {
        "name": "My Library",
        "type": "library",
        "description": "Open source library/SDK",
        "github_repo": "owner/lib",
        "appstore_url": "",
        "keywords": ["library", "sdk", "api"],
        "notes_query": "library",
        "gmail_query": "release",
        "default_branch": "main",
        "enabled": True,
    },
    "research": {
        "name": "Research Project",
        "type": "research",
        "description": "Research or academic project",
        "github_repo": "",
        "appstore_url": "",
        "keywords": ["research", "paper", "study"],
        "notes_query": "research",
        "gmail_query": "",
        "default_branch": "",
        "enabled": True,
    },
    "course": {
        "name": "My Course",
        "type": "course",
        "description": "Online course or learning project",
        "github_repo": "",
        "appstore_url": "",
        "keywords": ["course", "learning", "study"],
        "notes_query": "course",
        "gmail_query": "",
        "default_branch": "",
        "enabled": True,
    },
    "business": {
        "name": "My Business",
        "type": "business",
        "description": "Business or client project",
        "github_repo": "",
        "appstore_url": "",
        "keywords": ["business", "client"],
        "notes_query": "business",
        "gmail_query": "client",
        "default_branch": "",
        "enabled": True,
    },
}


def _project_to_export(row: ProjectProfileRow) -> dict:
    """Convert a project profile to export format (no secrets, no ids)."""
    return {
        "name": row.name,
        "type": row.type,
        "description": row.description,
        "github_repo": row.github_repo,
        "appstore_url": row.appstore_url,
        "keywords": list(row.keywords),
        "notes_query": row.notes_query,
        "gmail_query": row.gmail_query,
        "default_branch": row.default_branch,
        "enabled": row.enabled,
    }


def _validate_import_project(proj: dict) -> tuple[bool, str]:
    """Validate a single project in import data."""
    required = ["name", "type", "description"]
    for field in required:
        if field not in proj:
            return False, f"缺少必需字段: {field}"

    if proj["type"] not in PROJECT_TYPES:
        return False, f"无效项目类型: {proj['type']}，支持: {', '.join(PROJECT_TYPES)}"

    if not isinstance(proj.get("name", ""), str) or not proj["name"].strip():
        return False, "项目名称不能为空"

    # Optional fields type checks
    if "keywords" in proj and not isinstance(proj["keywords"], list):
        return False, "keywords 必须是数组"

    for str_field in ["github_repo", "appstore_url", "notes_query", "gmail_query", "default_branch"]:
        if str_field in proj and not isinstance(proj[str_field], str):
            return False, f"{str_field} 必须是字符串"

    if "enabled" in proj and not isinstance(proj["enabled"], bool):
        return False, "enabled 必须是布尔值"

    return True, ""


def project_export(settings: Settings, operator_id: str, project_id: str = "") -> ToolResult:
    """Export a single project or all projects to JSON."""
    store = PersonalToolsStore(settings)

    if project_id.strip():
        # Export single project
        try:
            pid = int(project_id.strip())
        except ValueError:
            return ToolResult(ok=False, text=f"⚠️ 无效项目 ID: {project_id}")

        proj = store.get_project_profile(operator_id, pid)
        if not proj:
            return ToolResult(ok=False, text=f"⚠️ 项目 #{pid} 不存在")

        export_data = {
            "schema": EXPORT_SCHEMA,
            "projects": [_project_to_export(proj)],
        }
    else:
        # Export all projects
        projects = store.list_project_profiles(operator_id)
        if not projects:
            return ToolResult(ok=False, text="⚠️ 没有可导出的项目")

        export_data = {
            "schema": EXPORT_SCHEMA,
            "projects": [_project_to_export(p) for p in projects],
        }

    json_str = json.dumps(export_data, ensure_ascii=False, indent=2)
    return ToolResult(ok=True, text=f"📤 项目导出:\n\n```json\n{json_str}\n```\n\n使用 /project_import 导入此 JSON。")


def project_export_all(settings: Settings, operator_id: str) -> ToolResult:
    """Export all projects to JSON."""
    return project_export(settings, operator_id, "")


def project_import(settings: Settings, operator_id: str, json_str: str) -> ToolResult:
    """Import projects from JSON."""
    json_str = json_str.strip()
    if not json_str:
        return ToolResult(ok=False, text="⚠️ 用法: /project_import <JSON>")

    # Parse JSON
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        return ToolResult(ok=False, text=f"⚠️ JSON 解析失败: {exc}")

    # Validate schema
    if not isinstance(data, dict):
        return ToolResult(ok=False, text="⚠️ JSON 必须是对象")

    schema = data.get("schema", "")
    if schema != EXPORT_SCHEMA:
        return ToolResult(ok=False, text=f"⚠️ 无效 schema: {schema}，期望: {EXPORT_SCHEMA}")

    projects_data = data.get("projects", [])
    if not isinstance(projects_data, list) or not projects_data:
        return ToolResult(ok=False, text="⚠️ projects 数组为空或格式错误")

    store = PersonalToolsStore(settings)
    existing_names = {p.name for p in store.list_project_profiles(operator_id)}

    imported = []
    skipped = []
    errors = []

    for proj in projects_data:
        # Validate
        valid, err = _validate_import_project(proj)
        if not valid:
            errors.append(f"  ❌ {proj.get('name', '?')}: {err}")
            continue

        name = proj["name"].strip()

        # Check duplicate
        if name in existing_names:
            skipped.append(f"  ⏭️ {name}: 已存在，跳过")
            continue

        # Import
        try:
            keywords = tuple(proj.get("keywords", []))
            row = store.create_project_profile(
                operator_id=operator_id,
                name=name,
                project_type=proj["type"],
                description=proj.get("description", ""),
                github_repo=proj.get("github_repo", ""),
                appstore_url=proj.get("appstore_url", ""),
                keywords=keywords,
                notes_query=proj.get("notes_query", ""),
                gmail_query=proj.get("gmail_query", ""),
                default_branch=proj.get("default_branch", ""),
                enabled=proj.get("enabled", True),
            )
            imported.append(f"  ✅ #{row.id} {name} [{proj['type']}]")
            existing_names.add(name)

            # Set as active if no active project
            active = store.get_active_project(operator_id)
            if active is None:
                store.set_active_project(operator_id, row.id)
                imported.append(f"     👈 已设为活跃项目")
        except Exception as exc:
            errors.append(f"  ❌ {name}: {redact_text(str(exc))}")

    # Build result
    lines = ["📥 项目导入结果:", ""]
    if imported:
        lines.append(f"成功导入 ({len(imported)} 个):")
        lines.extend(imported)
        lines.append("")
    if skipped:
        lines.append(f"跳过 ({len(skipped)} 个):")
        lines.extend(skipped)
        lines.append("")
    if errors:
        lines.append(f"失败 ({len(errors)} 个):")
        lines.extend(errors)
        lines.append("")

    if not imported and not skipped:
        lines.append("没有导入任何项目。")

    return ToolResult(ok=True, text="\n".join(lines))


def project_template(settings: Settings, operator_id: str, project_type: str = "") -> ToolResult:
    """Show a project template for a given type."""
    project_type = project_type.strip().lower()

    if not project_type:
        # Show all available templates
        lines = ["📋 项目模板", "", "使用 /project_template <类型> 查看模板:", ""]
        for t in PROJECT_TYPES:
            tmpl = _PROJECT_TEMPLATES.get(t, {})
            desc = tmpl.get("description", "")
            lines.append(f"  • {t} — {desc}")
        lines.append("")
        lines.append("示例: /project_template mobile_app")
        return ToolResult(ok=True, text="\n".join(lines))

    if project_type not in PROJECT_TYPES:
        return ToolResult(ok=False, text=f"⚠️ 无效类型: {project_type}，支持: {', '.join(PROJECT_TYPES)}")

    tmpl = _PROJECT_TEMPLATES.get(project_type, _PROJECT_TEMPLATES["generic"])

    # Create export format (without schema wrapper for easy copy)
    export_proj = {
        "name": tmpl["name"],
        "type": tmpl["type"],
        "description": tmpl["description"],
        "github_repo": tmpl["github_repo"],
        "appstore_url": tmpl["appstore_url"],
        "keywords": tmpl["keywords"],
        "notes_query": tmpl["notes_query"],
        "gmail_query": tmpl["gmail_query"],
        "default_branch": tmpl["default_branch"],
        "enabled": tmpl["enabled"],
    }

    export_data = {
        "schema": EXPORT_SCHEMA,
        "projects": [export_proj],
    }

    json_str = json.dumps(export_data, ensure_ascii=False, indent=2)

    lines = [
        f"📋 项目模板: {project_type}",
        "",
        f"描述: {tmpl['description']}",
        "",
        "JSON 模板 (可直接修改后用 /project_import 导入):",
        "",
        f"```json\n{json_str}\n```",
        "",
        "使用方法:",
        "1. 复制上方 JSON",
        "2. 修改 name、description 等字段",
        "3. 使用 /project_import 导入",
    ]
    return ToolResult(ok=True, text="\n".join(lines))


# --- Adapters for personal_tools/registry.py ---

async def project_export_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return project_export(settings, operator_id, arg)


async def project_export_all_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return project_export_all(settings, operator_id)


async def project_import_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return project_import(settings, operator_id, arg)


async def project_template_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return project_template(settings, operator_id, arg)
