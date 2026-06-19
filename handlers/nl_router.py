"""handlers/nl_router.py — Natural Language Agent Router (P4.3).

Builds a unified tool catalog from both host and personal tool registries,
provides NL pattern matching for domains not covered by intent.py, and
generates /nl_help output.

Classification:
  READ_DETERMINISTIC  — run tool directly
  READ_HYBRID         — collect facts, then Codex synthesis
  WRITE_SAFE_AUTO     — low-risk audited action, executes immediately
  WRITE_CONFIRM_PREVIEW — WRITE/DESTRUCTIVE, must ask confirmation
  CLARIFY             — ask user for missing info
  CODEX_LLM           — open-ended → Codex

Safety: false negatives preferred over unsafe false positives.
WRITE/DESTRUCTIVE never auto-execute from NL.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from handlers.tools.registry import TOOL_REGISTRY, DangerLevel

# Ensure builtin tools are registered.
import handlers.tools.executors  # noqa: F401


class NLCategory(str, Enum):
    READ_DETERMINISTIC = "read_deterministic"
    READ_HYBRID = "read_hybrid"
    WRITE_SAFE_AUTO = "write_safe_auto"
    WRITE_CONFIRM_PREVIEW = "write_confirm_preview"
    CLARIFY = "clarify"
    CODEX_LLM = "codex_llm"


@dataclass(frozen=True)
class NLRoute:
    category: NLCategory
    tool_name: str = ""
    arg: str = ""
    question: str = ""


@dataclass(frozen=True)
class ToolCatalogEntry:
    name: str
    summary: str
    danger: DangerLevel
    keywords: tuple[str, ...]
    examples_zh: tuple[str, ...]
    examples_en: tuple[str, ...]
    domain: str
    # NL support level: "auto" (runs automatically), "clarify" (asks for info),
    # "confirm" (needs confirmation), "example" (catalog example only, no route)
    nl_support: str = "auto"


# ---- Tool Catalog -----------------------------------------------------------

_CATALOG: dict[str, ToolCatalogEntry] = {}


def _build_catalog() -> dict[str, ToolCatalogEntry]:
    """Build unified catalog from host + personal registries."""
    if _CATALOG:
        return _CATALOG

    from personal_tools.registry import PERSONAL_TOOL_REGISTRY, register_personal_tools
    register_personal_tools()

    # Domain definitions with examples and NL support level
    # nl_support: "auto" (runs automatically), "clarify" (asks for info),
    #            "confirm" (needs confirmation), "example" (catalog example only)
    _DOMAIN_DEFS: dict[str, dict] = {
        # --- Host ops ---
        "load": {"domain": "运维", "examples_zh": ["看看负载", "服务器负载怎么样"], "examples_en": ["check load", "server load"]},
        "ps": {"domain": "运维", "examples_zh": ["看看进程", "哪些进程在跑"], "examples_en": ["show processes"]},
        "htop": {"domain": "运维", "examples_zh": ["跑一下 htop"], "examples_en": ["run htop"]},
        "disk": {"domain": "运维", "examples_zh": ["磁盘空间还够吗", "看看磁盘"], "examples_en": ["disk usage"]},
        "logs": {"domain": "运维", "examples_zh": ["看看日志", "最近日志"], "examples_en": ["show logs"]},
        "service_status": {"domain": "运维", "examples_zh": ["服务还在跑吗", "bot 状态"], "examples_en": ["service status"]},
        "git_status": {"domain": "运维", "examples_zh": ["git status", "代码改了什么"], "examples_en": ["git status"]},
        "service_restart": {"domain": "运维", "examples_zh": ["重启 telegram bot"], "examples_en": ["restart telegram"], "nl_support": "confirm"},
        # --- Notes ---
        "notes.add": {"domain": "笔记", "examples_zh": ["记一下明天开会", "记 xxx"], "examples_en": ["note xxx"]},
        "notes.search": {"domain": "笔记", "examples_zh": ["搜一下笔记里的 deploy", "笔记里有没有 OAuth"], "examples_en": ["search notes for deploy"]},
        "notes.list_recent": {"domain": "笔记", "examples_zh": ["看看最近的笔记", "笔记列表"], "examples_en": ["recent notes"], "nl_support": "example"},
        # --- Reminders ---
        "reminders.create": {"domain": "提醒", "examples_zh": ["提醒我明天9点开会", "提醒我下午3点xxx"], "examples_en": ["remind me tomorrow 9am"]},
        "reminders.list": {"domain": "提醒", "examples_zh": ["看看提醒", "有什么提醒"], "examples_en": ["list reminders"], "nl_support": "example"},
        # --- Gmail ---
        "gmail.status": {"domain": "邮件", "examples_zh": ["邮箱状态", "gmail 连上了吗"], "examples_en": ["gmail status"]},
        "gmail.recent": {"domain": "邮件", "examples_zh": ["看看最近的邮件", "收件箱有什么"], "examples_en": ["recent emails"]},
        "gmail.search": {"domain": "邮件", "examples_zh": ["搜索邮件关于发票", "找一下邮件里的快递"], "examples_en": ["search email for invoice"]},
        "email.send": {"domain": "邮件", "examples_zh": ["发邮件给 x"], "examples_en": ["send email to x"], "nl_support": "clarify"},
        # --- Calendar ---
        "calendar.today": {"domain": "日历", "examples_zh": ["今天有什么安排", "今天的日程"], "examples_en": ["today's schedule"]},
        "calendar.tomorrow": {"domain": "日历", "examples_zh": ["明天有什么安排", "明天日程"], "examples_en": ["tomorrow's schedule"]},
        "calendar.week": {"domain": "日历", "examples_zh": ["本周日程", "这周有什么安排"], "examples_en": ["this week's schedule"]},
        "calendar.search": {"domain": "日历", "examples_zh": ["搜索日程关于会议", "有没有关于出差的日程"], "examples_en": ["search calendar for meeting"]},
        "calendar.freebusy": {"domain": "日历", "examples_zh": ["下午有空吗", "查询忙闲"], "examples_en": ["am I free this afternoon"]},
        "calendar.create": {"domain": "日历", "examples_zh": ["创建日程", "安排一个会议"], "examples_en": ["create calendar event"], "nl_support": "clarify"},
        # --- Contacts ---
        "contacts.search": {"domain": "联系人", "examples_zh": ["搜索联系人张三", "找一下李四的电话"], "examples_en": ["search contacts for John"]},
        # --- Google OAuth ---
        "google.status": {"domain": "Google", "examples_zh": ["google 状态", "OAuth 连上了吗"], "examples_en": ["google status"]},
        # --- Briefing ---
        "briefing.today": {"domain": "简报", "examples_zh": ["今日简报", "今天简报"], "examples_en": ["today's briefing"]},
        "briefing.tomorrow": {"domain": "简报", "examples_zh": ["明日简报", "明天简报"], "examples_en": ["tomorrow's briefing"]},
        "briefing.status": {"domain": "简报", "examples_zh": ["简报设置", "简报状态"], "examples_en": ["briefing status"]},
        "briefing.enable": {"domain": "简报", "examples_zh": ["启用简报", "每天发简报"], "examples_en": ["enable briefing"]},
        "briefing.disable": {"domain": "简报", "examples_zh": ["禁用简报", "关闭简报"], "examples_en": ["disable briefing"], "nl_support": "confirm"},
        # --- GitHub ---
        "github.status": {"domain": "GitHub", "examples_zh": ["github 状态", "github 连上了吗"], "examples_en": ["github status"]},
        "github.issues": {"domain": "GitHub", "examples_zh": ["看看 issue", "列出 open issue"], "examples_en": ["list issues"]},
        "github.issue": {"domain": "GitHub", "examples_zh": ["查看 issue #42"], "examples_en": ["show issue #42"]},
        "github.prs": {"domain": "GitHub", "examples_zh": ["看看 PR", "列出 open PR"], "examples_en": ["list PRs"]},
        "github.pr": {"domain": "GitHub", "examples_zh": ["查看 PR #10"], "examples_en": ["show PR #10"]},
        "github.ci": {"domain": "GitHub", "examples_zh": ["CI 挂了吗", "构建状态"], "examples_en": ["CI status"]},
        "github.create_issue": {"domain": "GitHub", "examples_zh": ["创建 issue", "提个 bug"], "examples_en": ["create issue"], "nl_support": "clarify"},
        "github.comment": {"domain": "GitHub", "examples_zh": ["评论 issue #42"], "examples_en": ["comment on issue #42"], "nl_support": "clarify"},
        # --- Planner ---
        "planner.today": {"domain": "规划", "examples_zh": ["今天应该先干啥", "今日优先级"], "examples_en": ["what should I do today"]},
        "planner.dev": {"domain": "规划", "examples_zh": ["今天开发计划", "开发计划"], "examples_en": ["dev plan"]},
        "planner.health": {"domain": "规划", "examples_zh": ["项目健康状态", "项目有问题吗"], "examples_en": ["project health"]},
        "planner.triage": {"domain": "规划", "examples_zh": ["帮我整理邮件", "邮件分类"], "examples_en": ["triage inbox"]},
        "planner.schedule": {"domain": "规划", "examples_zh": ["今天日程安排", "日程审查"], "examples_en": ["schedule review"]},
        # --- Projects ---
        "projects.list": {"domain": "项目", "examples_zh": ["项目列表", "看看我的项目"], "examples_en": ["list projects"]},
        "projects.add": {"domain": "项目", "examples_zh": ["添加项目"], "examples_en": ["add project"], "nl_support": "example"},
        "projects.use": {"domain": "项目", "examples_zh": ["切换项目到 1", "用项目 2"], "examples_en": ["switch to project 1"]},
        "projects.show": {"domain": "项目", "examples_zh": ["项目详情", "看看项目 1"], "examples_en": ["show project"], "nl_support": "example"},
        "project.status": {"domain": "项目", "examples_zh": ["项目状态", "当前项目怎么样"], "examples_en": ["project status"]},
        "project.health": {"domain": "项目", "examples_zh": ["项目健康检查"], "examples_en": ["project health check"]},
        "project.roadmap": {"domain": "项目", "examples_zh": ["项目 roadmap", "路线图"], "examples_en": ["project roadmap"]},
        "project.next": {"domain": "项目", "examples_zh": ["项目下一步", "下一步做什么"], "examples_en": ["next steps"]},
        "project.release_checklist": {"domain": "项目", "examples_zh": ["发布清单", "release checklist"], "examples_en": ["release checklist"]},
        "project.brief": {"domain": "项目", "examples_zh": ["项目简报"], "examples_en": ["project brief"], "nl_support": "example"},
        "project.export": {"domain": "项目", "examples_zh": ["导出项目", "导出项目为 JSON"], "examples_en": ["export project"], "nl_support": "example"},
        "project.template": {"domain": "项目", "examples_zh": ["项目模板"], "examples_en": ["project template"], "nl_support": "example"},
        "project.import": {"domain": "项目", "examples_zh": ["导入项目"], "examples_en": ["import project"], "nl_support": "example"},
        # --- Setup ---
        "setup.status": {"domain": "设置", "examples_zh": ["配置状态", "设置怎么样了"], "examples_en": ["setup status"]},
        "setup.check": {"domain": "设置", "examples_zh": ["检查清单", "设置检查"], "examples_en": ["setup check"], "nl_support": "example"},
        # --- Web / Research ---
        "web.fetch": {"domain": "Web", "examples_zh": ["获取网页 https://example.com"], "examples_en": ["fetch https://example.com"]},
        "web.search": {"domain": "Web", "examples_zh": ["搜索 Python asyncio", "搜一下 AI 新闻"], "examples_en": ["search for Python asyncio"]},
        "research.run": {"domain": "研究", "examples_zh": ["研究一下 React Native", "调研 AI 编程助手"], "examples_en": ["research about React Native"]},
        "research.project": {"domain": "研究", "examples_zh": ["研究一下项目里的认证方案"], "examples_en": ["research auth in project"], "nl_support": "example"},
        # --- File Search / KB ---
        "files.list_roots": {"domain": "文件", "examples_zh": ["搜索根目录"], "examples_en": ["list search roots"], "nl_support": "example"},
        "files.search": {"domain": "文件", "examples_zh": ["搜索文件 deploy", "找一下文档"], "examples_en": ["search files for deploy"]},
        "files.read": {"domain": "文件", "examples_zh": ["读取文件 README.md"], "examples_en": ["read file README.md"], "nl_support": "example"},
        "kb.index": {"domain": "知识库", "examples_zh": ["索引知识库"], "examples_en": ["index knowledge base"], "nl_support": "example"},
        "kb.status": {"domain": "知识库", "examples_zh": ["知识库状态"], "examples_en": ["KB status"], "nl_support": "example"},
        "kb.search": {"domain": "知识库", "examples_zh": ["知识库里搜索 OAuth"], "examples_en": ["search KB for OAuth"]},
        "kb.collect_facts": {"domain": "知识库", "examples_zh": ["收集文档证据关于 deploy"], "examples_en": ["collect evidence about deploy"]},
        # --- Queue ---
        "queue.status": {"domain": "队列", "examples_zh": ["队列状态", "看看队列"], "examples_en": ["queue status"]},
        "scheduler_status": {"domain": "调度", "examples_zh": ["调度器状态", "提醒调度器状态"], "examples_en": ["scheduler status"], "nl_support": "example"},
    }

    # Build catalog entries
    for name, spec in TOOL_REGISTRY.items():
        domain_def = _DOMAIN_DEFS.get(name, {})
        _CATALOG[name] = ToolCatalogEntry(
            name=name,
            summary=spec.summary,
            danger=spec.danger,
            keywords=spec.keywords,
            examples_zh=tuple(domain_def.get("examples_zh", ())),
            examples_en=tuple(domain_def.get("examples_en", ())),
            domain=domain_def.get("domain", "其他"),
            nl_support=domain_def.get("nl_support", "auto"),
        )

    # Add personal tools
    for name, spec in PERSONAL_TOOL_REGISTRY.items():
        if name in _CATALOG:
            continue
        domain_def = _DOMAIN_DEFS.get(name, {})
        _CATALOG[name] = ToolCatalogEntry(
            name=name,
            summary=spec.summary,
            danger=spec.danger,
            keywords=spec.keywords,
            examples_zh=tuple(domain_def.get("examples_zh", ())),
            examples_en=tuple(domain_def.get("examples_en", ())),
            domain=domain_def.get("domain", "其他"),
            nl_support=domain_def.get("nl_support", "auto"),
        )

    return _CATALOG


def get_catalog() -> dict[str, ToolCatalogEntry]:
    """Return the tool catalog (builds on first call)."""
    return _build_catalog()


def get_catalog_entry(name: str) -> ToolCatalogEntry | None:
    """Return a single catalog entry."""
    return get_catalog().get(name)


# ---- Additional NL patterns for uncovered domains --------------------------

# Notes: "记 xxx" is handled by memo detection in dispatch.py, but
# "搜索笔记 xxx" / "笔记里有没有 xxx" should route to notes.search.
_NOTES_SEARCH_PATTERNS = (
    re.compile(r"(搜|找|search).*(笔记|notes|备忘)", re.IGNORECASE),
    re.compile(r"(笔记|notes|备忘).*(里|中).*(搜|找|search|关于|有没有)", re.IGNORECASE),
)

# Reminders: "提醒我 xxx" patterns
_REMINDERS_CREATE_PATTERNS = (
    re.compile(r"(提醒|remind)\s*(我|me)\s*(.+)", re.IGNORECASE),
)

# Calendar freebusy
_CALENDAR_FREEBUSY_PATTERNS = (
    re.compile(r"(下午|上午|早上|晚上|中午|\d+:\d+).*(有空|忙吗|有时间|free|busy)", re.IGNORECASE),
    re.compile(r"(有空|忙吗|有时间).*(下午|上午|早上|晚上|中午|\d+:\d+)", re.IGNORECASE),
    re.compile(r"(查询|查|看看).*(忙闲|free\s*busy)", re.IGNORECASE),
)

# Queue status (job queue, not reminder scheduler)
_QUEUE_PATTERNS = (
    re.compile(r"(队列|queue).*(状态|status|看看|怎么样)", re.IGNORECASE),
    re.compile(r"(看看|查).*(队列|queue)", re.IGNORECASE),
)

# Setup status
_SETUP_PATTERNS = (
    re.compile(r"(配置|设置|setup).*(状态|status|怎么样|检查|check)", re.IGNORECASE),
    re.compile(r"(看看|查).*(配置|设置|setup)", re.IGNORECASE),
)


def classify_nl(text: str) -> NLRoute:
    """Classify natural language text into an NL route.

    This is called AFTER intent.py's existing patterns, so it only
    handles domains not already covered there.
    """
    body = (text or "").strip()
    if not body:
        return NLRoute(category=NLCategory.CODEX_LLM)

    # Notes search
    for pat in _NOTES_SEARCH_PATTERNS:
        if pat.search(body):
            return NLRoute(category=NLCategory.READ_DETERMINISTIC, tool_name="notes.search", arg=body)

    # Reminders create — WRITE_SAFE_AUTO, executes immediately with audit
    for pat in _REMINDERS_CREATE_PATTERNS:
        m = pat.search(body)
        if m:
            return NLRoute(category=NLCategory.WRITE_SAFE_AUTO, tool_name="reminders.create", arg=m.group(3).strip())

    # Calendar freebusy
    for pat in _CALENDAR_FREEBUSY_PATTERNS:
        if pat.search(body):
            return NLRoute(category=NLCategory.READ_DETERMINISTIC, tool_name="calendar.freebusy", arg=body)

    # Queue status (job queue)
    for pat in _QUEUE_PATTERNS:
        if pat.search(body):
            return NLRoute(category=NLCategory.READ_DETERMINISTIC, tool_name="queue.status")

    # Setup
    for pat in _SETUP_PATTERNS:
        if pat.search(body):
            return NLRoute(category=NLCategory.READ_DETERMINISTIC, tool_name="setup.status")

    # Default: let Codex handle it
    return NLRoute(category=NLCategory.CODEX_LLM)


# ---- /nl_help ----------------------------------------------------------------

def build_nl_help() -> str:
    """Build /nl_help output grouped by domain with honest support levels."""
    catalog = get_catalog()

    # Group by domain
    by_domain: dict[str, list[ToolCatalogEntry]] = {}
    for entry in catalog.values():
        if not entry.examples_zh:
            continue
        by_domain.setdefault(entry.domain, []).append(entry)

    # Domain order
    domain_order = ["运维", "笔记", "提醒", "邮件", "日历", "联系人", "简报", "GitHub", "规划", "项目", "设置", "Web", "研究", "文件", "知识库", "队列", "调度", "其他"]

    lines = ["自然语言命令示例（NL-first，斜杠命令为后备）：", ""]

    for domain in domain_order:
        entries = by_domain.get(domain)
        if not entries:
            continue
        lines.append(f"【{domain}】")
        for entry in entries:
            examples = entry.examples_zh[:2]
            for ex in examples:
                support_tag = _get_support_tag(entry)
                lines.append(f"  「{ex}」{support_tag}")
        lines.append("")

    lines.append("说明：")
    lines.append("  无标记 = 可直接执行（READ）")
    lines.append("  [自动] = WRITE_SAFE 自动执行（有审计日志）")
    lines.append("  [需确认] = WRITE/DESTRUCTIVE 需要确认")
    lines.append("  [会追问] = 缺少参数，会用自然语言追问")
    lines.append("  [示例] = 仅作参考，暂无 NL 路由")

    return "\n".join(lines)


def _get_support_tag(entry: ToolCatalogEntry) -> str:
    """Get support level tag for an example."""
    nl_support = entry.nl_support

    if nl_support == "clarify":
        return " [会追问]"
    if nl_support == "confirm":
        return " [需确认]"
    if nl_support == "example":
        return " [示例]"

    # nl_support == "auto" — check danger level for more specific tag
    if entry.danger == DangerLevel.WRITE_SAFE:
        return " [自动]"
    if entry.danger == DangerLevel.WRITE:
        return " [需确认]"
    if entry.danger == DangerLevel.DESTRUCTIVE:
        return " [需确认]"

    # READ — no tag needed
    return ""


# ---- Example phrases for smokes / docs --------------------------------------

NL_EXAMPLES: dict[str, list[str]] = {
    "今天有什么安排": "calendar.today",
    "明天日程": "calendar.tomorrow",
    "本周日程": "calendar.week",
    "帮我整理邮件": "planner.triage",
    "CI 挂了吗": "github.ci",
    "研究一下 React Native 状态管理": "research.run",
    "记一下 xxx": "notes.add",
    "提醒我明天9点开会": "reminders.create",
    "看看最近的邮件": "gmail.recent",
    "搜索邮件关于发票": "gmail.search",
    "项目列表": "projects.list",
    "项目健康检查": "project.health",
    "看看 issue": "github.issues",
    "搜索 Python asyncio": "web.search",
    "知识库状态": "kb.status",
    "配置状态": "setup.status",
    "队列状态": "queue.status",
}
