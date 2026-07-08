"""handlers/intent.py — lightweight intent router for the agent tool layer.

Routes free-text messages to one of three paths:
- deterministic: run registered tools directly (no Codex)
- hybrid: collect tool facts, then pass to Codex for analysis
- llm: open-ended coding/debugging → Codex only

Conservative matching: false negatives preferred over hijacking
coding requests.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from handlers.ops import detect_ops_intent
from handlers.tools.diagnose import DIAGNOSE_MODES
from handlers.tools.registry import TOOL_REGISTRY
from handlers.tools.restart_aliases import RESTART_ALIASES, RESTART_ALIASES_ZH

# Ensure builtin tools are registered before route_intent uses TOOL_REGISTRY.
import handlers.tools.executors  # noqa: F401

RouteKind = Literal["deterministic", "hybrid", "llm"]


@dataclass(frozen=True)
class RouteResult:
    kind: RouteKind
    tools: tuple[str, ...] = ()
    tool_items: tuple[tuple[str, str], ...] = ()
    question: str = ""
    arg: str = ""


# ---- Hybrid: diagnosis / analysis questions --------------------------------

_HYBRID_PATTERNS = (
    re.compile(
        r"(为什么|为啥|怎么回事|分析一下|诊断|help.*分析|why\s+is|what.*wrong)"
        r".*(慢|卡|高|异常|problem|slow|high|issue|down)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(服务器|vps|主机|机器|server|host).*(慢|卡|异常|问题|issue|slow)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(帮|help).*(看看|分析|诊断|check).*(服务器|vps|负载|server|load)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(为什么|why).*(负载|load|cpu|内存|memory|disk).*(高|满|full|high)",
        re.IGNORECASE,
    ),
)

_HYBRID_DEFAULT_TOOLS = ("load", "ps", "disk", "service_status")

# Explicit diagnose phrasing (conservative; no coding/docs hijack).
_DIAGNOSE_SERVER_PATTERNS = (
    re.compile(r"(诊断|diagnose).*(服务器|server|vps|主机|host)", re.IGNORECASE),
    re.compile(r"(帮我|请).*(诊断|看看|分析).*(服务器|vps|主机)", re.IGNORECASE),
)
_DIAGNOSE_BOT_PATTERNS = (
    re.compile(r"(诊断|diagnose).*\b(bot|机器人)\b", re.IGNORECASE),
    re.compile(r"(帮我|请).*诊断.*\b(bot|机器人)\b", re.IGNORECASE),
)
_DIAGNOSE_LOGS_PATTERNS = (
    re.compile(r"(诊断|分析).*(日志|log|journal)", re.IGNORECASE),
)
# Coding/docs guard: skip diagnose if clearly about source/docs/code.
_CODING_GUARD = re.compile(
    r"(source\s*code|源码|改.*代码|写.*文档|docs?\s+about|implement|refactor|debug\s+code)",
    re.IGNORECASE,
)


# ---- Deterministic tool patterns (beyond legacy ops) -----------------------

_DISK_PATTERNS = (
    re.compile(r"(看看|查|看).*(磁盘|disk|df|空间|storage)", re.IGNORECASE),
    re.compile(r"(disk|storage)\s*(usage|space|full)", re.IGNORECASE),
)
_LOGS_PATTERNS = (
    re.compile(r"(看看|查|看|tail).*(日志|log|journal)", re.IGNORECASE),
    re.compile(r"(journalctl|service\s+log)", re.IGNORECASE),
)
_SERVICE_PATTERNS = (
    re.compile(r"(服务|service|bot).*(状态|status|还在|running|alive)", re.IGNORECASE),
    re.compile(r"(systemctl|服务状态)", re.IGNORECASE),
)
_GIT_PATTERNS = (
    re.compile(r"\bgit\s+status\b", re.IGNORECASE),
    re.compile(r"(代码|git).*(改了什么|变更|改动|diff|status)", re.IGNORECASE),
)
_RESTART_PATTERNS = (
    re.compile(r"(重启|restart).*(bot|服务|service|conveyor|telegram|feishu|飞书|电报|维护|maintain)", re.IGNORECASE),
)

# Gmail / email intent patterns (P3.3)
_GMAIL_STATUS_PATTERNS = (
    re.compile(r"(gmail|邮箱|邮件).*(状态|连接|status|connect)", re.IGNORECASE),
    re.compile(r"(邮箱|邮件).*(正常|好使|能用|ok)", re.IGNORECASE),
)
_GMAIL_RECENT_PATTERNS = (
    re.compile(r"(看看|查|看|最近).*(收件箱|邮件|inbox|email|gmail)", re.IGNORECASE),
    re.compile(r"(收件箱|inbox).*(里有什么|最近|新邮件)", re.IGNORECASE),
    re.compile(r"(最近|latest).*(邮件|email|mail)", re.IGNORECASE),
)
_GMAIL_SEARCH_PATTERNS = (
    re.compile(r"(搜索|搜|找|search).*(邮件|email|gmail|mail)", re.IGNORECASE),
    re.compile(r"(邮件|email).*(搜|找|search)", re.IGNORECASE),
)
_EMAIL_SEND_PATTERNS = (
    re.compile(r"(发|send).*(邮件|email|mail|gmail)", re.IGNORECASE),
    re.compile(r"(写|compose).*(邮件|email)", re.IGNORECASE),
)

# Calendar intent patterns (P3.4)
_CALENDAR_TODAY_PATTERNS = (
    re.compile(r"(今天|today).*(日程|日历|行程|calendar|schedule|安排)", re.IGNORECASE),
    re.compile(r"(看看|查|看).*(今天|今日).*(日程|行程|安排)", re.IGNORECASE),
    re.compile(r"(日程|日历|行程).*(今天|today)", re.IGNORECASE),
    re.compile(r"(今天|today).*(有什么|有啥|啥事)", re.IGNORECASE),
)
_CALENDAR_TOMORROW_PATTERNS = (
    re.compile(r"(明天|tomorrow).*(日程|日历|行程|calendar|schedule|安排)", re.IGNORECASE),
    re.compile(r"(看看|查|看).*(明天|明日).*(日程|行程|安排)", re.IGNORECASE),
    re.compile(r"(明天|明日).*(有什么|有啥|啥事)", re.IGNORECASE),
)
_CALENDAR_WEEK_PATTERNS = (
    re.compile(r"(本周|这周|this week).*(日程|日历|行程|calendar|schedule)", re.IGNORECASE),
    re.compile(r"(看看|查|看).*(本周|这周).*(日程|行程|安排)", re.IGNORECASE),
)
_CALENDAR_SEARCH_PATTERNS = (
    re.compile(r"(搜索|搜|找|search).*(日程|日历|行程|calendar|schedule)", re.IGNORECASE),
    re.compile(r"(日程|日历|行程).*(搜|找|search)", re.IGNORECASE),
)
_CALENDAR_CREATE_PATTERNS = (
    re.compile(r"(创建|新建|添加|add|create).*(日程|日历|行程|calendar|event)", re.IGNORECASE),
    re.compile(r"(安排|约|schedule).*(会议|meeting|日程)", re.IGNORECASE),
)

# Contacts intent patterns (P3.4)
_CONTACTS_SEARCH_PATTERNS = (
    re.compile(r"(搜索|搜|找|search).*(联系人|通讯录|contacts|contact)", re.IGNORECASE),
    re.compile(r"(联系人|通讯录|contacts).*(搜|找|search)", re.IGNORECASE),
    re.compile(r"(找|查|look up).*(电话|号码|phone).*(联系人|通讯录)?", re.IGNORECASE),
)

# Google OAuth patterns (P3.4)
_GOOGLE_AUTH_PATTERNS = (
    re.compile(r"(授权|auth|login|登录).*(google|谷歌|日历|calendar|contacts)", re.IGNORECASE),
    re.compile(r"(google|谷歌).*(授权|auth)", re.IGNORECASE),
)

# Daily Briefing patterns (P3.5)
_BRIEFING_TODAY_PATTERNS = (
    re.compile(r"(今日|今天|today).*(简报|briefing|brief)", re.IGNORECASE),
    re.compile(r"(简报|briefing|brief).*(今日|今天|today)", re.IGNORECASE),
    re.compile(r"(看看|看|给).*(今天|今日).*(简报|安排|计划)", re.IGNORECASE),
)
_BRIEFING_TOMORROW_PATTERNS = (
    re.compile(r"(明日|明天|tomorrow).*(简报|briefing|brief)", re.IGNORECASE),
    re.compile(r"(简报|briefing|brief).*(明日|明天|tomorrow)", re.IGNORECASE),
    re.compile(r"(看看|看).*(明天|明日).*(简报|安排|计划)", re.IGNORECASE),
)
_BRIEFING_STATUS_PATTERNS = (
    re.compile(r"(简报|briefing).*(设置|状态|status|config)", re.IGNORECASE),
    re.compile(r"(设置|查看).*(简报|briefing)", re.IGNORECASE),
)
_BRIEFING_ENABLE_PATTERNS = (
    re.compile(r"(启用|开启|enable|设置).*(简报|briefing|每日)", re.IGNORECASE),
    re.compile(r"(简报|briefing).*(启用|开启|enable)", re.IGNORECASE),
    re.compile(r"(每天|每日).*(发送|推送).*(简报|briefing)", re.IGNORECASE),
)
_BRIEFING_DISABLE_PATTERNS = (
    re.compile(r"(禁用|关闭|disable|取消).*(简报|briefing|每日)", re.IGNORECASE),
    re.compile(r"(简报|briefing).*(禁用|关闭|disable)", re.IGNORECASE),
)

# GitHub intent patterns (P3.6)
_GITHUB_STATUS_PATTERNS = (
    re.compile(r"(github).*(状态|连接|status|connect)", re.IGNORECASE),
    re.compile(r"(github).*(正常|好使|能用|ok)", re.IGNORECASE),
)
_GITHUB_ISSUES_PATTERNS = (
    re.compile(r"(看看|查|看|列出|list).*(issue|issues|问题单)", re.IGNORECASE),
    re.compile(r"(issue|issues|问题单).*(看看|查|看|列出|list)", re.IGNORECASE),
    re.compile(r"(open|closed|all)\s*(issue|issues)", re.IGNORECASE),
)
_GITHUB_ISSUE_DETAIL_PATTERNS = (
    re.compile(r"(查看|看|详情|detail).*#?\s*(\d+)\s*(issue)?", re.IGNORECASE),
    re.compile(r"(issue|问题单)\s*#?\s*(\d+)", re.IGNORECASE),
)
_GITHUB_PRS_PATTERNS = (
    re.compile(r"(看看|查|看|列出|list).*(pr|pull\s*request|prs|合并请求)", re.IGNORECASE),
    re.compile(r"(pr|pull\s*request|prs|合并请求).*(看看|查|看|列出|list)", re.IGNORECASE),
    re.compile(r"(open|closed|all|merged)\s*(pr|pull\s*request)", re.IGNORECASE),
    re.compile(r"(pr|pull\s*request|prs|合并请求).*(状态|status)", re.IGNORECASE),
    re.compile(r"(状态|status).*(pr|pull\s*request|prs)", re.IGNORECASE),
)
_GITHUB_PR_DETAIL_PATTERNS = (
    re.compile(r"(查看|看|详情|detail).*#?\s*(\d+)\s*(pr|pull\s*request)?", re.IGNORECASE),
    re.compile(r"(pr|pull\s*request|合并请求)\s*#?\s*(\d+)", re.IGNORECASE),
)
_GITHUB_CI_PATTERNS = (
    re.compile(r"(ci|构建|build).*(状态|挂|失败|成功|status|fail|pass)", re.IGNORECASE),
    re.compile(r"(看看|查|看).*(ci|构建|build)", re.IGNORECASE),
    re.compile(r"(ci|build).*(挂了吗|怎么样|如何|ok)", re.IGNORECASE),
    re.compile(r"(挂了吗|ci\s*挂了)", re.IGNORECASE),
)
_GITHUB_CREATE_ISSUE_PATTERNS = (
    re.compile(r"(创建|新建|提|开|create|open|file).*(issue|问题单|bug|ticket)", re.IGNORECASE),
    re.compile(r"(issue|问题单).*(创建|新建|提|开|create)", re.IGNORECASE),
    re.compile(r"(提个|开个|建个).*(issue|bug|问题)", re.IGNORECASE),
)
_GITHUB_COMMENT_PATTERNS = (
    re.compile(r"(评论|comment).*(issue|pr|pull\s*request|问题单)", re.IGNORECASE),
    re.compile(r"(issue|pr|pull\s*request|问题单).*(评论|comment)", re.IGNORECASE),
    re.compile(r"(回复|reply).*(#|号)\s*\d+", re.IGNORECASE),
)

# Planner intent patterns (P3.7)
_PLANNER_TODAY_PATTERNS = (
    re.compile(r"(我今天|今天|today).*(应该|先|干啥|干什么|做什么|做什么好)", re.IGNORECASE),
    re.compile(r"(应该|先).*(干啥|干什么|做什么).*(今天|today)?", re.IGNORECASE),
    re.compile(r"(优先|priority).*(事项|任务|今天)", re.IGNORECASE),
    re.compile(r"(今天|today).*(优先|重点|先做)", re.IGNORECASE),
)
_PLANNER_DEV_PATTERNS = (
    re.compile(r"(今天|today|今天).*(开发|dev).*(计划|plan)", re.IGNORECASE),
    re.compile(r"(开发|dev).*(计划|plan)", re.IGNORECASE),
    re.compile(r"(今天|today).*(写|改|修|开发).*(什么|啥)", re.IGNORECASE),
    re.compile(r"(制定|给).*(开发|dev).*(计划|plan)", re.IGNORECASE),
)
_PLANNER_HEALTH_PATTERNS = (
    re.compile(r"(项目|project).*(健康|health).*(状态|status)?", re.IGNORECASE),
    re.compile(r"(conveyor|项目).*(有没有|有啥|有什么).*(问题|issue|bug)", re.IGNORECASE),
    re.compile(r"(项目|project).*(状态|status|检查|check)", re.IGNORECASE),
    re.compile(r"(有没有问题|有问题吗|健康吗)", re.IGNORECASE),
)
_PLANNER_TRIAGE_PATTERNS = (
    re.compile(r"(帮|help).*(我|me).*(整理|triage|分类).*(邮件|email|inbox|收件箱)", re.IGNORECASE),
    re.compile(r"(整理|triage|分类).*(邮件|email|inbox|收件箱)", re.IGNORECASE),
    re.compile(r"(邮件|email|inbox).*(整理|triage|分类)", re.IGNORECASE),
)
_PLANNER_SCHEDULE_PATTERNS = (
    re.compile(r"(今天|today).*(日程|schedule).*(安排|review|审查)", re.IGNORECASE),
    re.compile(r"(日程|schedule).*(安排|审查|review)", re.IGNORECASE),
    re.compile(r"(看看|看).*(日程|行程).*(安排|有没有冲突)", re.IGNORECASE),
)

# Project intent patterns (P3.9)
_PROJECT_LIST_PATTERNS = (
    re.compile(r"(项目列表|列出项目|list\s*projects?|看看项目)", re.IGNORECASE),
    re.compile(r"(我有哪些|看看我的).*(项目|project)", re.IGNORECASE),
)
_PROJECT_USE_PATTERNS = (
    re.compile(r"(切换|switch|use).*(项目|project)", re.IGNORECASE),
    re.compile(r"(用|use).*(项目|project)\s*(\d+|\w+)", re.IGNORECASE),
)
_PROJECT_NEXT_PATTERNS = (
    re.compile(r"(这个|当前|active).*(项目|project).*(下一步|next|做什么|干啥)", re.IGNORECASE),
    re.compile(r"(项目|project).*(下一步|next\s*step|做什么|干啥)", re.IGNORECASE),
    re.compile(r"(下一步|next).*(做什么|干啥)", re.IGNORECASE),
)
_PROJECT_HEALTH_PATTERNS = (
    re.compile(r"(项目|project).*(健康|health).*(状态|status)?", re.IGNORECASE),
    re.compile(r"(项目|project).*(有没有|有啥|有什么).*(问题|issue|bug)", re.IGNORECASE),
)
_PROJECT_ROADMAP_PATTERNS = (
    re.compile(r"(项目|project).*(roadmap|路线图|规划)", re.IGNORECASE),
    re.compile(r"(看看|看).*(项目|project).*(roadmap|路线图)", re.IGNORECASE),
)
_PROJECT_RELEASE_PATTERNS = (
    re.compile(r"(生成|generate|看看).*(发布|release).*(清单|checklist)", re.IGNORECASE),
    re.compile(r"(发布|release).*(清单|checklist)", re.IGNORECASE),
)

# File Search / Knowledge Base intent patterns (P4.2)
_FILE_SEARCH_PATTERNS = (
    re.compile(r"(找一下|查找|搜索|搜|search).*(文档|文件|file|doc)", re.IGNORECASE),
    re.compile(r"(文档|文件|file|doc).*(里|中|内).*(关于|about|有没有|怎么说|contains)", re.IGNORECASE),
    re.compile(r"(README|文档|说明|doc).*(里|中).*(有没有|怎么说|关于|about)", re.IGNORECASE),
    re.compile(r"(本地|local).*(文档|文件|file|search|搜索)", re.IGNORECASE),
    re.compile(r"(根据|based on|from).*(本地|local|文档|doc).*(总结|总结|summarize)", re.IGNORECASE),
    re.compile(r"(notes|笔记|备忘).*(里|中).*(关于|about|有没有|contains)", re.IGNORECASE),
    re.compile(r"(知识库|kb|knowledge\s*base).*(里|中).*(搜索|search|关于|about)", re.IGNORECASE),
    re.compile(r"^(搜索|search|找|查找)\s+(本地|local|文档|file|notes)", re.IGNORECASE),
)

# Web / Research intent patterns (P4.1)
_WEB_FETCH_PATTERNS = (
    re.compile(r"(获取|抓取|fetch|打开|open).*(网页|页面|web|page|url)", re.IGNORECASE),
    re.compile(r"(网页|页面|web|page).*(内容|content|看看)", re.IGNORECASE),
    re.compile(r"(帮我|请).*(打开|看看|获取).*(http|www)", re.IGNORECASE),
)

# ---- Execution nodes / Computer Use intent (P5.0 phase 0) ----------------
#
# These patterns detect desktop-target requests. They NEVER trigger
# real desktop control in this task — they route to the
# ``nodes.status`` / ``computer.status`` stubs so the user gets a
# clear "not implemented" message instead of Codex hallucinating
# what is on the operator's screen.
#
# Conservative matching: a phrase must look like the user is
# talking to *their* machine, not to the VPS. A vague "open Xcode"
# without "my Mac" / "我的 Mac" / "MacBook" is left to Codex.
_NODES_STATUS_PATTERNS = (
    re.compile(r"(我的节点|机器状态|主机状态|node\s*status|nodes\s*status|执行节点|节点状态|(?:macbook|mac|电脑|本机)\s*在线)", re.IGNORECASE),
)

# P5.2.1 / P5.3: screenshot metadata/status queries (no capture).
_SCREENSHOT_STATUS_PATTERNS = (
    re.compile(r"截图状态", re.IGNORECASE),
    re.compile(r"最近的截图", re.IGNORECASE),
    re.compile(r"看看最近截图", re.IGNORECASE),
    re.compile(r"desktop\s*screenshot\s*status", re.IGNORECASE),
    re.compile(r"latest\s*desktop\s*screenshot", re.IGNORECASE),
    re.compile(r"mac\s*截图状态", re.IGNORECASE),
    re.compile(r"observe\s*status", re.IGNORECASE),
)

# P5.4: remote upload request phrases.
_DESKTOP_UPLOAD_PATTERNS = (
    re.compile(r"把刚才截图发我", re.IGNORECASE),
    re.compile(r"发一下刚才的截图", re.IGNORECASE),
    re.compile(r"上传刚才截图", re.IGNORECASE),
    re.compile(r"给我看看刚才截图预览", re.IGNORECASE),
    re.compile(r"send\s+the\s+latest\s+screenshot\s+preview", re.IGNORECASE),
    re.compile(r"upload\s+screenshot\s+preview", re.IGNORECASE),
)


# P5.3: remote observe request phrases (create pending request).
_OBSERVE_REQUEST_PATTERNS = (
    re.compile(r"截图看看我电脑现在是什么", re.IGNORECASE),
    re.compile(r"帮我截一下\s*mac\s*屏幕", re.IGNORECASE),
    re.compile(r"看一下\s*(macbook|mac|电脑)\s*屏幕", re.IGNORECASE),
    re.compile(r"take\s+a\s+screenshot\s+on\s+my\s+desktop", re.IGNORECASE),
    re.compile(r"request\s+desktop\s+screenshot", re.IGNORECASE),
    re.compile(r"capture\s+my\s+mac\s+screen", re.IGNORECASE),
    re.compile(r"看一下.*(屏幕|桌面|screen)", re.IGNORECASE),
    re.compile(r"看看.*(屏幕|桌面|screen)", re.IGNORECASE),
    re.compile(r"take\s+a\s+screenshot", re.IGNORECASE),
    re.compile(
        r"screenshot\s*(on|of)\s*(my\s*)?(mac|macbook|desktop|laptop|screen)",
        re.IGNORECASE,
    ),
    re.compile(r"截图", re.IGNORECASE),
    re.compile(r"截屏", re.IGNORECASE),
)


# Computer Use / desktop action intent. The first match wins and
# routes to the ``computer.status`` stub. We intentionally do NOT
# match vague "open x" or "click y" — those are coding requests
# unless the user clearly anchors the target to their machine.
#
# Word-boundary note: ``\b`` does not match between two CJK
# characters in Python's ``re`` (only around ``\w``). For the
# CJK verb / noun tokens we use look-around patterns instead
# (``(?<![a-z])`` / ``(?![a-z])``) so "操作我的电脑" still
# matches as a verb-then-machine sequence. The patterns cover
# the three orderings we expect:
#   1. verb + machine: "打开 我的 Mac", "take a screenshot on my desktop"
#   2. machine + verb: "我的 Mac 打开 Xcode", "我的 MacBook 上的 Xcode"
#   3. machine + 在/上 + verb: "在 Mac 上打开", "我 MacBook 上的截图"
_ASCII_VERBS = r"(?:click|launch|control)"
_CJK_VERBS = r"(?:\u6253\u5f00|\u8fd0\u884c|\u542f\u52a8|\u70b9|\u64cd\u4f5c|\u622a\u56fe|\u770b\u4e00\u4e0b|\u770b\u770b|\u622a\u5c4f)"
_VERB_GROUP = rf"(?:{_CJK_VERBS}|{_ASCII_VERBS})"
_CJK_NOUN = r"(?:\u6211\u7684?\s*mac|\u6211\u7684?\s*macbook|\u672c\u673a|\u7535\u8111)"
_ASCII_NOUN = r"(?:macbook|mac|desktop|laptop)"
_NOUN_GROUP = rf"(?:{_CJK_NOUN}|{_ASCII_NOUN})"

_COMPUTER_USE_PATTERNS = (
    # verb + machine  (English verb can use \b; CJK verb cannot)
    re.compile(
        rf"(?:{_ASCII_VERBS})(?!\w).*?(?:{_CJK_NOUN})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_CJK_VERBS}.*?{_CJK_NOUN}",
        re.IGNORECASE,
    ),
    # CJK verb + ASCII noun (e.g. "操作 my desktop")
    re.compile(
        rf"{_CJK_VERBS}.*?{_ASCII_NOUN}",
        re.IGNORECASE,
    ),
    # machine + verb  (CJK side; \b not used between CJK)
    re.compile(
        rf"(?:{_CJK_NOUN}).*?{_CJK_VERBS}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:{_ASCII_NOUN})(?!\w).*?(?:{_ASCII_VERBS})(?!\w)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:{_ASCII_NOUN})(?!\w).*?{_CJK_VERBS}",
        re.IGNORECASE,
    ),
    # 在 (我的) mac/macbook/desktop (上)?  +  verb
    re.compile(
        rf"\u5728\s*(?:\u6211\u7684?\s*)?(?:{_ASCII_NOUN})(?:\s*\u4e0a)?(?!\w).*?"
        rf"(?:{_ASCII_VERBS})(?!\w)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\u5728\s*(?:\u6211\u7684?\s*)?(?:{_ASCII_NOUN})(?:\s*\u4e0a)?(?!\w).*?"
        rf"{_CJK_VERBS}",
        re.IGNORECASE,
    ),
    # (我的) mac/macbook + 上的 + verb  (e.g. "我的 MacBook 上的截图")
    re.compile(
        rf"(?:\u6211\u7684?\s*mac|\u6211\u7684?\s*macbook|macbook)\s*\u4e0a\u768e?"
        rf".*?{_CJK_VERBS}",
        re.IGNORECASE,
    ),
    # English: "computer use my mac"
    re.compile(
        r"computer\s*use\s*(my\s*)?(mac|macbook|desktop|laptop)?",
        re.IGNORECASE,
    ),
)

# P5.6: action directives that should RUN a Computer Use task (hands-free
# control via the local cua-driver), NOT just report status. These are
# checked BEFORE ``_COMPUTER_USE_PATTERNS`` so an explicit action verb wins
# over a status query. The verb set is deliberately narrower than
# ``_CJK_VERBS`` (no "看一下"/"看看") to avoid routing a "look at my mac"
# status question into a mutating task.
_CJK_TASK_VERBS = (
    r"(?:\u6253\u5f00|\u8fd0\u884c|\u542f\u52a8|\u70b9|\u64cd\u4f5c|"
    r"\u622a\u56fe|\u9f20\u6807|\u952e\u76d8|\u6309|\u6572)"
)
_TASK_APP = (
    r"(?:chrome|safari|\u6d4f\u89c8\u5668|xcode|terminal|\u7ec8\u7aef|"
    r"finder|\u8ba1\u7b97\u5668|app|application|\u5e94\u7528|\u8bbe\u7f6e)"
)
_COMPUTER_TASK_PATTERNS = (
    # 操作电脑 / 操作我的 Mac / 操作本机
    re.compile(rf"{_CJK_TASK_VERBS}.*?{_NOUN_GROUP}", re.IGNORECASE),
    # 帮我点 / 帮我打开 / 帮我运行 / 帮我截图  (action verb after 帮我)
    re.compile(rf"\u5e2e\u6211\s*(?:{_CJK_TASK_VERBS})", re.IGNORECASE),
    # 在(我的)电脑/mac(上) 打开/运行/点...
    re.compile(
        rf"\u5728\s*{_NOUN_GROUP}\s*(?:\u4e0a)?\s*(?:{_CJK_TASK_VERBS})",
        re.IGNORECASE,
    ),
    # 用(我的)电脑/mac 打开/运行...
    re.compile(
        rf"\u7528\s*{_NOUN_GROUP}\s*(?:{_CJK_TASK_VERBS})",
        re.IGNORECASE,
    ),
    # 打开/运行/启动 <app>  (e.g. 打开 Chrome, 打开浏览器)
    re.compile(
        rf"(?:{_CJK_TASK_VERBS})\s*(?:我(?:\u7684)?\s*)?{_TASK_APP}",
        re.IGNORECASE,
    ),
    # English: "control my mac", "operate my desktop", "use my mac"
    re.compile(
        r"(?:control|operate|use)\s+my\s+(mac|macbook|desktop|laptop)",
        re.IGNORECASE,
    ),
)
_WEB_SEARCH_PATTERNS = (
    re.compile(r"(搜索|搜|search|查|找).*(网上|web|网页|internet|谷歌|google)", re.IGNORECASE),
    re.compile(r"(网上|web|internet).*(搜索|搜|search|查|找)", re.IGNORECASE),
    re.compile(r"(搜一下|搜搜|search for|look up)", re.IGNORECASE),
    re.compile(r"^搜索\s+\S+", re.IGNORECASE),  # "搜索 <query>" at start
)
_RESEARCH_PATTERNS = (
    re.compile(r"(研究|调研|research|调查).*(一下|看看)", re.IGNORECASE),
    re.compile(r"(帮我|请).*(研究|调研|research|查查|调查)", re.IGNORECASE),
    re.compile(r"(深入了解|详细了解|learn about|find out about)", re.IGNORECASE),
    re.compile(r"^研究\s+\S+", re.IGNORECASE),  # "研究 <query>" at start
    re.compile(r"^research\s+(?:about|on)?\s*\S+", re.IGNORECASE),  # "research about <query>"
)


def route_intent(text: str) -> RouteResult:
    """Classify user text into deterministic / hybrid / llm path."""
    body = (text or "").strip()
    if not body:
        return RouteResult(kind="llm")

    # P5.0: execution-node patterns run BEFORE the generic
    # ``detect_ops_intent`` (``机器状态``/``主机状态`` would
    # otherwise match the load snapshot pattern). The desktop
    # layer is stub-only, so a phrase that names a target
    # machine (Mac / MacBook / 本机 / desktop / computer) is
    # almost always a nodes.status or computer.status query,
    # not a load query.
    for pat in _NODES_STATUS_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("nodes.status",))
    for pat in _SCREENSHOT_STATUS_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("desktop.observe.status",))
    for pat in _DESKTOP_UPLOAD_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("desktop.upload.request",))
    for pat in _OBSERVE_REQUEST_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("desktop.observe.request",))
    for pat in _COMPUTER_TASK_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("computer.task",), arg=body)
    for pat in _COMPUTER_USE_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("computer.status",))

    # Explicit ops/tool requests win over hybrid diagnosis patterns.
    # "帮我运行 htop 看看我的 vps" is a snapshot request, not analysis.
    ops_kind = detect_ops_intent(body)
    if ops_kind == "htop":
        return RouteResult(kind="deterministic", tools=("htop",))
    if ops_kind == "ps":
        return RouteResult(kind="deterministic", tools=("ps",))
    if ops_kind == "load":
        return RouteResult(kind="deterministic", tools=("load",))

    for pat in _RESTART_PATTERNS:
        if pat.search(body):
            arg = _extract_service_arg(body)
            if arg:
                return RouteResult(
                    kind="deterministic",
                    tools=("service_restart",),
                    arg=arg,
                )
            # Ambiguous restart (e.g. "重启 bot" / "重启服务"). Fall
            # through to llm so Codex can ask which one, instead of
            # silently defaulting to conveyor-telegram-bot. We embed a
            # short clarification directive alongside the original
            # text so the user message is preserved.
            return RouteResult(
                kind="llm",
                question=(
                    "用户发了重启请求但目标不明确，原文：\n"
                    f"{body}\n\n"
                    "请用简短中文反问「要重启 telegram、feishu 还是 maintain？」"
                    "不要调用任何工具，不要编造主机状态。"
                ),
                arg="",
            )
    for pat in _DISK_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("disk",))
    for pat in _LOGS_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("logs",), arg=_extract_log_arg(body))
    for pat in _SERVICE_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("service_status",))
    for pat in _GIT_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("git_status",))

    # Gmail / email intent (P3.3) — before hybrid so "邮件" doesn't hijack to ops
    for pat in _GMAIL_STATUS_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("gmail.status",))
    for pat in _GMAIL_RECENT_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("gmail.recent",))
    for pat in _GMAIL_SEARCH_PATTERNS:
        if pat.search(body):
            # Extract search query if present
            query = _extract_gmail_search_query(body)
            return RouteResult(kind="deterministic", tools=("gmail.search",), arg=query)
    for pat in _EMAIL_SEND_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="llm", question=(
                "用户想发邮件，但需要收件人、主题和正文。请用中文问用户："
                "「发给谁？主题和正文分别是什么？」"
            ))

    # Planner intent (P3.7) — hybrid: collect facts, then Codex analysis
    # Must come BEFORE calendar patterns since "今天日程安排" is a planner
    # request (schedule_review), not a simple calendar.today lookup.
    from personal_tools.planner import (
        DAILY_PRIORITY, DEV_PLAN, PROJECT_HEALTH, INBOX_TRIAGE, SCHEDULE_REVIEW,
    )
    for pat in _PLANNER_TODAY_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="hybrid", tool_items=DAILY_PRIORITY.tool_items, question="今日优先级分析")
    for pat in _PLANNER_DEV_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="hybrid", tool_items=DEV_PLAN.tool_items, question="开发计划")
    for pat in _PLANNER_HEALTH_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="hybrid", tool_items=PROJECT_HEALTH.tool_items, question="项目健康检查")
    for pat in _PLANNER_TRIAGE_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="hybrid", tool_items=INBOX_TRIAGE.tool_items, question="邮件分类整理")
    for pat in _PLANNER_SCHEDULE_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="hybrid", tool_items=SCHEDULE_REVIEW.tool_items, question="日程审查")

    # Project intent (P3.9) — conservative matching
    for pat in _PROJECT_LIST_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("projects.list",))
    for pat in _PROJECT_USE_PATTERNS:
        if pat.search(body):
            # Try to extract project ID
            m = re.search(r"(\d+)", body)
            arg = m.group(1) if m else ""
            return RouteResult(kind="deterministic", tools=("projects.use",), arg=arg)
    for pat in _PROJECT_NEXT_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("project.next",))
    for pat in _PROJECT_HEALTH_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("project.health",))
    for pat in _PROJECT_ROADMAP_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("project.roadmap",))
    for pat in _PROJECT_RELEASE_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("project.release_checklist",))

    # File Search / Knowledge Base intent (P4.2 / P4.2.1)
    # Routes to hybrid via kb.collect_facts which tries KB first, then file search.
    for pat in _FILE_SEARCH_PATTERNS:
        if pat.search(body):
            query = _extract_file_search_query(body)
            if query:
                return RouteResult(kind="deterministic", tools=("kb.collect_facts",), arg=query)
            return RouteResult(kind="llm", question=(
                "用户想搜索本地文件/文档，但没有提供搜索词。请用简短中文问用户："
                "「想搜什么关键词？」"
            ))

    # Web / Research intent (P4.1)
    for pat in _WEB_FETCH_PATTERNS:
        if pat.search(body):
            # Extract URL if present
            m = re.search(r'(https?://\S+)', body)
            arg = m.group(1) if m else ""
            if arg:
                return RouteResult(kind="deterministic", tools=("web.fetch",), arg=arg)
            return RouteResult(kind="llm", question=(
                "用户想获取网页内容，但没有提供 URL。请用简短中文问用户："
                "「请提供要获取的网页地址。」"
            ))
    for pat in _WEB_SEARCH_PATTERNS:
        if pat.search(body):
            # Extract search query
            query = _extract_web_search_query(body)
            if query:
                return RouteResult(kind="deterministic", tools=("web.search",), arg=query)
            return RouteResult(kind="llm", question=(
                "用户想搜索网页，但没有提供搜索词。请用简短中文问用户："
                "「想搜什么？」"
            ))
    for pat in _RESEARCH_PATTERNS:
        if pat.search(body):
            # Extract research question
            query = _extract_research_query(body)
            if query:
                return RouteResult(kind="deterministic", tools=("research.run",), arg=query)
            return RouteResult(kind="llm", question=(
                "用户想进行研究，但没有提供研究问题。请用简短中文问用户："
                "「想研究什么？」"
            ))

    # Calendar intent (P3.4)
    for pat in _CALENDAR_TODAY_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("calendar.today",))
    for pat in _CALENDAR_TOMORROW_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("calendar.tomorrow",))
    for pat in _CALENDAR_WEEK_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("calendar.week",))
    for pat in _CALENDAR_SEARCH_PATTERNS:
        if pat.search(body):
            query = _extract_gmail_search_query(body)  # reuse extraction
            return RouteResult(kind="deterministic", tools=("calendar.search",), arg=query)
    for pat in _CALENDAR_CREATE_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="llm", question=(
                "用户想创建日程，但需要标题、时间和描述。请用简短中文问用户："
                "「日程标题、时间（如明天 14:00-15:00）和可选描述分别是什么？」"
            ))

    # Contacts intent (P3.4)
    for pat in _CONTACTS_SEARCH_PATTERNS:
        if pat.search(body):
            query = _extract_gmail_search_query(body)  # reuse extraction
            return RouteResult(kind="deterministic", tools=("contacts.search",), arg=query)

    # Google OAuth intent (P3.4)
    for pat in _GOOGLE_AUTH_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("google.status",))

    # Daily Briefing intent (P3.5)
    for pat in _BRIEFING_TODAY_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("briefing.today",))
    for pat in _BRIEFING_TOMORROW_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("briefing.tomorrow",))
    for pat in _BRIEFING_STATUS_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("briefing.status",))
    for pat in _BRIEFING_ENABLE_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("briefing.enable",))
    for pat in _BRIEFING_DISABLE_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("briefing.disable",))

    # GitHub intent (P3.6)
    for pat in _GITHUB_STATUS_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("github.status",))
    # Issue detail patterns must come before issues list patterns
    for pat in _GITHUB_ISSUE_DETAIL_PATTERNS:
        if pat.search(body):
            m = re.search(r"#?\s*(\d+)", body)
            if m:
                return RouteResult(kind="deterministic", tools=("github.issue",), arg=m.group(1))
    for pat in _GITHUB_ISSUES_PATTERNS:
        if pat.search(body):
            query = _extract_github_query(body)
            return RouteResult(kind="deterministic", tools=("github.issues",), arg=query)
    # PR detail patterns must come before PRs list patterns
    for pat in _GITHUB_PR_DETAIL_PATTERNS:
        if pat.search(body):
            m = re.search(r"#?\s*(\d+)", body)
            if m:
                return RouteResult(kind="deterministic", tools=("github.pr",), arg=m.group(1))
    for pat in _GITHUB_PRS_PATTERNS:
        if pat.search(body):
            query = _extract_github_query(body)
            return RouteResult(kind="deterministic", tools=("github.prs",), arg=query)
    for pat in _GITHUB_CI_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="deterministic", tools=("github.ci",))
    for pat in _GITHUB_CREATE_ISSUE_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="llm", question=(
                "用户想创建 GitHub Issue，但需要标题和正文。请用简短中文问用户："
                "「Issue 标题和正文分别是什么？」"
            ))
    for pat in _GITHUB_COMMENT_PATTERNS:
        if pat.search(body):
            return RouteResult(kind="llm", question=(
                "用户想在 GitHub Issue/PR 上评论，但需要编号和评论内容。请用简短中文问用户："
                "「Issue/PR 编号是多少？评论内容是什么？」"
            ))

    tool_match = _match_explicit_tool(body)
    if tool_match is not None:
        return RouteResult(kind="deterministic", tools=(tool_match,))

    if not _CODING_GUARD.search(body):
        for pat in _DIAGNOSE_BOT_PATTERNS:
            if pat.search(body):
                return RouteResult(
                    kind="hybrid",
                    tool_items=DIAGNOSE_MODES["bot"],
                    question=body,
                )
        for pat in _DIAGNOSE_LOGS_PATTERNS:
            if pat.search(body):
                return RouteResult(
                    kind="hybrid",
                    tool_items=DIAGNOSE_MODES["logs"],
                    question=body,
                )
        for pat in _DIAGNOSE_SERVER_PATTERNS:
            if pat.search(body):
                return RouteResult(
                    kind="hybrid",
                    tool_items=DIAGNOSE_MODES["server"],
                    question=body,
                )

    # Hybrid: diagnosis / analysis questions (tools first, then Codex).
    for pat in _HYBRID_PATTERNS:
        if pat.search(body):
            return RouteResult(
                kind="hybrid",
                tools=_HYBRID_DEFAULT_TOOLS,
                question=body,
            )

    # NL router fallback (P4.3): handles additional domains not covered above.
    from handlers.nl_router import classify_nl, NLCategory
    nl = classify_nl(body)
    if nl.category == NLCategory.READ_DETERMINISTIC and nl.tool_name:
        return RouteResult(kind="deterministic", tools=(nl.tool_name,), arg=nl.arg)
    if nl.category == NLCategory.READ_HYBRID and nl.tool_name:
        return RouteResult(kind="deterministic", tools=(nl.tool_name,), arg=nl.arg)
    if nl.category == NLCategory.WRITE_SAFE_AUTO and nl.tool_name:
        return RouteResult(kind="deterministic", tools=(nl.tool_name,), arg=nl.arg)
    if nl.category == NLCategory.WRITE_CONFIRM_PREVIEW and nl.tool_name:
        # WRITE/DESTRUCTIVE — route to tool which will handle confirmation
        return RouteResult(kind="deterministic", tools=(nl.tool_name,), arg=nl.arg)
    if nl.category == NLCategory.CLARIFY and nl.question:
        return RouteResult(kind="llm", question=nl.question)

    return RouteResult(kind="llm")


def _extract_service_arg(body: str) -> str:
    """Return a concrete conveyor unit name from natural-language restart
    intent, or '' when the target is ambiguous.

    Accepts both full systemd unit names and the friendly /restart
    aliases (English + Chinese). Order matters: full units first
    (longer matches win over substrings like 'telegram' inside a URL).
    """
    lower = body.lower()
    full_units = (
        "conveyor-feishu-bot",
        "conveyor-telegram-bot",
        "conveyor-maintain.timer",
    )
    for name in full_units:
        if name in lower:
            return name
    # Aliases: longest keys first so 'tg' does not shadow 'telegram'.
    alias_keys = sorted(
        list(RESTART_ALIASES.keys()) + list(RESTART_ALIASES_ZH.keys()),
        key=len,
        reverse=True,
    )
    for alias in alias_keys:
        if not alias:
            continue
        # Use word-boundary matching for ASCII aliases so 'tg' does not
        # match inside 'btg-bot'; Chinese aliases match as substrings.
        if any("\u4e00" <= ch <= "\u9fff" for ch in alias):
            if alias in body:
                return RESTART_ALIASES.get(alias) or RESTART_ALIASES_ZH[alias]
        else:
            pattern = r"(?<!\w)" + re.escape(alias) + r"(?!\w)"
            if re.search(pattern, lower):
                return RESTART_ALIASES.get(alias) or RESTART_ALIASES_ZH[alias]
    return ""


def _extract_log_arg(body: str) -> str:
    m = re.search(r"\b(\d{1,3})\s*(行|lines?)\b", body, re.IGNORECASE)
    if m:
        return m.group(1)
    if "feishu" in body.lower():
        return "conveyor-feishu-bot"
    if "telegram" in body.lower():
        return "conveyor-telegram-bot"
    return ""


def _extract_gmail_search_query(body: str) -> str:
    """Extract search query from natural language Gmail search intent.

    Examples:
        "搜索邮件 关于发票" → "发票"
        "找一下邮件里的快递" → "快递"
        "search email for invoice" → "invoice"
    """
    # Chinese: "搜索/搜/找 邮件 <query>"
    m = re.search(r"(?:搜索|搜|找|search)\s*(?:邮件|email|gmail|mail)\s*(?:里的?|中的|for)?\s*(.+)", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # "邮件 搜索 <query>"
    m = re.search(r"(?:邮件|email)\s*(?:搜索|搜|找|search)\s*(.+)", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def _extract_github_query(body: str) -> str:
    """Extract state/query from natural language GitHub intent.

    Examples:
        "看看 open issue" → "open"
        "列出 closed pr" → "closed"
        "查看所有 issue" → "all"
    """
    lower = body.lower()
    # Check for explicit state
    for state in ("open", "closed", "all", "merged"):
        if state in lower:
            return state
    # Check for Chinese state words
    if "关闭" in body or "已关" in body:
        return "closed"
    if "所有" in body or "全部" in body:
        return "all"
    return "open"


def _extract_web_search_query(body: str) -> str:
    """Extract search query from natural language web search intent.

    Examples:
        "搜索 Python asyncio" → "Python asyncio"
        "搜一下最新的 AI 新闻" → "最新的 AI 新闻"
        "search web for machine learning" → "machine learning"
    """
    # Chinese: "搜索/搜/搜一下/搜搜 <query>"
    m = re.search(r"(?:搜索|搜一下?|搜搜|search)\s*(?:网上|web|网页|internet|谷歌|google)?\s*(?:for|关于|关于)?\s*(.+)", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # "网上搜索 <query>"
    m = re.search(r"(?:网上|web|internet)\s*(?:搜索|搜|search|查|找)\s*(.+)", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # English: "search for <query>" / "look up <query>"
    m = re.search(r"(?:search\s+for|look\s+up)\s+(.+)", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def _extract_research_query(body: str) -> str:
    """Extract research question from natural language research intent.

    Examples:
        "研究一下 Python 异步编程" → "Python 异步编程"
        "帮我调研一下 AI 编程助手" → "AI 编程助手"
        "research about LLM agents" → "LLM agents"
    """
    # Chinese: "研究/调研/调查 一下 <query>"
    m = re.search(r"(?:研究|调研|调查)\s*(?:一下|看看)?\s*(.+)", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # "帮我研究/调研 <query>"
    m = re.search(r"(?:帮我|请)\s*(?:研究|调研|调查|查查)\s*(?:一下|看看)?\s*(.+)", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # "深入了解/详细了解 <query>"
    m = re.search(r"(?:深入了解|详细了解)\s*(.+)", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # English: "research about <query>" / "learn about <query>"
    m = re.search(r"(?:research|learn|find\s+out)\s+(?:about|on)?\s*(.+)", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def _extract_file_search_query(body: str) -> str:
    """Extract search query from natural language file search intent.

    Examples:
        "找一下文档里关于 deploy 的说明" → "deploy"
        "README 里有没有 Gmail 配置步骤" → "Gmail 配置步骤"
        "项目文档怎么说 scheduler" → "scheduler"
        "根据本地文档总结安装流程" → "安装流程"
        "查一下我 notes 里关于 OAuth 的内容" → "OAuth"
    """
    # Chinese: "找一下/查找/搜索 文档里关于 <query>"
    m = re.search(r"(?:找一下|查找|搜索|搜)\s*(?:文档|文件|file|doc)?\s*(?:里|中|内)?\s*(?:关于|about)?\s*(.+)", body, re.IGNORECASE)
    if m:
        query = m.group(1).strip()
        # Remove trailing "的说明/内容/步骤" etc.
        query = re.sub(r"\s*(的|之|的说明|的内容|的步骤|的配置|contains)$", "", query, flags=re.IGNORECASE)
        if query:
            return query
    # "README/文档 里有没有 <query>"
    m = re.search(r"(?:README|文档|说明|doc)\s*(?:里|中)?\s*(?:有没有|怎么说|contains)\s*(.+)", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # "notes/笔记 里关于 <query>"
    m = re.search(r"(?:notes|笔记|备忘)\s*(?:里|中)?\s*(?:关于|about|有没有)\s*(.+)", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # "知识库里搜索 <query>"
    m = re.search(r"(?:知识库|kb|knowledge\s*base)\s*(?:里|中)?\s*(?:搜索|search|关于|about)\s*(.+)", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # "根据本地文档总结 <query>"
    m = re.search(r"(?:根据|based\s+on|from)\s*(?:本地|local|文档|doc)\s*(?:总结|summarize)\s*(.+)", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def _match_explicit_tool(body: str) -> str | None:
    m = re.match(r"^(?:tool|run\s+tool)\s+(\w+)\s*$", body.strip(), re.IGNORECASE)
    if m and m.group(1) in TOOL_REGISTRY:
        return m.group(1)
    return None


def list_tool_names() -> tuple[str, ...]:
    return tuple(TOOL_REGISTRY.keys())
