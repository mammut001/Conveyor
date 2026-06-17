"""personal_tools/planner.py — Natural Language Planner for Conveyor (P3.7).

Composes existing deterministic tools into useful personal-agent workflows.
All planner profiles are READ-only: no write tools, no sending, no creating.

Flow:
  1. Collect facts from registered READ tools
  2. Build a hybrid prompt with collected facts
  3. Pass to Codex for structured analysis
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from config import Settings
from personal_tools.base import ToolResult
from redaction import redact_text, truncate

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlannerProfile:
    """Defines a planner workflow profile."""
    name: str
    command: str
    tool_items: tuple[tuple[str, str], ...]  # (tool_name, arg) pairs
    prompt_template: str
    summary: str


# --- Planner Profiles ---

DAILY_PRIORITY = PlannerProfile(
    name="daily_priority",
    command="/plan_today",
    tool_items=(
        ("calendar.today", ""),
        ("reminders.list", ""),
        ("gmail.recent", ""),
        ("github.issues", "open"),
        ("github.prs", "open"),
        ("notes.list_recent", ""),
    ),
    prompt_template=(
        "用户想了解今天的优先事项。以下是采集到的事实数据：\n\n"
        "{facts}\n\n"
        "请基于以上数据，用中文给出：\n"
        "1. 🎯 今日优先事项（最重要的 3 件事）\n"
        "2. ⏰ 日程风险（时间冲突、密集会议）\n"
        "3. 📧 需要回复的邮件（如有）\n"
        "4. 🐙 GitHub 需要关注的 items（PR review、紧急 issue）\n"
        "5. ✅ 推荐的下一步 3 个行动\n\n"
        "如果某个数据源未配置或为空，跳过该部分。保持简洁实用。"
    ),
    summary="今日优先级分析",
)

DEV_PLAN = PlannerProfile(
    name="dev_plan",
    command="/plan_dev",
    tool_items=(
        ("calendar.today", ""),
        ("github.issues", "open"),
        ("github.prs", "open"),
        ("github.ci", ""),
        ("git_status", ""),
    ),
    prompt_template=(
        "用户想制定今天的开发计划。以下是采集到的事实数据：\n\n"
        "{facts}\n\n"
        "请基于以上数据，用中文给出：\n"
        "1. 🔧 开发焦点（今天应该先修什么）\n"
        "2. 📋 可以等一等的事项\n"
        "3. ⏱️ 建议的 2 小时工作块安排\n"
        "4. ⚠️ 阻塞项或风险\n\n"
        "如果 CI 失败，优先分析失败原因。如果日程密集，建议缩短工作块。"
    ),
    summary="开发计划",
)

PROJECT_HEALTH = PlannerProfile(
    name="project_health",
    command="/project_health",
    tool_items=(
        ("github.ci", ""),
        ("github.issues", "open"),
        ("github.prs", "open"),
        ("scheduler_status", ""),
        ("service_status", ""),
        ("logs", "20"),
    ),
    prompt_template=(
        "用户想了解项目健康状态。以下是采集到的事实数据：\n\n"
        "{facts}\n\n"
        "请基于以上数据，用中文给出：\n"
        "1. 🏥 整体健康状态（绿/黄/红）\n"
        "2. ⚠️ 风险项（CI 失败、服务异常、积压 issue）\n"
        "3. 🔴 需要立即修复的组件\n"
        "4. 📌 推荐的下一步行动\n\n"
        "如果有 CI 失败或服务异常，优先分析。如果一切正常，简短确认即可。"
    ),
    summary="项目健康检查",
)

INBOX_TRIAGE = PlannerProfile(
    name="inbox_triage",
    command="/inbox_triage",
    tool_items=(
        ("gmail.recent", "10"),
    ),
    prompt_template=(
        "用户想整理收件箱。以下是最近的邮件数据：\n\n"
        "{facts}\n\n"
        "请基于以上数据，用中文给出：\n"
        "1. 🔴 紧急邮件（需要立即处理）\n"
        "2. 💬 需要回复的邮件\n"
        "3. 📎 低优先级邮件（可以稍后处理）\n"
        "4. ✍️ 建议的回复要点（不发送，仅供参考）\n\n"
        "如果 Gmail 未配置，说明如何配置。不要发送任何邮件。"
    ),
    summary="邮件分类整理",
)

SCHEDULE_REVIEW = PlannerProfile(
    name="schedule_review",
    command="/schedule_review",
    tool_items=(
        ("calendar.today", ""),
        ("calendar.tomorrow", ""),
        ("reminders.list", ""),
    ),
    prompt_template=(
        "用户想审查日程安排。以下是采集到的事实数据：\n\n"
        "{facts}\n\n"
        "请基于以上数据，用中文给出：\n"
        "1. 📅 今明日日程概览\n"
        "2. ⚡ 时间冲突或密集时段\n"
        "3. 🕐 空闲时间块\n"
        "4. 📝 需要准备的事项（会议准备、材料等）\n"
        "5. ➕ 建议添加的提醒（基于日程推断）\n\n"
        "如果日历未配置，说明如何配置。不要创建任何日程或提醒。"
    ),
    summary="日程审查",
)


# All available planner profiles
PLANNER_PROFILES: dict[str, PlannerProfile] = {
    "daily_priority": DAILY_PRIORITY,
    "dev_plan": DEV_PLAN,
    "project_health": PROJECT_HEALTH,
    "inbox_triage": INBOX_TRIAGE,
    "schedule_review": SCHEDULE_REVIEW,
}

# Command name → profile name mapping
PLANNER_COMMAND_MAP: dict[str, str] = {
    profile.command.lstrip("/"): profile.name
    for profile in PLANNER_PROFILES.values()
}


def get_profile(name: str) -> PlannerProfile | None:
    """Get a planner profile by name."""
    return PLANNER_PROFILES.get(name)


def get_profile_by_command(command: str) -> PlannerProfile | None:
    """Get a planner profile by slash command name (without leading /)."""
    profile_name = PLANNER_COMMAND_MAP.get(command)
    if profile_name:
        return PLANNER_PROFILES[profile_name]
    return None


def list_profiles() -> list[PlannerProfile]:
    """List all available planner profiles."""
    return list(PLANNER_PROFILES.values())


def build_planner_prompt(profile: PlannerProfile, facts: str) -> str:
    """Build the hybrid prompt for a planner profile."""
    redacted_facts = redact_text(facts)
    return profile.prompt_template.format(facts=redacted_facts)


def planner_status() -> ToolResult:
    """List all available planner profiles."""
    profiles = list_profiles()
    lines = ["📋 Planner Profiles:", ""]
    for p in profiles:
        lines.append(f"  {p.command} — {p.summary}")
    lines.append("")
    lines.append("使用命令直接运行，或用自然语言描述需求。")
    return ToolResult(ok=True, text="\n".join(lines))


# --- Adapters for personal_tools/registry.py ---

async def planner_status_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    return planner_status()


async def planner_today_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    return ToolResult(ok=True, text=DAILY_PRIORITY.command)


async def planner_dev_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    return ToolResult(ok=True, text=DEV_PLAN.command)


async def planner_health_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    return ToolResult(ok=True, text=PROJECT_HEALTH.command)


async def planner_triage_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    return ToolResult(ok=True, text=INBOX_TRIAGE.command)


async def planner_schedule_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    return ToolResult(ok=True, text=SCHEDULE_REVIEW.command)
