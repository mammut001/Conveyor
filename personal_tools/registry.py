"""personal_tools/registry.py — personal tool registry and execution."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from config import Settings
from handlers.tools.registry import DangerLevel
from personal_tools.base import PersonalToolSpec, ToolResult
from personal_tools import notes as notes_tools
from personal_tools import reminders as reminders_tools
from personal_tools import gmail_imap as gmail_tools
from personal_tools import email_smtp as email_tools
from personal_tools import google_oauth as oauth_tools
from personal_tools import calendar_google as calendar_tools
from personal_tools import contacts_google as contacts_tools
from personal_tools import briefing as briefing_tools
from personal_tools import github_tools as github_tools
from personal_tools import planner as planner_tools
from personal_tools import projects as projects_tools
from personal_tools import setup as setup_tools
from personal_tools import project_io as project_io_tools
from personal_tools import web_fetch as web_fetch_tools
from personal_tools import web_search as web_search_tools
from personal_tools import research as research_tools

if TYPE_CHECKING:
    pass

PersonalExecutor = Callable[..., Awaitable[ToolResult]]

PERSONAL_TOOL_REGISTRY: dict[str, PersonalToolSpec] = {}
_PERSONAL_EXECUTORS: dict[str, PersonalExecutor] = {}


def _register(
    name: str,
    summary: str,
    danger: DangerLevel,
    executor: PersonalExecutor,
    *,
    keywords: tuple[str, ...] = (),
) -> None:
    PERSONAL_TOOL_REGISTRY[name] = PersonalToolSpec(
        name=name,
        summary=summary,
        danger=danger,
        keywords=keywords,
    )
    _PERSONAL_EXECUTORS[name] = executor


def register_personal_tools() -> None:
    if PERSONAL_TOOL_REGISTRY:
        return
    _register(
        "notes.add",
        "添加本地笔记",
        DangerLevel.WRITE_SAFE,
        notes_tools.notes_add,
        keywords=("笔记", "note"),
    )
    _register(
        "notes.search",
        "搜索本地笔记",
        DangerLevel.READ,
        notes_tools.notes_search,
    )
    _register(
        "notes.list_recent",
        "列出最近笔记",
        DangerLevel.READ,
        notes_tools.notes_list_recent,
    )
    _register(
        "notes.delete",
        "删除本地笔记 (需确认)",
        DangerLevel.DESTRUCTIVE,
        notes_tools.notes_delete,
    )
    _register(
        "reminders.create",
        "创建本地提醒",
        DangerLevel.WRITE_SAFE,
        reminders_tools.reminders_create,
        keywords=("提醒", "remind"),
    )
    _register(
        "reminders.list",
        "列出提醒",
        DangerLevel.READ,
        reminders_tools.reminders_list,
    )
    _register(
        "reminders.cancel",
        "取消提醒 (需确认)",
        DangerLevel.WRITE,
        reminders_tools.reminders_cancel,
    )
    _register(
        "reminders.due",
        "列出到期提醒",
        DangerLevel.READ,
        reminders_tools.reminders_due,
    )
    # Gmail tools (P3.3)
    _register(
        "gmail.status",
        "Gmail 连接状态",
        DangerLevel.READ,
        gmail_tools.gmail_status_adapter,
        keywords=("gmail", "邮箱状态"),
    )
    _register(
        "gmail.recent",
        "最近邮件",
        DangerLevel.READ,
        gmail_tools.gmail_recent_adapter,
        keywords=("邮件", "email"),
    )
    _register(
        "gmail.search",
        "搜索邮件",
        DangerLevel.READ,
        gmail_tools.gmail_search_adapter,
        keywords=("搜索邮件",),
    )
    _register(
        "gmail.read",
        "读取邮件",
        DangerLevel.READ,
        gmail_tools.gmail_read_adapter,
        keywords=("读取邮件",),
    )
    _register(
        "email.send",
        "发送邮件 (需确认)",
        DangerLevel.WRITE,
        email_tools.email_send_adapter,
        keywords=("发邮件", "send email"),
    )
    # Google OAuth (P3.4)
    _register(
        "google.status",
        "Google OAuth 状态",
        DangerLevel.READ,
        oauth_tools.google_status_adapter,
        keywords=("google", "oauth"),
    )
    _register(
        "google.auth",
        "Google OAuth 授权",
        DangerLevel.WRITE,
        oauth_tools.google_auth_adapter,
        keywords=("授权", "auth"),
    )
    _register(
        "google.revoke",
        "撤销 Google 授权 (需确认)",
        DangerLevel.DESTRUCTIVE,
        oauth_tools.google_revoke_adapter,
        keywords=("撤销授权",),
    )
    # Google Calendar (P3.4)
    _register(
        "calendar.status",
        "Calendar 连接状态",
        DangerLevel.READ,
        calendar_tools.calendar_status_adapter,
        keywords=("日历", "calendar"),
    )
    _register(
        "calendar.today",
        "今日日程",
        DangerLevel.READ,
        calendar_tools.calendar_today_adapter,
        keywords=("今天", "today"),
    )
    _register(
        "calendar.tomorrow",
        "明日日程",
        DangerLevel.READ,
        calendar_tools.calendar_tomorrow_adapter,
        keywords=("明天", "tomorrow"),
    )
    _register(
        "calendar.week",
        "本周日程",
        DangerLevel.READ,
        calendar_tools.calendar_week_adapter,
        keywords=("本周", "week"),
    )
    _register(
        "calendar.search",
        "搜索日程",
        DangerLevel.READ,
        calendar_tools.calendar_search_adapter,
        keywords=("搜索日程",),
    )
    _register(
        "calendar.freebusy",
        "查询忙闲",
        DangerLevel.READ,
        calendar_tools.calendar_freebusy_adapter,
        keywords=("忙闲", "freebusy"),
    )
    _register(
        "calendar.create",
        "创建日程 (需确认)",
        DangerLevel.WRITE,
        calendar_tools.calendar_create_adapter,
        keywords=("创建日程", "新建日程"),
    )
    # Google Contacts (P3.4)
    _register(
        "contacts.search",
        "搜索联系人",
        DangerLevel.READ,
        contacts_tools.contacts_search_adapter,
        keywords=("联系人", "contacts"),
    )
    # Daily Briefing (P3.5)
    _register(
        "briefing.status",
        "Briefing 设置状态",
        DangerLevel.READ,
        briefing_tools.briefing_status_adapter,
        keywords=("briefing", "简报"),
    )
    _register(
        "briefing.today",
        "今日简报",
        DangerLevel.READ,
        briefing_tools.briefing_today_adapter,
        keywords=("今日简报",),
    )
    _register(
        "briefing.tomorrow",
        "明日简报",
        DangerLevel.READ,
        briefing_tools.briefing_tomorrow_adapter,
        keywords=("明日简报",),
    )
    _register(
        "briefing.enable",
        "启用 Briefing (立即执行)",
        DangerLevel.WRITE_SAFE,
        briefing_tools.briefing_enable_adapter,
        keywords=("启用简报",),
    )
    _register(
        "briefing.disable",
        "禁用 Briefing (需确认)",
        DangerLevel.WRITE,
        briefing_tools.briefing_disable_adapter,
        keywords=("禁用简报",),
    )
    _register(
        "briefing.probe",
        "Briefing 探针 (dry-run)",
        DangerLevel.READ,
        briefing_tools.briefing_probe_adapter,
        keywords=("简报探针",),
    )
    # GitHub tools (P3.6)
    _register(
        "github.status",
        "GitHub 连接状态",
        DangerLevel.READ,
        github_tools.github_status_adapter,
        keywords=("github",),
    )
    _register(
        "github.issues",
        "列出 Issues",
        DangerLevel.READ,
        github_tools.github_issues_adapter,
        keywords=("issue",),
    )
    _register(
        "github.issue",
        "查看 Issue 详情",
        DangerLevel.READ,
        github_tools.github_issue_adapter,
    )
    _register(
        "github.prs",
        "列出 Pull Requests",
        DangerLevel.READ,
        github_tools.github_prs_adapter,
        keywords=("pr", "pull request"),
    )
    _register(
        "github.pr",
        "查看 PR 详情",
        DangerLevel.READ,
        github_tools.github_pr_adapter,
    )
    _register(
        "github.ci",
        "CI 状态",
        DangerLevel.READ,
        github_tools.github_ci_adapter,
        keywords=("ci", "构建"),
    )
    _register(
        "github.create_issue",
        "创建 Issue (审计)",
        DangerLevel.WRITE_SAFE,
        github_tools.github_create_issue_adapter,
        keywords=("创建 issue",),
    )
    _register(
        "github.comment",
        "评论 Issue/PR (需确认)",
        DangerLevel.WRITE,
        github_tools.github_comment_adapter,
        keywords=("评论",),
    )
    # Planner profiles (P3.7)
    _register(
        "planner.list",
        "列出 Planner Profiles",
        DangerLevel.READ,
        planner_tools.planner_status_adapter,
        keywords=("planner", "计划"),
    )
    _register(
        "planner.today",
        "今日优先级分析",
        DangerLevel.READ,
        planner_tools.planner_today_adapter,
        keywords=("优先", "今天干啥"),
    )
    _register(
        "planner.dev",
        "开发计划",
        DangerLevel.READ,
        planner_tools.planner_dev_adapter,
        keywords=("开发计划",),
    )
    _register(
        "planner.health",
        "项目健康检查",
        DangerLevel.READ,
        planner_tools.planner_health_adapter,
        keywords=("项目健康",),
    )
    _register(
        "planner.triage",
        "邮件分类整理",
        DangerLevel.READ,
        planner_tools.planner_triage_adapter,
        keywords=("整理邮件",),
    )
    _register(
        "planner.schedule",
        "日程审查",
        DangerLevel.READ,
        planner_tools.planner_schedule_adapter,
        keywords=("日程安排",),
    )
    # Project Profiles (P3.9)
    _register(
        "projects.list",
        "列出项目",
        DangerLevel.READ,
        projects_tools.projects_list_adapter,
        keywords=("项目", "project"),
    )
    _register(
        "projects.add",
        "添加项目",
        DangerLevel.WRITE_SAFE,
        projects_tools.projects_add_adapter,
        keywords=("添加项目",),
    )
    _register(
        "projects.use",
        "切换活跃项目",
        DangerLevel.WRITE_SAFE,
        projects_tools.projects_use_adapter,
        keywords=("切换项目",),
    )
    _register(
        "projects.show",
        "查看项目详情",
        DangerLevel.READ,
        projects_tools.projects_show_adapter,
        keywords=("项目详情",),
    )
    _register(
        "projects.remove",
        "删除项目 (需确认)",
        DangerLevel.DESTRUCTIVE,
        projects_tools.projects_remove_adapter,
        keywords=("删除项目",),
    )
    _register(
        "project.status",
        "项目状态分析",
        DangerLevel.READ,
        projects_tools.project_status_adapter,
        keywords=("项目状态",),
    )
    _register(
        "project.health",
        "项目健康检查",
        DangerLevel.READ,
        projects_tools.project_health_adapter,
        keywords=("项目健康",),
    )
    _register(
        "project.roadmap",
        "项目 Roadmap",
        DangerLevel.READ,
        projects_tools.project_roadmap_adapter,
        keywords=("roadmap", "路线图"),
    )
    _register(
        "project.next",
        "项目下一步行动",
        DangerLevel.READ,
        projects_tools.project_next_adapter,
        keywords=("下一步",),
    )
    _register(
        "project.release_checklist",
        "项目发布清单",
        DangerLevel.READ,
        projects_tools.project_release_checklist_adapter,
        keywords=("发布清单", "release"),
    )
    _register(
        "project.brief",
        "项目简报",
        DangerLevel.READ,
        projects_tools.project_brief_adapter,
        keywords=("项目简报",),
    )
    # Setup Wizard (P3.10)
    _register(
        "setup.status",
        "配置状态概览",
        DangerLevel.READ,
        setup_tools.setup_status_adapter,
        keywords=("setup", "配置"),
    )
    _register(
        "setup.check",
        "设置检查清单",
        DangerLevel.READ,
        setup_tools.setup_check_adapter,
        keywords=("检查",),
    )
    _register(
        "setup.project",
        "项目配置指南",
        DangerLevel.READ,
        setup_tools.setup_project_adapter,
        keywords=("项目配置",),
    )
    _register(
        "setup.gmail",
        "Gmail 配置指南",
        DangerLevel.READ,
        setup_tools.setup_gmail_adapter,
        keywords=("gmail配置",),
    )
    _register(
        "setup.google",
        "Google OAuth 配置指南",
        DangerLevel.READ,
        setup_tools.setup_google_adapter,
        keywords=("google配置",),
    )
    _register(
        "setup.github",
        "GitHub 配置指南",
        DangerLevel.READ,
        setup_tools.setup_github_adapter,
        keywords=("github配置",),
    )
    # Project Import/Export (P3.11)
    _register(
        "project.export",
        "导出项目为 JSON",
        DangerLevel.READ,
        project_io_tools.project_export_adapter,
        keywords=("导出项目",),
    )
    _register(
        "project.export_all",
        "导出所有项目",
        DangerLevel.READ,
        project_io_tools.project_export_all_adapter,
        keywords=("导出所有",),
    )
    _register(
        "project.import",
        "从 JSON 导入项目",
        DangerLevel.WRITE_SAFE,
        project_io_tools.project_import_adapter,
        keywords=("导入项目",),
    )
    _register(
        "project.template",
        "项目模板",
        DangerLevel.READ,
        project_io_tools.project_template_adapter,
        keywords=("项目模板",),
    )
    # Web Fetch (P4.1 Phase A)
    _register(
        "web.fetch",
        "获取网页内容",
        DangerLevel.READ,
        web_fetch_tools.web_fetch_adapter,
        keywords=("网页", "fetch"),
    )
    _register(
        "web.text",
        "获取网页文本",
        DangerLevel.READ,
        web_fetch_tools.web_text_adapter,
        keywords=("网页文本",),
    )
    _register(
        "web.headers",
        "获取 HTTP headers",
        DangerLevel.READ,
        web_fetch_tools.web_headers_adapter,
        keywords=("headers",),
    )
    # Web Search (P4.1 Phase B)
    _register(
        "web.search",
        "Web 搜索",
        DangerLevel.READ,
        web_search_tools.web_search_adapter,
        keywords=("搜索", "search"),
    )
    # Research (P4.1 Phase C)
    _register(
        "research.run",
        "Web 研究",
        DangerLevel.READ,
        research_tools.research_adapter,
        keywords=("研究", "research"),
    )
    _register(
        "research.project",
        "项目相关研究",
        DangerLevel.READ,
        research_tools.project_research_adapter,
        keywords=("项目研究",),
    )


def get_personal_tool(name: str) -> PersonalToolSpec | None:
    register_personal_tools()
    return PERSONAL_TOOL_REGISTRY.get(name)


def requires_personal_confirmation(name: str) -> bool:
    spec = get_personal_tool(name)
    if spec is None:
        return False
    return spec.danger in (DangerLevel.WRITE, DangerLevel.DESTRUCTIVE)


async def execute_personal_tool(
    settings: Settings,
    tool_name: str,
    arg: str,
    *,
    operator_id: str,
    channel: str = "",
    chat_id: str = "",
) -> str:
    register_personal_tools()
    executor = _PERSONAL_EXECUTORS.get(tool_name)
    if executor is None:
        return f"未知个人工具: {tool_name}"
    try:
        result: ToolResult = await executor(
            settings, arg,
            operator_id=operator_id,
            channel=channel,
            chat_id=chat_id,
        )
    except Exception as exc:
        return f"个人工具 {tool_name} 执行失败: {type(exc).__name__}"
    return result.text


def personal_tool_danger(name: str) -> str:
    spec = get_personal_tool(name)
    return spec.danger.value if spec else "unknown"
