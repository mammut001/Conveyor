"""personal_tools/setup.py — Setup Wizard / Onboarding for Conveyor (P3.10).

Checks existing integrations and guides new users through setup.
All commands are READ-only. Never exposes tokens, passwords, or secrets.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

from config import Settings, SENSITIVE_FIELDS
from personal_tools.base import ToolResult
from redaction import redact_text, truncate

if TYPE_CHECKING:
    pass


def _check_icon(ok: bool) -> str:
    return "✅" if ok else "❌"


def _opt_icon(ok: bool) -> str:
    return "✅" if ok else "⚪"


def _check_systemd_timer(timer_name: str) -> bool:
    """Check if a systemd timer is active."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", timer_name],
            capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def setup_status(settings: Settings, operator_id: str) -> ToolResult:
    """Report integration status overview."""
    lines = ["🔧 Conveyor 配置状态", ""]

    # Telegram
    tg_ok = bool(settings.telegram_bot_token)
    uid_ok = settings.telegram_allowed_user_id > 0
    lines.append(f"{_check_icon(tg_ok)} Telegram Bot Token: {'已配置' if tg_ok else '未配置'}")
    lines.append(f"{_check_icon(uid_ok)} Allowed User ID: {'已配置' if uid_ok else '未配置'}")

    # Codex
    codex_bin = settings.codex_bin
    codex_avail = shutil.which(codex_bin) is not None
    ws_ok = settings.codex_workspace_root.exists()
    lines.append(f"{_opt_icon(codex_avail)} Codex Binary ({codex_bin}): {'可用' if codex_avail else '未找到'}")
    lines.append(f"{_check_icon(ws_ok)} Workspace Root: {'存在' if ws_ok else '不存在'}")

    # Gmail
    gmail_ok = bool(settings.gmail_address) and bool(settings.gmail_app_password)
    lines.append(f"{_opt_icon(gmail_ok)} Gmail (IMAP): {'已配置' if gmail_ok else '未配置'}")

    # Google OAuth
    google_ok = bool(settings.google_client_secret_path)
    lines.append(f"{_opt_icon(google_ok)} Google OAuth: {'已配置' if google_ok else '未配置'}")

    # GitHub
    gh_token_ok = bool(settings.github_token)
    gh_repo_ok = bool(settings.github_default_repo)
    lines.append(f"{_opt_icon(gh_token_ok)} GitHub Token: {'已配置' if gh_token_ok else '未配置'}")
    lines.append(f"{_opt_icon(gh_repo_ok)} GitHub Default Repo: {'已配置' if gh_repo_ok else '未配置'}")

    # Briefing (check from store)
    try:
        from personal_tools.store import PersonalToolsStore
        store = PersonalToolsStore(settings)
        bs = store.get_briefing_settings(operator_id)
        brief_ok = bs is not None and bs.enabled
        lines.append(f"{_opt_icon(brief_ok)} Daily Briefing: {'已启用' if brief_ok else '未启用'}")
    except Exception:
        lines.append("⚪ Daily Briefing: 无法检查")

    # Active project
    try:
        from personal_tools.store import PersonalToolsStore
        store = PersonalToolsStore(settings)
        proj = store.get_active_or_first_project(operator_id)
        proj_ok = proj is not None
        lines.append(f"{_opt_icon(proj_ok)} 活跃项目: {proj.name if proj else '未配置'}")
    except Exception:
        lines.append("⚪ 活跃项目: 无法检查")

    # Scheduler
    try:
        from personal_tools.store import PersonalToolsStore
        store = PersonalToolsStore(settings)
        # Just check if store is accessible
        lines.append(f"✅ Scheduler: Personal Tools Store 可用")
    except Exception:
        lines.append("❌ Scheduler: Store 不可用")

    # Systemd timers
    scheduler_timer_ok = _check_systemd_timer("conveyor-scheduler.timer")
    maintain_timer_ok = _check_systemd_timer("conveyor-maintain.timer")
    lines.append(f"{_opt_icon(scheduler_timer_ok)} Systemd Scheduler Timer: {'运行中' if scheduler_timer_ok else '未运行'}")
    lines.append(f"{_opt_icon(maintain_timer_ok)} Systemd Maintain Timer: {'运行中' if maintain_timer_ok else '未运行'}")

    lines.append("")
    lines.append("使用 /setup_check 查看完整检查清单。")
    return ToolResult(ok=True, text=truncate("\n".join(lines)))


def setup_check(settings: Settings, operator_id: str) -> ToolResult:
    """Produce a prioritized setup checklist."""
    required = []
    optional = []
    recommended = []

    # Required checks
    if not settings.telegram_bot_token:
        required.append("❌ Telegram Bot Token 未配置 → 设置 TELEGRAM_BOT_TOKEN")
    if settings.telegram_allowed_user_id <= 0:
        required.append("❌ Allowed User ID 未配置 → 设置 TELEGRAM_ALLOWED_USER_ID")

    codex_bin = settings.codex_bin
    if not shutil.which(codex_bin):
        required.append(f"❌ Codex Binary ({codex_bin}) 未找到 → 安装 codex CLI")

    if not settings.codex_workspace_root.exists():
        required.append(f"❌ Workspace Root 不存在 → 创建 {settings.codex_workspace_root}")

    # Optional checks
    if not settings.gmail_address or not settings.gmail_app_password:
        optional.append("⚪ Gmail 未配置 → /setup_gmail 查看指南")

    if not settings.google_client_secret_path:
        optional.append("⚪ Google OAuth 未配置 → /setup_google 查看指南")

    if not settings.github_token:
        optional.append("⚪ GitHub Token 未配置 → /setup_github 查看指南")
    elif not settings.github_default_repo:
        optional.append("⚪ GitHub Default Repo 未配置 → 设置 GITHUB_DEFAULT_REPO")

    # Briefing
    try:
        from personal_tools.store import PersonalToolsStore
        store = PersonalToolsStore(settings)
        bs = store.get_briefing_settings(operator_id)
        if not bs or not bs.enabled:
            optional.append("⚪ Daily Briefing 未启用 → /brief_enable 启用")
    except Exception:
        pass

    # Project
    try:
        from personal_tools.store import PersonalToolsStore
        store = PersonalToolsStore(settings)
        proj = store.get_active_or_first_project(operator_id)
        if not proj:
            optional.append("⚪ 未配置项目 → /setup_project 查看指南")
    except Exception:
        pass

    # Systemd timers
    if not _check_systemd_timer("conveyor-scheduler.timer"):
        optional.append("⚪ Scheduler Timer 未运行 → systemctl enable --now conveyor-scheduler.timer")
    if not _check_systemd_timer("conveyor-maintain.timer"):
        optional.append("⚪ Maintain Timer 未运行 → systemctl enable --now conveyor-maintain.timer")

    # Recommended next steps
    if not required:
        recommended.append("✅ 所有必需配置已完成！")
        if optional:
            recommended.append("")
            recommended.append("推荐的下一步:")
            recommended.append("  /setup_project — 配置项目")
            recommended.append("  /setup_gmail — 配置 Gmail")
            recommended.append("  /setup_google — 配置 Google OAuth")
            recommended.append("  /setup_github — 配置 GitHub")
            recommended.append("  /brief_enable — 启用每日简报")
    else:
        recommended.append("")
        recommended.append("请先完成必需配置，然后运行 /setup_check 重新检查。")

    lines = ["📋 设置检查清单", ""]
    if required:
        lines.append("必需 (必须修复):")
        lines.extend(required)
        lines.append("")
    if optional:
        lines.append("可选 (推荐配置):")
        lines.extend(optional)
        lines.append("")
    if recommended:
        lines.extend(recommended)

    return ToolResult(ok=True, text=truncate("\n".join(lines)))


def setup_project(settings: Settings, operator_id: str) -> ToolResult:
    """Guide user through project setup."""
    lines = [
        "📂 项目配置指南",
        "",
        "项目是 Conveyor 的核心概念，用于组织和管理你的工作。",
        "",
        "1. 添加项目:",
        "   /project_add <名称> | <类型> | <描述>",
        "",
        "   示例:",
        "   /project_add My App | mobile_app | iOS 待办应用 | user/repo | todo,productivity",
        "   /project_add 网站 | web_app | 公司官网 | org/website",
        "   /project_add 研究 | research | AI 对 NLP 的影响",
        "   /project_add Bot | bot | Telegram 机器人 | user/tg-bot",
        "",
        "   支持的类型: generic, mobile_app, web_app, bot, library, research, course, business",
        "",
        "2. 设置活跃项目:",
        "   /project_use <id>",
        "",
        "3. 查看项目:",
        "   /projects — 列出所有项目",
        "   /project_show [id] — 查看详情",
        "",
        "4. 项目分析:",
        "   /project_next — 下一步行动",
        "   /project_health — 健康检查",
        "   /project_roadmap — 路线图",
        "   /project_brief — 项目简报",
        "",
        "提示: 项目分析会自动从 GitHub、Notes、Gmail、Calendar 收集数据。",
    ]
    return ToolResult(ok=True, text="\n".join(lines))


def setup_gmail(settings: Settings, operator_id: str) -> ToolResult:
    """Guide user through Gmail setup."""
    lines = [
        "📧 Gmail 配置指南",
        "",
        "Gmail 使用 IMAP 协议读取邮件，需要配置 App Password。",
        "",
        "1. 生成 App Password:",
        "   - 访问 https://myaccount.google.com/apppasswords",
        "   - 登录你的 Gmail 账号",
        "   - 创建一个新应用密码",
        "   - 复制生成的 16 位密码",
        "",
        "2. 设置环境变量 (.env):",
        "   GMAIL_ADDRESS=your.email@gmail.com",
        "   GMAIL_APP_PASSWORD=your-16-digit-password",
        "",
        "⚠️ 安全提示:",
        "   - 永远不要分享你的 App Password",
        "   - 永远不要提交 .env 文件到 Git",
        "   - App Password 不是你的 Gmail 登录密码",
        "",
        "3. 测试连接:",
        "   /gmail_status — 检查连接状态",
        "   /gmail_recent — 查看最近邮件",
        "",
        "4. 使用 Gmail:",
        "   /gmail_search <关键词> — 搜索邮件",
        "   /gmail_read <邮件ID> — 读取邮件",
    ]

    # Check current status
    gmail_ok = bool(settings.gmail_address) and bool(settings.gmail_app_password)
    if gmail_ok:
        lines.append("")
        lines.append("✅ Gmail 已配置。使用 /gmail_status 测试连接。")
    else:
        lines.append("")
        lines.append("❌ Gmail 未配置。请按上述步骤设置环境变量。")

    return ToolResult(ok=True, text="\n".join(lines))


def setup_google(settings: Settings, operator_id: str) -> ToolResult:
    """Guide user through Google OAuth setup."""
    lines = [
        "🔐 Google OAuth 配置指南",
        "",
        "Google OAuth 用于访问 Calendar 和 Contacts。",
        "",
        "1. 创建 Google Cloud 项目:",
        "   - 访问 https://console.cloud.google.com/",
        "   - 创建新项目或选择现有项目",
        "   - 启用 Calendar API 和 People API",
        "",
        "2. 创建 OAuth 凭据:",
        "   - 进入 APIs & Services > Credentials",
        "   - 创建 OAuth 2.0 Client ID",
        "   - 下载 JSON 文件",
        "",
        "3. 设置环境变量 (.env):",
        "   GOOGLE_CLIENT_SECRET_PATH=/path/to/client_secret.json",
        "",
        "4. 授权:",
        "   /auth_google — 启动 OAuth 授权流程",
        "",
        "5. 测试:",
        "   /google_status — 检查 OAuth 状态",
        "   /calendar_today — 查看今日日程",
        "   /contacts_search <关键词> — 搜索联系人",
    ]

    # Check current status
    google_ok = bool(settings.google_client_secret_path)
    if google_ok:
        lines.append("")
        lines.append("✅ Google OAuth 已配置。使用 /google_status 检查状态。")
    else:
        lines.append("")
        lines.append("❌ Google OAuth 未配置。请按上述步骤设置。")

    return ToolResult(ok=True, text="\n".join(lines))


def setup_github(settings: Settings, operator_id: str) -> ToolResult:
    """Guide user through GitHub setup."""
    lines = [
        "🐙 GitHub 配置指南",
        "",
        "GitHub 集成用于查看 Issues、PRs 和 CI 状态。",
        "",
        "1. 创建 GitHub Token:",
        "   - 访问 https://github.com/settings/tokens",
        "   - 点击 Generate new token (classic)",
        "   - 选择 repo 权限",
        "   - 复制生成的 token",
        "",
        "2. 设置环境变量 (.env):",
        "   GITHUB_TOKEN=your-github-token",
        "   GITHUB_DEFAULT_REPO=owner/repo",
        "",
        "⚠️ 安全提示:",
        "   - 永远不要分享你的 GitHub Token",
        "   - 永远不要提交 .env 文件到 Git",
        "   - Token 可以随时在 GitHub 撤销",
        "",
        "3. 测试连接:",
        "   /github_status — 检查连接状态",
        "",
        "4. 使用 GitHub:",
        "   /github_issues — 列出 Issues",
        "   /github_prs — 列出 Pull Requests",
        "   /github_ci — 检查 CI 状态",
        "   /github_create_issue <标题> — 创建 Issue",
    ]

    # Check current status (without leaking token)
    gh_token_ok = bool(settings.github_token)
    gh_repo_ok = bool(settings.github_default_repo)
    if gh_token_ok and gh_repo_ok:
        lines.append("")
        lines.append(f"✅ GitHub 已配置 (repo: {settings.github_default_repo})。")
        lines.append("使用 /github_status 测试连接。")
    elif gh_token_ok:
        lines.append("")
        lines.append("⚠️ GitHub Token 已配置，但 Default Repo 未设置。")
        lines.append("请设置 GITHUB_DEFAULT_REPO 环境变量。")
    else:
        lines.append("")
        lines.append("❌ GitHub 未配置。请按上述步骤设置。")

    return ToolResult(ok=True, text="\n".join(lines))


# --- Adapters for personal_tools/registry.py ---

async def setup_status_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return setup_status(settings, operator_id)


async def setup_check_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return setup_check(settings, operator_id)


async def setup_project_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return setup_project(settings, operator_id)


async def setup_gmail_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return setup_gmail(settings, operator_id)


async def setup_google_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return setup_google(settings, operator_id)


async def setup_github_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return setup_github(settings, operator_id)
