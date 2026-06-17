"""personal_tools/projects.py — Generic Project Profiles for Conveyor (P3.9).

Provides a project skills layer that works for any user's projects.
Users define project profiles and run generic project commands against them.
Reuses existing Gmail, Calendar, GitHub, Notes, Reminders tools.

All project analysis commands are READ-only.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from config import Settings
from personal_tools.base import ToolResult
from personal_tools.store import PersonalToolsStore, PROJECT_TYPES, ProjectProfileRow
from redaction import redact_text, truncate

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# --- Prompt templates by project type ---

_PROMPT_TEMPLATES: dict[str, str] = {
    "generic": (
        "项目「{name}」状态分析。\n\n"
        "采集到的事实数据：\n{facts}\n\n"
        "请用中文给出：\n"
        "1. 📊 整体状态概览\n"
        "2. ⚠️ 风险项和阻塞项\n"
        "3. ✅ 推荐的下一步行动（3-5 个）\n"
        "4. 📝 备注和建议"
    ),
    "mobile_app": (
        "移动应用项目「{name}」状态分析。\n\n"
        "采集到的事实数据：\n{facts}\n\n"
        "请用中文给出：\n"
        "1. 📱 发布就绪度（版本号、构建状态）\n"
        "2. 🏪 App Store 状态（审核、评分、评论）\n"
        "3. 🐛 Bug 和 Issue 概况\n"
        "4. 🗺️ Roadmap 进度\n"
        "5. ✅ 推荐的下一步行动"
    ),
    "web_app": (
        "Web 应用项目「{name}」状态分析。\n\n"
        "采集到的事实数据：\n{facts}\n\n"
        "请用中文给出：\n"
        "1. 🚀 部署健康状态（CI/CD、环境）\n"
        "2. 🔧 CI/CD 状态\n"
        "3. 👥 用户反馈概况\n"
        "4. 📦 下一个发布版本的准备情况\n"
        "5. ✅ 推荐的下一步行动"
    ),
    "bot": (
        "Bot 项目「{name}」状态分析。\n\n"
        "采集到的事实数据：\n{facts}\n\n"
        "请用中文给出：\n"
        "1. 🤖 运行状态（uptime、服务状态）\n"
        "2. ⏰ 调度器状态\n"
        "3. 📋 日志概况（错误、警告）\n"
        "4. 🔗 集成状态（API、webhook）\n"
        "5. 📊 队列状态\n"
        "6. ✅ 推荐的下一步行动"
    ),
    "library": (
        "库/SDK 项目「{name}」状态分析。\n\n"
        "采集到的事实数据：\n{facts}\n\n"
        "请用中文给出：\n"
        "1. 🔌 API 稳定性（breaking changes）\n"
        "2. 📖 文档完整性\n"
        "3. 🐛 Issues 和 PR 概况\n"
        "4. 📦 发布说明和版本管理\n"
        "5. ✅ 推荐的下一步行动"
    ),
    "research": (
        "研究项目「{name}」状态分析。\n\n"
        "采集到的事实数据：\n{facts}\n\n"
        "请用中文给出：\n"
        "1. 📅 截止日期和里程碑\n"
        "2. 📝 笔记和文献概况\n"
        "3. 📄 大纲/论文结构进度\n"
        "4. ✍️ 下一步研究/写作行动\n"
        "5. ⚠️ 风险项（延期、遗漏）"
    ),
    "course": (
        "课程项目「{name}」状态分析。\n\n"
        "采集到的事实数据：\n{facts}\n\n"
        "请用中文给出：\n"
        "1. 📚 作业和任务完成情况\n"
        "2. 📅 截止日期提醒\n"
        "3. 📝 笔记和学习资料\n"
        "4. 📖 学习计划建议\n"
        "5. ⚠️ 需要注意的事项"
    ),
    "business": (
        "商业项目「{name}」状态分析。\n\n"
        "采集到的事实数据：\n{facts}\n\n"
        "请用中文给出：\n"
        "1. 👥 客户/合作伙伴概况\n"
        "2. 📧 邮件沟通状态\n"
        "3. ✅ 任务和待办事项\n"
        "4. 🔄 跟进行动\n"
        "5. ⚠️ 风险和机会"
    ),
}


def _no_active_project_msg() -> str:
    return (
        "⚠️ 没有活跃项目。\n\n"
        "请先添加项目：/project_add <名称> | <类型> | <描述>\n"
        "或设置活跃项目：/project_use <id>\n\n"
        "支持的类型: " + ", ".join(PROJECT_TYPES)
    )


def _format_project_row(row: ProjectProfileRow) -> str:
    """Format a project profile for display."""
    status = "✅" if row.enabled else "⏸️"
    gh = f" | GH:{row.github_repo}" if row.github_repo else ""
    return f"{status} #{row.id} {row.name} [{row.type}]{gh}"


def _collect_facts(settings: Settings, project: ProjectProfileRow, operator_id: str) -> str:
    """Collect facts from existing tools for a project."""
    facts = []

    # GitHub facts
    if project.github_repo and settings.github_token:
        try:
            from personal_tools.github_tools import (
                github_status, github_issues, github_prs, github_ci,
            )
            repo = project.github_repo
            status = github_status(settings)
            if status.ok:
                facts.append(f"[GitHub 状态]\n{status.text}")
            issues = github_issues(settings, repo, state="open")
            if issues.ok:
                facts.append(f"[Open Issues]\n{truncate(issues.text, 500)}")
            prs = github_prs(settings, repo, state="open")
            if prs.ok:
                facts.append(f"[Open PRs]\n{truncate(prs.text, 500)}")
            ci = github_ci(settings, repo)
            if ci.ok:
                facts.append(f"[CI 状态]\n{truncate(ci.text, 300)}")
        except Exception as exc:
            facts.append(f"[GitHub] 获取失败: {redact_text(str(exc))}")
    else:
        facts.append("[GitHub] 未配置仓库（跳过）")

    # Notes facts
    notes_query = project.notes_query or " ".join(project.keywords[:3])
    if notes_query:
        try:
            store = PersonalToolsStore(settings)
            notes = store.search_notes(operator_id, notes_query, limit=5)
            if notes:
                lines = [f"#{n.id} {n.text[:80]}" for n in notes]
                facts.append(f"[相关笔记]\n" + "\n".join(lines))
            else:
                facts.append("[相关笔记] 无匹配笔记")
        except Exception as exc:
            facts.append(f"[笔记] 获取失败: {redact_text(str(exc))}")

    # Gmail facts
    gmail_query = project.gmail_query or " ".join(project.keywords[:3])
    if gmail_query and settings.gmail_address and settings.gmail_app_password:
        try:
            from personal_tools.gmail_imap import gmail_search
            result = gmail_search(settings, gmail_query, limit=5)
            if result.ok:
                facts.append(f"[相关邮件]\n{truncate(result.text, 500)}")
            else:
                facts.append(f"[邮件] 搜索无结果")
        except Exception as exc:
            facts.append(f"[邮件] 获取失败: {redact_text(str(exc))}")
    else:
        facts.append("[邮件] 未配置或无查询词（跳过）")

    # Calendar facts
    try:
        from personal_tools.google_oauth import load_credentials
        creds = load_credentials(settings)
        if creds:
            from personal_tools.calendar_google import calendar_today
            cal = calendar_today(settings)
            if cal.ok:
                facts.append(f"[今日日程]\n{truncate(cal.text, 300)}")
            else:
                facts.append("[日历] 无今日日程")
        else:
            facts.append("[日历] Google OAuth 未配置（跳过）")
    except Exception:
        facts.append("[日历] 获取失败（跳过）")

    # Reminders facts
    try:
        store = PersonalToolsStore(settings)
        reminders = store.list_reminders(operator_id, limit=5)
        if reminders:
            lines = [f"• {r.text[:60]}" for r in reminders[:3]]
            facts.append(f"[提醒]\n" + "\n".join(lines))
        else:
            facts.append("[提醒] 无待办提醒")
    except Exception:
        facts.append("[提醒] 获取失败（跳过）")

    return "\n\n".join(facts)


# --- Tool implementations ---

async def projects_list(settings: Settings, arg: str, *, operator_id: str, **_kw) -> ToolResult:
    """List all project profiles for the operator."""
    store = PersonalToolsStore(settings)
    rows = store.list_project_profiles(operator_id)
    if not rows:
        return ToolResult(ok=True, text=(
            "📂 还没有项目配置。\n\n"
            "使用 /project_add <名称> | <类型> | <描述> 添加项目。\n"
            "支持的类型: " + ", ".join(PROJECT_TYPES)
        ))
    active = store.get_active_project(operator_id)
    active_id = active.id if active else None
    lines = [f"📂 项目列表 ({len(rows)} 个):", ""]
    for r in rows:
        marker = " 👈 活跃" if r.id == active_id else ""
        lines.append(f"  {_format_project_row(r)}{marker}")
    lines.append("")
    lines.append("使用 /project_use <id> 切换活跃项目。")
    return ToolResult(ok=True, text="\n".join(lines))


async def projects_add(settings: Settings, arg: str, *, operator_id: str, **_kw) -> ToolResult:
    """Add a new project profile.

    Format: name | type | description | [github_repo] | [keywords]
    """
    raw = (arg or "").strip()
    if not raw:
        return ToolResult(ok=False, text=(
            "用法: /project_add <名称> | <类型> | <描述> | [github_repo] | [关键词]\n\n"
            "示例:\n"
            "  /project_add My App | mobile_app | iOS 待办应用 | user/repo | todo,productivity\n"
            "  /project_add 研究课题 | research | AI 对 NLP 的影响\n\n"
            "支持的类型: " + ", ".join(PROJECT_TYPES)
        ))
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3:
        return ToolResult(ok=False, text="⚠️ 至少需要: 名称 | 类型 | 描述")
    name, ptype, desc = parts[0], parts[1], parts[2]
    if ptype not in PROJECT_TYPES:
        return ToolResult(ok=False, text=f"⚠️ 无效类型 '{ptype}'，支持: {', '.join(PROJECT_TYPES)}")
    github_repo = parts[3] if len(parts) > 3 else ""
    keywords_raw = parts[4] if len(parts) > 4 else ""
    keywords = tuple(k.strip() for k in keywords_raw.split(",") if k.strip()) if keywords_raw else ()

    store = PersonalToolsStore(settings)
    row = store.create_project_profile(
        operator_id, name, ptype, desc,
        github_repo=github_repo, keywords=keywords,
    )
    return ToolResult(ok=True, text=(
        f"✅ 项目已创建: #{row.id} {row.name}\n"
        f"类型: {row.type}\n"
        f"描述: {row.description}\n"
        f"GitHub: {row.github_repo or '(未设置)'}\n\n"
        f"使用 /project_use {row.id} 设为活跃项目。"
    ))


async def projects_use(settings: Settings, arg: str, *, operator_id: str, **_kw) -> ToolResult:
    """Set the active project."""
    raw = (arg or "").strip()
    if not raw:
        return ToolResult(ok=False, text="用法: /project_use <id>")
    try:
        project_id = int(raw)
    except ValueError:
        return ToolResult(ok=False, text=f"⚠️ 无效项目 ID: {raw}")
    store = PersonalToolsStore(settings)
    if store.set_active_project(operator_id, project_id):
        proj = store.get_project_profile(operator_id, project_id)
        name = proj.name if proj else str(project_id)
        return ToolResult(ok=True, text=f"✅ 已切换到活跃项目: #{project_id} {name}")
    return ToolResult(ok=False, text=f"⚠️ 项目 #{project_id} 不存在。")


async def projects_show(settings: Settings, arg: str, *, operator_id: str, **_kw) -> ToolResult:
    """Show project details."""
    raw = (arg or "").strip()
    store = PersonalToolsStore(settings)
    if raw:
        try:
            project_id = int(raw)
        except ValueError:
            return ToolResult(ok=False, text=f"⚠️ 无效项目 ID: {raw}")
        proj = store.get_project_profile(operator_id, project_id)
    else:
        proj = store.get_active_or_first_project(operator_id)
    if not proj:
        return ToolResult(ok=True, text=_no_active_project_msg())
    lines = [
        f"📋 项目详情: #{proj.id}",
        "",
        f"  名称: {proj.name}",
        f"  类型: {proj.type}",
        f"  描述: {proj.description or '(无)'}",
        f"  GitHub: {proj.github_repo or '(未设置)'}",
        f"  App Store: {proj.appstore_url or '(未设置)'}",
        f"  关键词: {', '.join(proj.keywords) if proj.keywords else '(无)'}",
        f"  笔记查询: {proj.notes_query or '(未设置)'}",
        f"  邮件查询: {proj.gmail_query or '(未设置)'}",
        f"  默认分支: {proj.default_branch or '(未设置)'}",
        f"  状态: {'✅ 启用' if proj.enabled else '⏸️ 禁用'}",
        f"  创建时间: {proj.created_at}",
        f"  更新时间: {proj.updated_at}",
    ]
    return ToolResult(ok=True, text="\n".join(lines))


async def projects_remove(settings: Settings, arg: str, *, operator_id: str, **_kw) -> ToolResult:
    """Remove a project profile (DESTRUCTIVE, requires confirmation)."""
    raw = (arg or "").strip()
    if not raw:
        return ToolResult(ok=False, text="用法: /project_remove <id>")
    try:
        project_id = int(raw)
    except ValueError:
        return ToolResult(ok=False, text=f"⚠️ 无效项目 ID: {raw}")
    store = PersonalToolsStore(settings)
    proj = store.get_project_profile(operator_id, project_id)
    if not proj:
        return ToolResult(ok=False, text=f"⚠️ 项目 #{project_id} 不存在。")
    if store.delete_project_profile(operator_id, project_id):
        return ToolResult(ok=True, text=f"🗑️ 已删除项目: #{project_id} {proj.name}")
    return ToolResult(ok=False, text=f"⚠️ 删除失败。")


async def project_status(settings: Settings, arg: str, *, operator_id: str, **_kw) -> ToolResult:
    """Show project status (hybrid: collect facts + Codex analysis)."""
    raw = (arg or "").strip()
    store = PersonalToolsStore(settings)
    if raw:
        try:
            project_id = int(raw)
        except ValueError:
            return ToolResult(ok=False, text=f"⚠️ 无效项目 ID: {raw}")
        proj = store.get_project_profile(operator_id, project_id)
    else:
        proj = store.get_active_or_first_project(operator_id)
    if not proj:
        return ToolResult(ok=True, text=_no_active_project_msg())

    facts = _collect_facts(settings, proj, operator_id)
    template = _PROMPT_TEMPLATES.get(proj.type, _PROMPT_TEMPLATES["generic"])
    prompt = template.format(name=proj.name, facts=redact_text(facts))
    return ToolResult(ok=True, text=f"[HYBRID_PROMPT]{prompt}")


async def project_health(settings: Settings, arg: str, *, operator_id: str, **_kw) -> ToolResult:
    """Show project health (hybrid)."""
    raw = (arg or "").strip()
    store = PersonalToolsStore(settings)
    if raw:
        try:
            project_id = int(raw)
        except ValueError:
            return ToolResult(ok=False, text=f"⚠️ 无效项目 ID: {raw}")
        proj = store.get_project_profile(operator_id, project_id)
    else:
        proj = store.get_active_or_first_project(operator_id)
    if not proj:
        return ToolResult(ok=True, text=_no_active_project_msg())

    facts = _collect_facts(settings, proj, operator_id)
    prompt = (
        f"项目「{proj.name}」健康检查。\n\n"
        f"采集到的事实数据：\n{redact_text(facts)}\n\n"
        "请用中文给出：\n"
        "1. 🏥 整体健康状态（绿/黄/红）\n"
        "2. ⚠️ 风险项（CI 失败、服务异常、积压 issue）\n"
        "3. 🔴 需要立即关注的问题\n"
        "4. 📌 推荐的修复/改进行动\n\n"
        "如果有 CI 失败或服务异常，优先分析。如果一切正常，简短确认即可。"
    )
    return ToolResult(ok=True, text=f"[HYBRID_PROMPT]{prompt}")


async def project_roadmap(settings: Settings, arg: str, *, operator_id: str, **_kw) -> ToolResult:
    """Show project roadmap (hybrid)."""
    raw = (arg or "").strip()
    store = PersonalToolsStore(settings)
    if raw:
        try:
            project_id = int(raw)
        except ValueError:
            return ToolResult(ok=False, text=f"⚠️ 无效项目 ID: {raw}")
        proj = store.get_project_profile(operator_id, project_id)
    else:
        proj = store.get_active_or_first_project(operator_id)
    if not proj:
        return ToolResult(ok=True, text=_no_active_project_msg())

    facts = _collect_facts(settings, proj, operator_id)
    prompt = (
        f"项目「{proj.name}」Roadmap 规划。\n\n"
        f"采集到的事实数据：\n{redact_text(facts)}\n\n"
        "请用中文给出：\n"
        "1. 🗺️ 当前里程碑和进度\n"
        "2. 📅 接下来的里程碑（短期 1-2 周）\n"
        "3. 🔮 中期目标（1-3 个月）\n"
        "4. ⚠️ 路径上的风险和依赖\n"
        "5. ✅ 推荐的下一步行动"
    )
    return ToolResult(ok=True, text=f"[HYBRID_PROMPT]{prompt}")


async def project_next(settings: Settings, arg: str, *, operator_id: str, **_kw) -> ToolResult:
    """Show next actions for project (hybrid)."""
    raw = (arg or "").strip()
    store = PersonalToolsStore(settings)
    if raw:
        try:
            project_id = int(raw)
        except ValueError:
            return ToolResult(ok=False, text=f"⚠️ 无效项目 ID: {raw}")
        proj = store.get_project_profile(operator_id, project_id)
    else:
        proj = store.get_active_or_first_project(operator_id)
    if not proj:
        return ToolResult(ok=True, text=_no_active_project_msg())

    facts = _collect_facts(settings, proj, operator_id)
    prompt = (
        f"项目「{proj.name}」下一步行动建议。\n\n"
        f"采集到的事实数据：\n{redact_text(facts)}\n\n"
        "请用中文给出：\n"
        "1. 🎯 最重要的 3 个下一步行动\n"
        "2. ⏰ 时间敏感的任务\n"
        "3. 🔧 技术债务或改进项\n"
        "4. 💡 可以并行进行的任务\n\n"
        "保持简洁实用，每个行动项给出简短理由。"
    )
    return ToolResult(ok=True, text=f"[HYBRID_PROMPT]{prompt}")


async def project_release_checklist(settings: Settings, arg: str, *, operator_id: str, **_kw) -> ToolResult:
    """Generate release checklist for project (hybrid)."""
    raw = (arg or "").strip()
    store = PersonalToolsStore(settings)
    if raw:
        try:
            project_id = int(raw)
        except ValueError:
            return ToolResult(ok=False, text=f"⚠️ 无效项目 ID: {raw}")
        proj = store.get_project_profile(operator_id, project_id)
    else:
        proj = store.get_active_or_first_project(operator_id)
    if not proj:
        return ToolResult(ok=True, text=_no_active_project_msg())

    facts = _collect_facts(settings, proj, operator_id)
    prompt = (
        f"项目「{proj.name}」发布清单。\n\n"
        f"采集到的事实数据：\n{redact_text(facts)}\n\n"
        "请用中文生成一个发布检查清单：\n"
        "1. ✅ 代码质量（测试、lint、CI）\n"
        "2. 📦 版本号和 changelog\n"
        "3. 📖 文档更新\n"
        "4. 🔧 配置和环境变量\n"
        "5. 🚀 部署步骤\n"
        "6. 📢 发布后事项\n\n"
        "根据项目类型调整清单内容。"
    )
    return ToolResult(ok=True, text=f"[HYBRID_PROMPT]{prompt}")


async def project_brief(settings: Settings, arg: str, *, operator_id: str, **_kw) -> ToolResult:
    """Generate a brief summary for project (hybrid)."""
    raw = (arg or "").strip()
    store = PersonalToolsStore(settings)
    if raw:
        try:
            project_id = int(raw)
        except ValueError:
            return ToolResult(ok=False, text=f"⚠️ 无效项目 ID: {raw}")
        proj = store.get_project_profile(operator_id, project_id)
    else:
        proj = store.get_active_or_first_project(operator_id)
    if not proj:
        return ToolResult(ok=True, text=_no_active_project_msg())

    facts = _collect_facts(settings, proj, operator_id)
    prompt = (
        f"项目「{proj.name}」简报。\n\n"
        f"采集到的事实数据：\n{redact_text(facts)}\n\n"
        "请用中文生成一个简短的项目简报（不超过 300 字）：\n"
        "1. 📊 一句话总结当前状态\n"
        "2. 🎯 本周重点\n"
        "3. ⚠️ 需要注意的风险\n"
        "4. ✅ 下一步行动"
    )
    return ToolResult(ok=True, text=f"[HYBRID_PROMPT]{prompt}")


# --- Adapters for personal_tools/registry.py ---

async def projects_list_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return await projects_list(settings, arg, operator_id=operator_id)


async def projects_add_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return await projects_add(settings, arg, operator_id=operator_id)


async def projects_use_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return await projects_use(settings, arg, operator_id=operator_id)


async def projects_show_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return await projects_show(settings, arg, operator_id=operator_id)


async def projects_remove_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return await projects_remove(settings, arg, operator_id=operator_id)


async def project_status_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return await project_status(settings, arg, operator_id=operator_id)


async def project_health_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return await project_health(settings, arg, operator_id=operator_id)


async def project_roadmap_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return await project_roadmap(settings, arg, operator_id=operator_id)


async def project_next_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return await project_next(settings, arg, operator_id=operator_id)


async def project_release_checklist_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return await project_release_checklist(settings, arg, operator_id=operator_id)


async def project_brief_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return await project_brief(settings, arg, operator_id=operator_id)


def build_project_briefing_section(settings: Settings, operator_id: str) -> str:
    """Build a project status section for the daily briefing.

    Shows up to 3 enabled active projects with short status.
    Degrades gracefully if no projects configured.
    """
    try:
        store = PersonalToolsStore(settings)
        projects = store.list_project_profiles(operator_id)
        enabled = [p for p in projects if p.enabled][:3]
        if not enabled:
            return "📂 项目: 未配置（使用 /project_add 添加）"

        lines = [f"📂 活跃项目 ({len(enabled)} 个):"]
        for proj in enabled:
            parts = [f"  • {proj.name} [{proj.type}]"]

            # GitHub summary if available
            if proj.github_repo and settings.github_token:
                try:
                    from personal_tools.github_tools import github_issues, github_prs
                    issues = github_issues(settings, proj.github_repo, state="open")
                    prs = github_prs(settings, proj.github_repo, state="open")
                    if issues.ok:
                        # Count items roughly
                        issue_count = issues.text.count("\n") if issues.ok else 0
                        parts.append(f"Issues: ~{issue_count}")
                    if prs.ok:
                        pr_count = prs.text.count("\n") if prs.ok else 0
                        parts.append(f"PRs: ~{pr_count}")
                except Exception:
                    pass

            # Recent notes
            notes_query = proj.notes_query or " ".join(proj.keywords[:2])
            if notes_query:
                try:
                    notes = store.search_notes(operator_id, notes_query, limit=2)
                    if notes:
                        parts.append(f"笔记: {len(notes)} 条相关")
                except Exception:
                    pass

            lines.append(" | ".join(parts))

        return "\n".join(lines)
    except Exception as exc:
        return f"📂 项目: 获取失败 ({redact_text(str(exc))})"
