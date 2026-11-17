"""Intent detection and server-backed action cards for the owner web chat (对话).

Handles two natural-language intents cleanly and safely:
- ``create_project``: create a Conveyor-hosted empty Git repository + worktree
- ``start_timed_goal``: confirm and start a continuous timed supervisor goal

Deterministic regex patterns run first; only ambiguous intent-like messages
fall back to the configured chat-tier model for strict JSON classification.
Every interactive button points to a server-stored action ID whose payload is
authoritative on the server and disabled after a single execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any

from conveyor.agents import agent_supervisor as ags
from conveyor.agents import intent_decider
from conveyor.agents import supervisor_goals as goals_mod
from conveyor.agents import supervisor_merge as merge_mod
from conveyor.agents import supervisor_service as svc
from conveyor.agents import supervisor_validation
from conveyor.agents.goal_notify import goal_status_label
from conveyor.core.transcript_store import get_transcript_store

logger = logging.getLogger(__name__)

# The parallel timed-goal backend task adds duration_minutes (30..1440) and
# mode ('outcome' | 'continuous'). Unknown keys are accepted safely either way.
GOAL_TIMEBOX_SUPPORTED = True

PENDING_TTL_SECONDS = 10 * 60
MIN_DURATION_MINUTES = 30
MAX_DURATION_MINUTES = 1440
DEFAULT_TIMED_GOAL_OUTCOME = "按你的项目笔记找最值得做的改进"
# One 「理解指令暂时用不了」 notice per chat session, not one per message.
DECIDER_NOTICE_INTERVAL_SECONDS = 6 * 60 * 60

_DB_INIT_LOCK = threading.Lock()
_INITIALIZED_DBS: set[tuple[str, int, int]] = set()

_EXECUTOR_ZH_LABELS: dict[str, str] = {
    "pool": "执行者池",
    "pi": "Pi",
    "claude": "Claude Code",
    "command-code": "Command Code",
    "codex": "Codex CLI",
    "antigravity": "Antigravity",
    "chat": "对话模型",
}
_NAVIGATION_ONLY_ACTIONS = frozenset({
    "open_project",
    "open_goal_progress",
    "open_workers",
    "open_code",
})
MAX_GOAL_TITLE_CHARS = 24

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chat_intent_sessions (
    session_id TEXT PRIMARY KEY,
    recent_repo_id TEXT NOT NULL DEFAULT '',
    pending_kind TEXT NOT NULL DEFAULT '',
    pending_payload_json TEXT NOT NULL DEFAULT '{}',
    pending_expires_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chat_action_cards (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT '',
    deadline_at TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_action_buttons (
    id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL,
    sort_index INTEGER NOT NULL,
    label TEXT NOT NULL,
    action TEXT NOT NULL,
    style TEXT NOT NULL DEFAULT 'secondary',
    client_payload_json TEXT NOT NULL DEFAULT '{}',
    server_payload_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(card_id) REFERENCES chat_action_cards(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chat_action_buttons_card ON chat_action_buttons(card_id, sort_index);

CREATE TABLE IF NOT EXISTS chat_decider_notices (
    session_id TEXT PRIMARY KEY,
    last_kind TEXT NOT NULL DEFAULT '',
    last_notice_at REAL NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class Intent:
    kind: str  # "create_project" | "start_timed_goal"
    project_name: str | None = None
    duration_minutes: int | None = None
    goal_text: str = ""
    mentions_github: bool = False


# ---------------------------------------------------------------------------
# Deterministic NL detection & Chinese/English numeral helpers
# ---------------------------------------------------------------------------

_CN_DIGITS: dict[str, int] = {
    "一": 1,
    "二": 2,
    "两": 2,
    "俩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
    "十三": 13,
    "十四": 14,
    "十五": 15,
    "十六": 16,
    "十七": 17,
    "十八": 18,
    "十九": 19,
    "二十": 20,
    "二十一": 21,
    "二十二": 22,
    "二十三": 23,
    "二十四": 24,
}

# Sentences matching these patterns are questions, past reports, hypotheticals,
# or negations — never trigger an execution intent on them.
_NEGATIVE_INTERROGATIVE_RE = re.compile(
    r"(?:"
    r"怎么|如何|怎样|为啥|为什么|为何|多少钱|多少|几块钱|"
    r"是什么|什么是|啥是|什么叫|什么意思|有什么用|区别|教程|步骤|原理|"
    r"能不能解释|可以解释|介绍下|介绍一下|"
    r"\bwhat\s+is\b|\bwhat's\b|\bhow\s+to\b|\bhow\s+do\b|\bhow\s+much\b|"
    r"\bwhy\s+(?:is|are|do|does|did|would)\b"
    r")",
    re.IGNORECASE,
)

_QUESTION_ENDING_RE = re.compile(r"[?？]\s*$")
_PAST_TIME_RE = re.compile(
    r"(?:昨天|前天|上周|上个月|上星期|刚才|刚刚|曾经|以前|之前|上次|"
    r"\byesterday\b|\blast\s+(?:week|night|month)\b|\balready\s+(?:created|iterated|ran)\b)",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(
    r"(?:^|[\s，,。！!])(?:不要|别|不用|不必|不能|不可|取消|不想|没想|没打算|"
    r"如果|要是|假如|假设|听说|别人说|"
    r"\bdon't\b|\bdo\s+not\b|\bshouldn't\b|\bwouldn't\b)",
    re.IGNORECASE,
)
_REQUEST_LEAD_RE = re.compile(
    r"^\s*(?:请|麻烦|帮我|给我|替我|你来|你可以|你能|我想|我要|准备|打算|继续|开始|再|"
    r"please\b|can\s+you\b|could\s+you\b|let's\b)",
    re.IGNORECASE,
)
_PAST_VERB_LE_RE = re.compile(
    r"(?:^|我)\s*(?:已经)?\s*(?:创建|新建|建|迭代|跑|干|改)了(?!(?:个新|个|一下))",
)
_EN_PAST_VERB_RE = re.compile(r"\bi\s+(?:created|made|iterated|ran|worked)\b", re.IGNORECASE)

_GITHUB_RE = re.compile(r"\bgithub\b", re.IGNORECASE)

_STOP_NAME_TOKENS = frozenset({
    "github",
    "repo",
    "repository",
    "project",
    "please",
    "pls",
    "here",
    "now",
    "on",
    "in",
    "for",
    "with",
    "to",
    "and",
    "or",
    "the",
    "a",
    "an",
    "项目",
    "仓库",
    "代码库",
    "的",
    "吧",
    "呀",
    "啊",
    "呢",
    "吗",
    "了",
})

# Project creation regexes (ordered from most explicit to bare noun).
_CREATE_PREFIX = r"(?:(?:帮我|给我|替我|麻烦|请你|请|我要|我想|来)\s*)*"
_CREATE_VERB = r"(?:创建|新建|建立|建|开|初始化|搞|弄)"
_CREATE_ART = r"(?:一个|个|一个新的|个新的|新的)?"
_REPO_NOUN = r"(?:(?:GitHub|github)\s*)?(?:repo|repository|Git\s*仓库|git\s*仓库|仓库|项目|代码库)"
_NAME_INTRO = r"(?:叫(?:做)?|名为|名称(?:为|是)|名字(?:叫|为|是)|named|called|name\s*[:=]?)"
_NAME_TOKEN = r"[`\"'「『]?([^\s,，.。!！?？;；:：\"'`」』]+)[`\"'」』]?"

_ZH_CREATE_NAMED_BEFORE_NOUN_RE = re.compile(
    rf"{_CREATE_PREFIX}{_CREATE_VERB}\s*{_CREATE_ART}\s*(?:GitHub\s*|github\s*)?"
    rf"{_NAME_INTRO}\s*{_NAME_TOKEN}\s*的\s*{_REPO_NOUN}",
    re.IGNORECASE,
)
_ZH_CREATE_NAME_BETWEEN_RE = re.compile(
    rf"{_CREATE_PREFIX}{_CREATE_VERB}\s*([A-Za-z0-9][A-Za-z0-9._-]{{0,79}})\s*(?:这个)?\s*{_REPO_NOUN}",
    re.IGNORECASE,
)
_ZH_CREATE_NAMED_AFTER_NOUN_RE = re.compile(
    rf"{_CREATE_PREFIX}{_CREATE_VERB}\s*{_CREATE_ART}\s*{_REPO_NOUN}"
    rf"(?:\s*[，,:：]?\s*{_NAME_INTRO}\s*{_NAME_TOKEN}|\s+([A-Za-z0-9][A-Za-z0-9._-]{{0,79}}))?",
    re.IGNORECASE,
)
_EN_CREATE_RE = re.compile(
    rf"(?:^|\b)(?:create|make|init(?:ialize)?|new)\s+(?:a\s+|an\s+|new\s+)?(?:github\s+)?(?:repo|repository|project)"
    rf"(?:\s+(?:named|called)\s+{_NAME_TOKEN}|\s+([A-Za-z0-9][A-Za-z0-9._-]{{0,79}}))?",
    re.IGNORECASE,
)

# Timed goal regexes.
_HOUR_CN_TOKENS = "|".join(sorted(_CN_DIGITS.keys(), key=len, reverse=True))
_DURATION_PATTERN = (
    rf"(?:"
    rf"(?P<half>半)\s*个?\s*小时"
    rf"|(?P<num_half>{_HOUR_CN_TOKENS}|\d+)\s*个?\s*半\s*小时"
    rf"|(?P<num_hours_half>{_HOUR_CN_TOKENS}|\d+)\s*个?\s*小时半"
    rf"|(?P<hours_num>\d+(?:\.\d+)?)\s*(?:个?\s*小时|hours?|hrs?|h\b)"
    rf"|(?P<hours_cn>{_HOUR_CN_TOKENS})\s*个?\s*小时"
    rf"|(?P<mins>\d+)\s*(?:分钟|mins?|minutes?)"
    rf")"
)
_TIMED_ACTION_ZH = (
    r"(?:持续(?:改进|优化|开发|改|跑|干|做)|连续(?:迭代|跑|干|改)|一直(?:迭代|跑|干|改)|"
    r"自动(?:迭代|跑|改)|迭代|跑|干|改)"
)
_TIMED_ACTION_EN = r"(?:\biterate\b|\bkeep\s+(?:working|improving)\b|\bwork\b|\brun\b|\bimprove\b)"

_TIMED_VERB_DURATION_RE = re.compile(
    rf"(?:{_TIMED_ACTION_ZH}|{_TIMED_ACTION_EN})"
    rf"(?:\s*(?:个|一下|上|了))?"
    rf"(?:\s*(?:on\s+[A-Za-z0-9][A-Za-z0-9._-]{{0,79}}))?"
    rf"(?:\s*(?:for|大约|大概|约))?\s*{_DURATION_PATTERN}",
    re.IGNORECASE,
)
_DURATION_VERB_RE = re.compile(
    rf"{_DURATION_PATTERN}\s*(?:地|的|内|里)?\s*(?:{_TIMED_ACTION_ZH}|{_TIMED_ACTION_EN})",
    re.IGNORECASE,
)
_PROJECT_IN_TIMED_RE = re.compile(
    r"(?:(?:在|对|给|让|把|帮|于)\s*([A-Za-z0-9][A-Za-z0-9._-]{0,79})(?:\s*(?:项目|仓库|代码库))?(?:\s*[上里内]))"
    r"|(?:(?:在|对|给|让|把|帮)\s*([A-Za-z0-9][A-Za-z0-9._-]{0,79})\s+(?:项目|仓库|代码库))"
    r"|(?:(?:在|对|给|让)\s*(?:项目|仓库|repo)\s*([A-Za-z0-9][A-Za-z0-9._-]{0,79}))"
    r"|(?:\b(?:on|for)\s+(?:the\s+)?(?:(?:project|repo)\s+)?([A-Za-z0-9][A-Za-z0-9._-]{1,79})\b)",
    re.IGNORECASE,
)

# Hints that make a message worth checking with the optional chat-tier LLM fallback.
_AMBIGUOUS_INTENT_HINT_RE = re.compile(
    r"(?:repo|repository|仓库|项目|代码库|github|迭代|持续改进|持续优化|小时|半小时|hours?)",
    re.IGNORECASE,
)


def _is_clear_negative(text: str) -> bool:
    if _NEGATIVE_INTERROGATIVE_RE.search(text):
        return True
    if _QUESTION_ENDING_RE.search(text):
        return True
    if _PAST_TIME_RE.search(text):
        return True
    if _NEGATION_RE.search(text):
        return True
    if _EN_PAST_VERB_RE.search(text):
        return True
    if _PAST_VERB_LE_RE.search(text) and not _REQUEST_LEAD_RE.search(text):
        return True
    if re.search(r"[吗么]\s*[。！!]?$", text) and not _REQUEST_LEAD_RE.search(text):
        return True
    return False


def _clean_project_candidate(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip().strip("`\"'「『」』.,!?;:。，！？；：")
    if not cleaned:
        return None
    if cleaned.lower() in _STOP_NAME_TOKENS:
        return None
    return cleaned


def _parse_duration_match(match: re.Match[str]) -> int | None:
    minutes: int | None = None
    if match.group("half"):
        minutes = 30
    elif match.group("num_half") or match.group("num_hours_half"):
        token = match.group("num_half") or match.group("num_hours_half")
        base = int(token) if token.isdigit() else _CN_DIGITS.get(token)
        if base is not None:
            minutes = base * 60 + 30
    elif match.group("hours_num"):
        try:
            hours = float(match.group("hours_num"))
        except ValueError:
            return None
        minutes = int(round(hours * 60))
    elif match.group("hours_cn"):
        val = _CN_DIGITS.get(match.group("hours_cn"))
        if val is not None:
            minutes = val * 60
    elif match.group("mins"):
        try:
            minutes = int(match.group("mins"))
        except ValueError:
            return None
    if minutes is None or minutes <= 0:
        return None
    return max(MIN_DURATION_MINUTES, min(MAX_DURATION_MINUTES, minutes))


_FILLER_AND_PUNCT_EDGE_CHARS = r"吧呢啊呀嘛哈\s,，.。:：!！;；、~～"
_LEADING_CONNECTOR_RE = re.compile(
    r"^(?:那么|那|然后|并且|并|就|来帮我|去|(?:to|and)(?=\s|$))\s*",
    re.IGNORECASE,
)
_CLAUSE_SPLIT_RE = re.compile(r"[\r\n]+|[，,;；。！!？?：:、]|\.(?=\s|$)")


def _strip_goal_edges(fragment: str, *, strip_connectors: bool = True) -> str:
    text = fragment
    while True:
        prev = text
        text = re.sub(rf"^[{_FILLER_AND_PUNCT_EDGE_CHARS}]+", "", text)
        text = re.sub(rf"[{_FILLER_AND_PUNCT_EDGE_CHARS}]+$", "", text)
        if strip_connectors:
            text = _LEADING_CONNECTOR_RE.sub("", text)
        if text == prev:
            break
    return text.strip()


def _is_word_char(ch: str) -> bool:
    return ch in "_-" or (ch.isalnum() and ord(ch) < 0x2E80)


def _short_goal_title(goal_text: str, max_chars: int = MAX_GOAL_TITLE_CHARS) -> str:
    """Derive a concise title from the first clause of goal_text, <= max_chars."""
    collapsed = " ".join(str(goal_text or "").split())
    if not collapsed:
        return ""
    first_clause = ""
    for part in _CLAUSE_SPLIT_RE.split(collapsed):
        candidate = part.strip(" \t\r\n,，.。:：!！;；、~～")
        if candidate:
            first_clause = candidate
            break
    if not first_clause:
        first_clause = collapsed
    if len(first_clause) <= max_chars:
        return first_clause
    limit = max(1, max_chars - 1)
    cut = limit
    if cut < len(first_clause) and _is_word_char(first_clause[cut - 1]) and _is_word_char(first_clause[cut]):
        while cut > 0 and _is_word_char(first_clause[cut - 1]):
            cut -= 1
    prefix = first_clause[:cut].rstrip(" \t\r\n,，.。:：!！;；、~～-_")
    if not prefix:
        prefix = first_clause[:limit].rstrip()
    return prefix + "…"


def _extract_goal_text(text: str, *, verb_span: tuple[int, int]) -> str:
    """Take the remainder of the sentence after the trigger/duration phrase as goal text."""
    after = text[verb_span[1]:]
    cleaned = _strip_goal_edges(after)
    if cleaned:
        return cleaned
    # If the clause before the trigger holds the goal (e.g. "把首页做好，跑 2 小时").
    before = text[:verb_span[0]]
    before = _PROJECT_IN_TIMED_RE.sub("", before)
    before = _strip_goal_edges(before)
    before = re.sub(r"^\s*(?:帮我|请|你来|给我|麻烦|我想|我要|在|对)+\s*", "", before)
    return _strip_goal_edges(before)


def detect_intent(text: str) -> Intent | None:
    """Deterministically detect ``create_project`` or ``start_timed_goal`` from user text."""
    raw = (text or "").strip()
    if not raw or raw.startswith("/"):
        return None
    if _is_clear_negative(raw):
        return None

    mentions_github = bool(_GITHUB_RE.search(raw))

    # 1. create_project checks (prioritized before generic verbs).
    for pattern in (
        _ZH_CREATE_NAMED_BEFORE_NOUN_RE,
        _ZH_CREATE_NAME_BETWEEN_RE,
        _ZH_CREATE_NAMED_AFTER_NOUN_RE,
        _EN_CREATE_RE,
    ):
        m = pattern.search(raw)
        if m:
            candidate: str | None = None
            for grp in m.groups():
                candidate = _clean_project_candidate(grp)
                if candidate:
                    break
            return Intent(
                kind="create_project",
                project_name=candidate,
                duration_minutes=None,
                goal_text="",
                mentions_github=mentions_github,
            )

    # 2. start_timed_goal checks (requires BOTH action verb and duration).
    timed_match = _TIMED_VERB_DURATION_RE.search(raw) or _DURATION_VERB_RE.search(raw)
    if timed_match:
        duration_minutes = _parse_duration_match(timed_match)
        if duration_minutes is not None:
            proj_name: str | None = None
            for proj_match in _PROJECT_IN_TIMED_RE.finditer(raw):
                for grp in proj_match.groups():
                    cand = _clean_project_candidate(grp)
                    if cand and not re.fullmatch(r"\d+(?:\.\d+)?(?:h|hrs?|hours?|m|mins?|minutes?)", cand, re.I):
                        proj_name = cand
                        break
                if proj_name:
                    break
            goal_text = _extract_goal_text(raw, verb_span=timed_match.span())
            return Intent(
                kind="start_timed_goal",
                project_name=proj_name,
                duration_minutes=duration_minutes,
                goal_text=goal_text,
                mentions_github=mentions_github,
            )

    return None


def _parse_model_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").lstrip()
        if raw.startswith("json"):
            raw = raw[4:].lstrip()
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("expected JSON object")
    return parsed


async def classify_intent(
    text: str,
    settings: Any,
    *,
    model: Any = None,
) -> Intent | None:
    """Detect intent deterministically, falling back to chat-tier LLM only when ambiguous."""
    direct = detect_intent(text)
    if direct is not None:
        return direct

    raw = (text or "").strip()
    if not raw or raw.startswith("/") or _is_clear_negative(raw):
        return None
    if not _AMBIGUOUS_INTENT_HINT_RE.search(raw):
        return None

    if model is None:
        from dataclasses import replace
        from runner.chat_client import complete_chat, config_from_settings

        cfg = config_from_settings(settings)
        if cfg is None:
            return None

        async def model(messages: list[dict[str, str]]) -> dict[str, Any]:
            return await complete_chat(
                replace(cfg, max_tokens=min(cfg.max_tokens, 200), timeout=min(cfg.timeout, 10)),
                messages,
                tools=None,
            )

    prompt = [
        {
            "role": "system",
            "content": (
                "Classify the owner's web chat message into ONE JSON object and output JSON ONLY:\n"
                '{"intent": "create_project" | "start_timed_goal" | null, '
                '"project_name": string | null, '
                '"duration_minutes": integer | null, '
                '"goal_text": string}\n'
                "Rules:\n"
                "- Return intent=null for questions, past tense, negations, or unrelated chat.\n"
                "- For create_project: extract project_name if explicitly named, else null.\n"
                "- For start_timed_goal: duration_minutes must be an integer between 30 and 1440 (default 60 if hours implied without number? No, return null if no time duration is requested); goal_text is the goal portion.\n"
                "- Output strict JSON only, no markdown commentary."
            ),
        },
        {"role": "user", "content": raw},
    ]

    try:
        answer = await model(prompt)
        content = answer.get("content") if isinstance(answer, dict) else None
        if not isinstance(content, str):
            return None
        data = _parse_model_json(content)
    except Exception:
        logger.debug("Chat intent model classification failed or returned invalid JSON", exc_info=True)
        return None

    kind = data.get("intent")
    if kind not in ("create_project", "start_timed_goal"):
        return None

    proj = data.get("project_name")
    project_name = _clean_project_candidate(proj) if isinstance(proj, str) else None
    mentions_github = bool(_GITHUB_RE.search(raw))

    if kind == "create_project":
        return Intent(
            kind="create_project",
            project_name=project_name,
            duration_minutes=None,
            goal_text="",
            mentions_github=mentions_github,
        )

    dur = data.get("duration_minutes")
    if type(dur) is not int or dur < MIN_DURATION_MINUTES or dur > MAX_DURATION_MINUTES:
        return None
    goal_text = data.get("goal_text") if isinstance(data.get("goal_text"), str) else ""
    return Intent(
        kind="start_timed_goal",
        project_name=project_name,
        duration_minutes=dur,
        goal_text=goal_text.strip(),
        mentions_github=mentions_github,
    )


# ---------------------------------------------------------------------------
# SQLite storage: per-session pending state & server-authoritative action cards
# ---------------------------------------------------------------------------

def _db_path(settings: Any) -> Path:
    return ags._root(settings) / "state" / "chat_intents.sqlite3"


def _connect(settings: Any) -> sqlite3.Connection:
    path = _db_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    st_before = path.stat() if path.exists() else None
    key_before = (key, st_before.st_dev, st_before.st_ino) if st_before is not None else None
    with _DB_INIT_LOCK:
        need_init = key_before is None or key_before not in _INITIALIZED_DBS
    conn = sqlite3.connect(str(path), timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    if need_init:
        with _DB_INIT_LOCK:
            st = path.stat()
            cur_key = (key, st.st_dev, st.st_ino)
            if cur_key not in _INITIALIZED_DBS:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(_SCHEMA_SQL)
                cols = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(chat_action_cards)").fetchall()
                }
                if "deadline_at" not in cols:
                    conn.execute("ALTER TABLE chat_action_cards ADD COLUMN deadline_at TEXT NOT NULL DEFAULT ''")
                conn.commit()
                _INITIALIZED_DBS.add(cur_key)
    return conn


def _get_session_state(settings: Any, session_id: str) -> dict[str, Any]:
    if not session_id:
        return {"recent_repo_id": "", "pending_kind": "", "pending_payload": {}, "pending_expires_at": 0.0}
    conn = _connect(settings)
    try:
        row = conn.execute(
            "SELECT recent_repo_id, pending_kind, pending_payload_json, pending_expires_at "
            "FROM chat_intent_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"recent_repo_id": "", "pending_kind": "", "pending_payload": {}, "pending_expires_at": 0.0}
    try:
        payload = json.loads(row["pending_payload_json"] or "{}")
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    return {
        "recent_repo_id": str(row["recent_repo_id"] or ""),
        "pending_kind": str(row["pending_kind"] or ""),
        "pending_payload": payload,
        "pending_expires_at": float(row["pending_expires_at"] or 0.0),
    }


def _set_recent_repo(settings: Any, session_id: str, repo_id: str) -> None:
    if not session_id or not repo_id:
        return
    now = time.time()
    conn = _connect(settings)
    try:
        conn.execute(
            """INSERT INTO chat_intent_sessions (session_id, recent_repo_id, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   recent_repo_id = excluded.recent_repo_id,
                   updated_at = excluded.updated_at""",
            (session_id, repo_id, now),
        )
        conn.commit()
    finally:
        conn.close()


def _set_pending_state(
    settings: Any,
    session_id: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    ttl_seconds: int = PENDING_TTL_SECONDS,
) -> None:
    if not session_id:
        return
    now = time.time()
    expires = now + ttl_seconds if kind else 0.0
    payload_json = json.dumps(payload or {}, ensure_ascii=False)
    conn = _connect(settings)
    try:
        conn.execute(
            """INSERT INTO chat_intent_sessions
                   (session_id, pending_kind, pending_payload_json, pending_expires_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   pending_kind = excluded.pending_kind,
                   pending_payload_json = excluded.pending_payload_json,
                   pending_expires_at = excluded.pending_expires_at,
                   updated_at = excluded.updated_at""",
            (session_id, kind, payload_json, expires, now),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_pending_state(settings: Any, session_id: str) -> None:
    _set_pending_state(settings, session_id, "", {}, ttl_seconds=0)


def _has_active_pending(settings: Any, session_id: str, *, now: float | None = None) -> dict[str, Any] | None:
    state = _get_session_state(settings, session_id)
    if not state["pending_kind"]:
        return None
    ts = time.time() if now is None else now
    if state["pending_expires_at"] and ts > state["pending_expires_at"]:
        _clear_pending_state(settings, session_id)
        return None
    return state


def create_action_card(
    settings: Any,
    *,
    session_id: str,
    title: str,
    text: str,
    buttons: list[dict[str, Any]],
    deadline_at: str | None = None,
) -> dict[str, Any]:
    """Persist an interactive action card and return its client-safe representation."""
    card_id = f"card_{os.urandom(8).hex()}"
    now = time.time()
    clean_deadline = str(deadline_at or "").strip()
    client_buttons: list[dict[str, Any]] = []

    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO chat_action_cards (id, session_id, title, text, used, outcome, deadline_at, created_at)
               VALUES (?, ?, ?, ?, 0, '', ?, ?)""",
            (card_id, session_id, title, text, clean_deadline, now),
        )
        for idx, spec in enumerate(buttons):
            btn_id = f"act_{os.urandom(8).hex()}"
            label = str(spec["label"])
            action = str(spec["action"])
            style = str(spec.get("style") or "secondary")
            client_payload = dict(spec.get("client_payload") or {})
            server_payload = dict(spec.get("server_payload") or {})
            conn.execute(
                """INSERT INTO chat_action_buttons
                       (id, card_id, sort_index, label, action, style, client_payload_json, server_payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    btn_id,
                    card_id,
                    idx,
                    label,
                    action,
                    style,
                    json.dumps(client_payload, ensure_ascii=False),
                    json.dumps(server_payload, ensure_ascii=False),
                ),
            )
            client_buttons.append({
                "id": btn_id,
                "label": label,
                "action": action,
                "style": style,
                "payload": client_payload,
            })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    card: dict[str, Any] = {
        "id": card_id,
        "title": title,
        "text": text,
        "buttons": client_buttons,
        "used": False,
        "outcome": "",
    }
    if clean_deadline:
        card["deadline_at"] = clean_deadline
    return card


def _row_to_card(conn: sqlite3.Connection, card_id: str) -> dict[str, Any] | None:
    card_row = conn.execute(
        "SELECT id, title, text, used, outcome, deadline_at FROM chat_action_cards WHERE id = ?",
        (card_id,),
    ).fetchone()
    if card_row is None:
        return None
    btn_rows = conn.execute(
        "SELECT id, label, action, style, client_payload_json FROM chat_action_buttons "
        "WHERE card_id = ? ORDER BY sort_index ASC",
        (card_id,),
    ).fetchall()
    buttons: list[dict[str, Any]] = []
    for b in btn_rows:
        try:
            payload = json.loads(b["client_payload_json"] or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        buttons.append({
            "id": str(b["id"]),
            "label": str(b["label"]),
            "action": str(b["action"]),
            "style": str(b["style"] or "secondary"),
            "payload": payload,
        })
    card: dict[str, Any] = {
        "id": str(card_row["id"]),
        "title": str(card_row["title"]),
        "text": str(card_row["text"]),
        "buttons": buttons,
        "used": bool(card_row["used"]),
        "outcome": str(card_row["outcome"] or ""),
    }
    deadline_val = str(card_row["deadline_at"] or "").strip()
    if deadline_val:
        card["deadline_at"] = deadline_val
    return card


def get_action_card(settings: Any, card_id: str) -> dict[str, Any] | None:
    if not settings or not card_id:
        return None
    conn = _connect(settings)
    try:
        return _row_to_card(conn, card_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers for formatting, GitHub check, projects, worktrees, executors
# ---------------------------------------------------------------------------

def _has_github_configured(settings: Any) -> bool:
    if bool(getattr(settings, "github_token", "")):
        return True
    try:
        from personal_tools.github_tools import _check_config

        return _check_config(settings) is None
    except Exception:
        return False


def _format_duration_zh(minutes: int) -> str:
    if minutes == 30:
        return "半小时"
    if minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    if minutes % 60 == 30 and minutes > 60:
        return f"{minutes // 60} 个半小时"
    return f"{minutes} 分钟"


def _executor_label(executor: str) -> str:
    return _EXECUTOR_ZH_LABELS.get(executor, executor or "未配置")


def _format_executors_summary(role_executors: list[str]) -> str:
    if role_executors and all(ex == "pool" for ex in role_executors):
        return "由执行者池自动安排"
    distinct_labels: list[str] = []
    for ex in role_executors:
        label = _executor_label(ex)
        if label not in distinct_labels:
            distinct_labels.append(label)
    if not distinct_labels:
        return "未配置执行者"
    if len(distinct_labels) == 1:
        return f"用 {distinct_labels[0]}"
    return f"用 {'、'.join(distinct_labels[:-1])} 和 {distinct_labels[-1]}"


def _summarize_roles_and_overrides(settings: Any) -> tuple[str, dict[str, dict[str, Any]], str | None]:
    """Return ``(chinese_roles_summary, role_overrides, refusal_error_or_none)``."""
    roles_cfg = ags.load_roles(settings)
    try:
        cap = svc.get_capabilities(settings)
        roles_cap = cap.get("roles") or {}
    except svc.SupervisorServiceError:
        roles_cap = {}

    role_executors: list[str] = []
    overrides: dict[str, dict[str, Any]] = {}
    any_available = False
    first_reason = ""
    ui_executors = {name for name, _label in svc._UI_EXPOSED_EXECUTORS}

    for role in ("analyst", "implementer", "reviewer"):
        rc = roles_cfg[role]
        info = roles_cap.get(role) or {}
        executor = str(info.get("executor") or rc.executor or "")
        model = info.get("model") if "model" in info else (rc.model or None)
        avail = info.get("available")
        if avail is None:
            ok, reason = ags.get_executor(settings, executor).available()
            avail = bool(ok)
        else:
            reason = str(info.get("reason") or "")
        if avail:
            any_available = True
        elif not first_reason and reason:
            first_reason = reason
        role_executors.append(executor)
        if executor in ui_executors:
            entry: dict[str, Any] = {"executor": executor}
            if model:
                entry["model"] = model
            overrides[role] = entry

    summary = _format_executors_summary(role_executors)
    refusal = None
    if not any_available:
        detail = f"（{first_reason}）" if first_reason else ""
        refusal = f"目前没有可用的执行者{detail}，请先在「执行者」页面配置好后再试。"
    return summary, overrides, refusal


def _first_managed_worktree(repo: dict[str, Any]) -> dict[str, Any] | None:
    for wt in repo.get("worktrees") or []:
        if wt.get("managed") and not wt.get("is_main"):
            return wt
    return None


def _ensure_ready_worktree(settings: Any, repo: dict[str, Any]) -> dict[str, Any]:
    existing = _first_managed_worktree(repo)
    if existing is not None:
        return existing
    return svc.create_managed_worktree(settings, str(repo["id"]))


def _safe_list_repos(settings: Any) -> list[dict[str, Any]]:
    try:
        return svc.list_repositories(settings)
    except svc.SupervisorServiceError:
        return []


def _find_repo_by_id(repos: list[dict[str, Any]], repo_id: str) -> dict[str, Any] | None:
    for r in repos:
        if r.get("id") == repo_id:
            return r
    return None


def _find_repo_by_name(repos: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    target = name.strip().lower()
    if not target:
        return None
    for r in repos:
        if str(r.get("name") or "").strip().lower() == target:
            return r
    return None


def _match_repo_mentioned_in_text(repos: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
    # Prefer longest name match first so "todo-app-v2" beats "todo-app".
    for repo in sorted(repos, key=lambda r: len(str(r.get("name") or "")), reverse=True):
        rname = str(repo.get("name") or "").strip()
        if not rname:
            continue
        if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(rname)}(?![A-Za-z0-9_-])", text, re.IGNORECASE):
            return repo
    return None


def _build_created_project_card(
    settings: Any,
    session_id: str,
    repo: dict[str, Any],
    worktree: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    repo_name = str(repo.get("name") or "")
    repo_id = str(repo.get("id") or "")
    wt_id = str((worktree or _first_managed_worktree(repo) or {}).get("id") or "")
    text = f"已创建项目 {repo_name}（空的 Git 仓库），可以开始了。"
    card = create_action_card(
        settings,
        session_id=session_id,
        title="项目已创建",
        text=text,
        buttons=[
            {
                "label": "打开项目",
                "action": "open_project",
                "style": "primary",
                "client_payload": {"view": "code", "repo_id": repo_id, "worktree_id": wt_id},
                "server_payload": {"repo_id": repo_id, "worktree_id": wt_id},
            },
            {
                "label": "让它开始干活",
                "action": "prefill_timed_goal",
                "style": "secondary",
                "client_payload": {"repo_id": repo_id, "repo_name": repo_name},
                "server_payload": {"repo_id": repo_id, "repo_name": repo_name},
            },
        ],
    )
    return text, card


def _build_existing_project_card(
    settings: Any,
    session_id: str,
    repo: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    repo_name = str(repo.get("name") or "")
    repo_id = str(repo.get("id") or "")
    wt = _first_managed_worktree(repo)
    wt_id = str((wt or {}).get("id") or "")
    text = f"已经有叫「{repo_name}」的项目了，没有改动原来的文件，你可以直接打开它。"
    card = create_action_card(
        settings,
        session_id=session_id,
        title="项目已存在",
        text=text,
        buttons=[
            {
                "label": "打开项目",
                "action": "open_project",
                "style": "primary",
                "client_payload": {"view": "code", "repo_id": repo_id, "worktree_id": wt_id},
                "server_payload": {"repo_id": repo_id, "worktree_id": wt_id},
            },
        ],
    )
    return text, card


def _repo_has_validation(settings: Any, repo_id: str) -> bool:
    if not repo_id:
        return False
    try:
        info = supervisor_validation.get_repo_validation(settings, repo_id)
    except svc.SupervisorServiceError:
        return False
    return str(info.get("source") or "none") != "none"


def _build_timed_goal_confirmation_card(
    settings: Any,
    session_id: str,
    repo: dict[str, Any],
    duration_minutes: int,
    goal_text: str,
) -> tuple[str, dict[str, Any]]:
    repo_id = str(repo.get("id") or "")
    repo_name = str(repo.get("name") or "")
    wt = _first_managed_worktree(repo)
    wt_id = str((wt or {}).get("id") or "")
    duration_label = _format_duration_zh(duration_minutes)
    goal_display = goal_text.strip() or DEFAULT_TIMED_GOAL_OUTCOME

    roles_summary, _, _ = _summarize_roles_and_overrides(settings)
    has_validation = _repo_has_validation(settings, repo_id)
    text = (
        f"准备在 {repo_name} 上持续改进 {duration_label}：{goal_display}。"
        f"{roles_summary}。时间到或者模型额度用完会自动停。"
    )
    if not has_validation:
        text += (
            "这个项目还没有自动检查：会让执行者先写一个 scripts/check.sh 跑真正的测试，"
            "之后每一轮都用它来验证。"
        )
    buttons: list[dict[str, Any]] = [
        {
            "label": "开始",
            "action": "confirm_start_timed_goal",
            "style": "primary",
            "client_payload": {},
            "server_payload": {
                "repo_id": repo_id,
                "duration_minutes": int(duration_minutes),
                "goal_text": goal_display,
            },
        },
    ]
    if not has_validation:
        buttons.append({
            "label": "每轮等我确认",
            "action": "confirm_start_timed_goal_manual",
            "style": "secondary",
            "client_payload": {},
            "server_payload": {
                "repo_id": repo_id,
                "duration_minutes": int(duration_minutes),
                "goal_text": goal_display,
            },
        })
    buttons.extend([
        {
            "label": "改一改",
            "action": "edit_timed_goal",
            "style": "secondary",
            "client_payload": {
                "view": "code",
                "tab": "goals",
                "repo_id": repo_id,
                "worktree_id": wt_id,
                "goal_outcome": goal_display,
                "duration_minutes": int(duration_minutes),
            },
            "server_payload": {},
        },
        {
            "label": "取消",
            "action": "cancel_action",
            "style": "secondary",
            "client_payload": {},
            "server_payload": {"outcome": "已取消这次定时任务"},
        },
    ])
    card = create_action_card(
        settings,
        session_id=session_id,
        title="确认开始持续改进",
        text=text,
        buttons=buttons,
    )
    return text, card


def _build_pick_project_card(
    settings: Any,
    session_id: str,
    repos: list[dict[str, Any]],
    duration_minutes: int,
    goal_text: str,
    *,
    prefix_text: str = "",
) -> tuple[str, dict[str, Any]]:
    prompt_text = prefix_text or "在哪个项目上干？"
    buttons: list[dict[str, Any]] = []
    for repo in repos[:6]:
        buttons.append({
            "label": str(repo["name"]),
            "action": "pick_project",
            "style": "secondary",
            "client_payload": {},
            "server_payload": {
                "repo_id": str(repo["id"]),
                "duration_minutes": int(duration_minutes),
                "goal_text": goal_text,
            },
        })
    card = create_action_card(
        settings,
        session_id=session_id,
        title="选择项目",
        text=prompt_text,
        buttons=buttons,
    )
    return prompt_text, card


# ---------------------------------------------------------------------------
# Delivery helpers (SSE + TranscriptStore)
# ---------------------------------------------------------------------------

def _emit_plain_reply(port: Any, msg: Any, text: str) -> None:
    port.emit("message", {"text": text})
    if hasattr(port, "_persist_turn"):
        port._persist_turn(msg, text, kind="chat")


def _emit_decider_notice(port: Any, msg: Any, text: str) -> None:
    """Post one system-style notice; it never becomes this turn's final answer."""
    port.emit("message", {"text": text, "kind": "notice"})
    if not hasattr(port, "_persist_turn"):
        return
    try:
        port._persist_turn(msg, text, kind="notice", final=False)
    except TypeError:
        # Ports that only know the plain signature still keep the notice in history.
        port._persist_turn(msg, text, kind="notice")


def _should_emit_decider_notice(settings: Any, session_id: str) -> bool:
    """True at most once per 6 hours per chat session."""
    if not session_id:
        return False
    conn = _connect(settings)
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS chat_decider_notices (
                   session_id TEXT PRIMARY KEY,
                   last_kind TEXT NOT NULL DEFAULT '',
                   last_notice_at REAL NOT NULL DEFAULT 0
               )"""
        )
        row = conn.execute(
            "SELECT last_notice_at FROM chat_decider_notices WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return True
        return (time.time() - float(row["last_notice_at"] or 0.0)) >= DECIDER_NOTICE_INTERVAL_SECONDS
    except Exception:
        logger.exception("Failed reading the intent decider notice state")
        return False
    finally:
        conn.close()


def _mark_decider_notice(settings: Any, session_id: str, kind: str) -> None:
    if not session_id:
        return
    conn = _connect(settings)
    try:
        conn.execute(
            """INSERT INTO chat_decider_notices (session_id, last_kind, last_notice_at)
               VALUES (?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   last_kind = excluded.last_kind,
                   last_notice_at = excluded.last_notice_at""",
            (session_id, kind or "", time.time()),
        )
        conn.commit()
    except Exception:
        logger.exception("Failed recording the intent decider notice")
    finally:
        conn.close()


def _compute_decider_notice(settings: Any, session_id: str) -> str:
    """Return (and record) the one-off 理解指令-down notice, or "" when none is due.

    The cheap gate runs first: most messages arrive while a notice is still
    fresh. Shared by the web and phone surfaces so both announce the outage
    exactly once per six hours without blocking the fallback answer.
    """
    if not _should_emit_decider_notice(settings, session_id):
        return ""
    try:
        health = intent_decider.get_decider_health(settings)
    except Exception:
        logger.debug("intent decider health unavailable", exc_info=True)
        return ""
    if not health.get("unhealthy"):
        return ""
    text = intent_decider.decider_fallback_notice(health)
    if not text:
        return ""
    _mark_decider_notice(settings, session_id, str(health.get("last_error_kind") or ""))
    return text


def _emit_card_reply(port: Any, msg: Any, text: str, card: dict[str, Any]) -> None:
    port.emit("message", {"text": text, "kind": "action_card", "action_card": card})
    if hasattr(port, "_persist_turn"):
        port._persist_turn(
            msg,
            text,
            kind="action_card",
            metadata={
                "card_id": card["id"],
                "card_title": card["title"],
                "card_text": card["text"],
            },
        )


def _persist_follow_up_message(
    settings: Any,
    session_id: str,
    text: str,
    card: dict[str, Any] | None = None,
) -> dict[str, Any]:
    created_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    item: dict[str, Any] = {
        "role": "assistant",
        "text": text,
        "created_at": created_at,
        "kind": "action_card" if card else "chat",
    }
    if card is not None:
        item["action_card"] = card
    if not session_id:
        return item
    try:
        store = get_transcript_store(settings)
        metadata = None
        if card is not None:
            metadata = {
                "card_id": card["id"],
                "card_title": card["title"],
                "card_text": card["text"],
            }
        row = store.append(
            session_id,
            "assistant",
            text,
            kind=item["kind"],
            metadata=metadata,
            channel="web",
            operator_id="web-console",
        )
        row_created_at = getattr(row, "created_at", "")
        if row_created_at:
            item["created_at"] = row_created_at
    except Exception:
        logger.exception("Failed to persist chat intent follow-up message")
    return item


def _resolve_merge_branches(
    settings: Any,
    worktree_id: str,
    fallback_source: str,
    fallback_target: str,
) -> tuple[str, str]:
    source_branch = fallback_source.strip()
    target_branch = fallback_target.strip()
    if worktree_id:
        try:
            scope = merge_mod._resolve_scope(settings, worktree_id)
            live_source = merge_mod._current_branch(settings, str(scope["worktree_path"]))
            live_target = merge_mod._target_branch(settings, scope)
            if live_source:
                source_branch = live_source
            if live_target:
                target_branch = live_target
        except Exception:
            pass
    return source_branch or "当前工作分支", target_branch or "main"


def _build_merge_confirmation_card(
    settings: Any,
    session_id: str,
    *,
    worktree_id: str,
    repo_id: str,
    source_branch: str,
    target_branch: str,
) -> tuple[str, dict[str, Any]]:
    text = f"把 {source_branch} 合并到 {target_branch}？合并后不能自动撤销，确认要合并吗？"
    card = create_action_card(
        settings,
        session_id=session_id,
        title="确认合并到主分支",
        text=text,
        buttons=[
            {
                "label": "确认合并",
                "action": "confirm_merge_worktree",
                "style": "primary",
                "client_payload": {},
                "server_payload": {
                    "worktree_id": worktree_id,
                    "repo_id": repo_id,
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                },
            },
            {
                "label": "取消",
                "action": "cancel_action",
                "style": "secondary",
                "client_payload": {},
                "server_payload": {"outcome": "已取消合并"},
            },
        ],
    )
    return text, card


def post_goal_finish_report(
    settings: Any,
    *,
    session_id: str,
    report_text: str,
    goal_id: str,
    repo_id: str,
    worktree_id: str,
    source_branch: str,
    target_branch: str,
    outcome: str,
) -> dict[str, Any]:
    """Persist the goal finish report and its three-button action card into web chat."""
    card = create_action_card(
        settings,
        session_id=session_id,
        title="目标已结束",
        text=report_text,
        buttons=[
            {
                "label": "查看改动",
                "action": "open_project",
                "style": "secondary",
                "client_payload": {
                    "view": "code",
                    "tab": "workspace",
                    "repo_id": repo_id,
                    "worktree_id": worktree_id,
                },
                "server_payload": {},
            },
            {
                "label": "合并到主分支",
                "action": "request_merge_worktree",
                "style": "primary",
                "client_payload": {},
                "server_payload": {
                    "goal_id": goal_id,
                    "repo_id": repo_id,
                    "worktree_id": worktree_id,
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                },
            },
            {
                "label": "再跑 1 小时",
                "action": "rerun_goal_one_hour",
                "style": "secondary",
                "client_payload": {},
                "server_payload": {
                    "goal_id": goal_id,
                    "repo_id": repo_id,
                    "worktree_id": worktree_id,
                    "duration_minutes": 60,
                    "goal_text": outcome,
                },
            },
        ],
    )
    _persist_follow_up_message(settings, session_id, report_text, card)
    return card


# ---------------------------------------------------------------------------
# Intent execution paths (chat message & server action endpoint)
# ---------------------------------------------------------------------------

def _execute_create_project_now(
    settings: Any,
    session_id: str,
    project_name: str,
) -> tuple[str, dict[str, Any] | None]:
    try:
        result = svc.create_managed_project(settings, name=project_name)
    except svc.SupervisorServiceError as exc:
        if exc.code == "project_exists":
            existing = _find_repo_by_name(_safe_list_repos(settings), project_name)
            if existing is not None:
                _set_recent_repo(settings, session_id, str(existing["id"]))
                return _build_existing_project_card(settings, session_id, existing)
        return exc.message, None

    repo = result["repository"]
    worktree = result.get("worktree")
    _set_recent_repo(settings, session_id, str(repo["id"]))
    return _build_created_project_card(settings, session_id, repo, worktree)


def _handle_create_project_intent(
    settings: Any,
    session_id: str,
    intent: Intent,
) -> tuple[str, dict[str, Any] | None]:
    if not svc.is_enabled(settings):
        return "目前还没有开启项目管理功能，请先检查配置。", None

    if intent.mentions_github:
        configured = _has_github_configured(settings)
        name_part = f"「{intent.project_name}」" if intent.project_name else ""
        if configured:
            msg = (
                f"检测到已配置 GitHub，但这个版本只会在本机为你创建托管的 Git 仓库{name_part}，"
                "不会在 GitHub 上新建。要为你创建本机托管项目吗？"
            )
        else:
            msg = (
                f"这个版本只会在本机为你创建托管的 Git 仓库{name_part}，"
                "不会在 GitHub 上新建。要为你创建本机托管项目吗？"
            )
        card = create_action_card(
            settings,
            session_id=session_id,
            title="创建本机托管项目",
            text=msg,
            buttons=[
                {
                    "label": "创建本机托管项目",
                    "action": "create_hosted_project",
                    "style": "primary",
                    "client_payload": {},
                    "server_payload": {"project_name": intent.project_name or ""},
                },
                {
                    "label": "取消",
                    "action": "cancel_action",
                    "style": "secondary",
                    "client_payload": {},
                    "server_payload": {"outcome": "已取消创建项目"},
                },
            ],
        )
        return msg, card

    if not intent.project_name:
        _set_pending_state(settings, session_id, "awaiting_project_name", {})
        return "项目叫什么名字？（英文字母、数字、短横线）", None

    return _execute_create_project_now(settings, session_id, intent.project_name)


def _handle_start_timed_goal_intent(
    settings: Any,
    session_id: str,
    intent: Intent,
    raw_text: str,
) -> tuple[str, dict[str, Any] | None]:
    if not svc.is_enabled(settings):
        return "目前还没有开启项目目标功能，请先检查配置。", None

    repos = _safe_list_repos(settings)
    if not repos:
        text = "目前还没有项目，请先创建一个项目再开始持续改进。"
        card = create_action_card(
            settings,
            session_id=session_id,
            title="还没有项目",
            text=text,
            buttons=[
                {
                    "label": "去创建项目",
                    "action": "prefill_create_project",
                    "style": "primary",
                    "client_payload": {},
                    "server_payload": {"prompt": "帮我创建一个项目叫 "},
                }
            ],
        )
        return text, card

    target_repo: dict[str, Any] | None = None
    explicit_missing_name = ""

    # 1. Explicit project name in message, or matching a known project name in text.
    if intent.project_name:
        target_repo = _find_repo_by_name(repos, intent.project_name)
        if target_repo is None:
            explicit_missing_name = intent.project_name
    else:
        target_repo = _match_repo_mentioned_in_text(repos, raw_text)

    # 2. Most recently created/opened project in this chat session.
    if target_repo is None and not explicit_missing_name:
        recent_id = _get_session_state(settings, session_id).get("recent_repo_id", "")
        if recent_id:
            target_repo = _find_repo_by_id(repos, recent_id)

    # 3. Single existing project.
    if target_repo is None and not explicit_missing_name and len(repos) == 1:
        target_repo = repos[0]

    duration_minutes = int(intent.duration_minutes or 60)

    if target_repo is None:
        prefix = (
            f"没有找到叫「{explicit_missing_name}」的项目，要在哪个项目上干？"
            if explicit_missing_name
            else "在哪个项目上干？"
        )
        return _build_pick_project_card(
            settings,
            session_id,
            repos,
            duration_minutes,
            intent.goal_text,
            prefix_text=prefix,
        )

    _set_recent_repo(settings, session_id, str(target_repo["id"]))
    return _build_timed_goal_confirmation_card(
        settings,
        session_id,
        target_repo,
        duration_minutes,
        intent.goal_text,
    )


def format_relative_deadline_zh(minutes: int) -> str:
    """Format a positive minute duration as a timezone-independent relative end phrase."""
    total = max(1, int(minutes))
    if total < 60:
        return f"大约 {total} 分钟后结束"
    hours, rem = divmod(total, 60)
    if rem == 0:
        return f"大约 {hours} 小时后结束"
    return f"大约 {hours} 小时 {rem} 分后结束"


def _normalize_deadline_iso(deadline_at: Any) -> str | None:
    if deadline_at is None or isinstance(deadline_at, bool):
        return None
    if isinstance(deadline_at, (int, float)):
        try:
            return datetime.fromtimestamp(float(deadline_at), tz=timezone.utc).isoformat(timespec="seconds")
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(deadline_at, str):
        raw = deadline_at.strip()
        if not raw:
            return None
        iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat(timespec="seconds")
    return None


def _start_timed_goal_from_action(
    server: Any,
    settings: Any,
    session_id: str,
    payload: dict[str, Any],
    *,
    enable_check_script: bool = True,
) -> tuple[str, str, dict[str, Any] | None]:
    """Start a continuous timed goal after explicit confirmation.

    Returns ``(action_outcome, reply_text, follow_up_card)``.
    """
    repo_id = str(payload.get("repo_id") or "")
    duration_minutes = int(payload.get("duration_minutes") or 60)
    duration_minutes = max(MIN_DURATION_MINUTES, min(MAX_DURATION_MINUTES, duration_minutes))
    goal_text = str(payload.get("goal_text") or "").strip() or DEFAULT_TIMED_GOAL_OUTCOME

    repos = _safe_list_repos(settings)
    repo = _find_repo_by_id(repos, repo_id)
    if repo is None:
        msg = "找不到对应的项目，请到「项目」页面确认后再试。"
        card = create_action_card(
            settings,
            session_id=session_id,
            title="找不到项目",
            text=msg,
            buttons=[
                {
                    "label": "打开项目列表",
                    "action": "open_code",
                    "style": "primary",
                    "client_payload": {"view": "code"},
                    "server_payload": {},
                }
            ],
        )
        return "启动失败：找不到项目", msg, card

    _, role_overrides, executor_refusal = _summarize_roles_and_overrides(settings)
    if executor_refusal:
        card = create_action_card(
            settings,
            session_id=session_id,
            title="执行者未就绪",
            text=executor_refusal,
            buttons=[
                {
                    "label": "查看执行者设置",
                    "action": "open_workers",
                    "style": "primary",
                    "client_payload": {"view": "workers"},
                    "server_payload": {},
                }
            ],
        )
        return "启动失败：执行者未就绪", executor_refusal, card

    try:
        if enable_check_script and not _repo_has_validation(settings, str(repo["id"])):
            supervisor_validation.set_repo_validation(settings, str(repo["id"]), ["check-script"])
        requested_wt_id = str(payload.get("worktree_id") or "").strip()
        if requested_wt_id:
            worktree = next(
                (w for w in (repo.get("worktrees") or []) if str(w.get("id") or "") == requested_wt_id),
                None,
            )
            if worktree is None:
                raise svc.SupervisorServiceError(
                    HTTPStatus.NOT_FOUND,
                    "找不到原来的工作目录，请到「项目」页面确认后再试。",
                    code="worktree_not_found",
                )
        else:
            worktree = _ensure_ready_worktree(settings, repo)
        body: dict[str, Any] = {
            "repo_id": str(repo["id"]),
            "worktree_id": str(worktree["id"]),
            "title": _short_goal_title(goal_text),
            "outcome": goal_text,
            "role_overrides": role_overrides,
        }
        if session_id:
            body["origin_session_id"] = session_id
        if GOAL_TIMEBOX_SUPPORTED:
            body["duration_minutes"] = duration_minutes
            body["mode"] = "continuous"
        created_goal = goals_mod.create_goal(server, settings, body)
        if not isinstance(created_goal, dict):
            created_goal = {}
    except svc.SupervisorServiceError as exc:
        target_view = "workers" if "executor" in (exc.code or "") else "code"
        btn_label = "查看执行者设置" if target_view == "workers" else "打开项目 › 目标"
        client_payload = (
            {"view": "workers"}
            if target_view == "workers"
            else {"view": "code", "tab": "goals", "repo_id": str(repo["id"]), "worktree_id": ""}
        )
        card = create_action_card(
            settings,
            session_id=session_id,
            title="无法开始目标",
            text=exc.message,
            buttons=[
                {
                    "label": btn_label,
                    "action": "open_workers" if target_view == "workers" else "open_goal_progress",
                    "style": "primary",
                    "client_payload": client_payload,
                    "server_payload": {},
                }
            ],
        )
        return f"启动失败：{exc.message}", exc.message, card

    _set_recent_repo(settings, session_id, str(repo["id"]))
    deadline_iso = _normalize_deadline_iso(created_goal.get("deadline_at"))
    if deadline_iso:
        relative_end = format_relative_deadline_zh(duration_minutes)
        reply_text = f"已经开始了，{relative_end}。随时在 项目 › 目标 里看进度或停止。"
        duration_label = _format_duration_zh(duration_minutes)
        action_outcome = f"已开始持续改进「{repo['name']}」（{duration_label}）"
    else:
        raw_rounds = created_goal.get("max_iterations")
        try:
            rounds = int(raw_rounds) if raw_rounds is not None else goals_mod.DEFAULT_MAX_ITERATIONS
        except (TypeError, ValueError):
            rounds = goals_mod.DEFAULT_MAX_ITERATIONS
        if rounds <= 0:
            rounds = goals_mod.DEFAULT_MAX_ITERATIONS
        reply_text = f"已经开始了，最多跑 {rounds} 轮。随时在 项目 › 目标 里看进度或停止。"
        action_outcome = f"已开始持续改进「{repo['name']}」（最多 {rounds} 轮）"
    card = create_action_card(
        settings,
        session_id=session_id,
        title="已开始持续改进",
        text=reply_text,
        deadline_at=deadline_iso,
        buttons=[
            {
                "label": "查看进度",
                "action": "open_goal_progress",
                "style": "primary",
                "client_payload": {
                    "view": "code",
                    "tab": "goals",
                    "repo_id": str(repo["id"]),
                    "worktree_id": str(worktree["id"]),
                },
                "server_payload": {},
            }
        ],
    )
    return action_outcome, reply_text, card


DECIDER_CONFIDENCE_DIRECT = 0.8
DECIDER_CONFIDENCE_CLARIFY = 0.5

_DECIDER_QUESTION_END_RE = re.compile(r"(?:[?？]|[吗呢]\s*[?？。！!]?)\s*$")
_DECIDER_QUESTION_START_RE = re.compile(r"^\s*(?:怎么|如何|为什么|什么是)")

_CLARIFY_ACTION_TEXT: dict[str, str] = {
    "progress": "查看项目进度",
    "stop": "停止正在运行的目标",
    "resume": "继续之前的项目目标",
    "create_project": "创建一个新项目",
    "timed_goal": "让助手持续改进项目",
}

_ACTIVE_GOAL_STATES = ("running", "waiting_user", "waiting_quota")


def _is_question_for_decider(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    return bool(_DECIDER_QUESTION_END_RE.search(raw) or _DECIDER_QUESTION_START_RE.search(raw))


def _format_remaining_zh(remaining_seconds: int | None) -> str:
    if remaining_seconds is None:
        return ""
    rem = max(0, int(remaining_seconds))
    if rem == 0:
        return "已到结束时间"
    if rem < 60:
        return "还剩不到 1 分钟"
    total_mins = math.ceil(rem / 60)
    if total_mins < 60:
        return f"还剩 {total_mins} 分钟"
    hours, mins = divmod(total_mins, 60)
    if mins == 0:
        return f"还剩 {hours} 小时"
    return f"还剩 {hours} 小时 {mins} 分"


def _build_clarifying_card(
    settings: Any,
    session_id: str,
    choice: str,
    raw_text: str,
) -> tuple[str, dict[str, Any]]:
    action_phrase = _CLARIFY_ACTION_TEXT.get(choice, "执行这个操作")
    text = f"你是想{action_phrase}吗？"
    card = create_action_card(
        settings,
        session_id=session_id,
        title="确认你的意思",
        text=text,
        buttons=[
            {
                "label": "是",
                "action": "confirm_clarified_intent",
                "style": "primary",
                "client_payload": {},
                "server_payload": {"intent_choice": choice, "raw_text": raw_text},
            },
            {
                "label": "不是",
                "action": "reject_clarified_intent",
                "style": "secondary",
                "client_payload": {},
                "server_payload": {"raw_text": raw_text},
            },
        ],
    )
    return text, card


def _build_duration_picker_card(
    settings: Any,
    session_id: str,
    repo: dict[str, Any],
    goal_text: str,
    *,
    title: str = "选择持续时间",
    text: str = "",
    cancel_outcome: str = "已取消",
) -> tuple[str, dict[str, Any]]:
    repo_id = str(repo.get("id") or "")
    repo_name = str(repo.get("name") or "")
    goal_display = goal_text.strip() or DEFAULT_TIMED_GOAL_OUTCOME
    prompt_text = text or f"在 {repo_name} 上持续改进多久？"
    card = create_action_card(
        settings,
        session_id=session_id,
        title=title,
        text=prompt_text,
        buttons=[
            {
                "label": "1 小时",
                "action": "pick_timed_goal_duration",
                "style": "primary",
                "client_payload": {},
                "server_payload": {"repo_id": repo_id, "duration_minutes": 60, "goal_text": goal_display},
            },
            {
                "label": "3 小时",
                "action": "pick_timed_goal_duration",
                "style": "secondary",
                "client_payload": {},
                "server_payload": {"repo_id": repo_id, "duration_minutes": 180, "goal_text": goal_display},
            },
            {
                "label": "10 小时",
                "action": "pick_timed_goal_duration",
                "style": "secondary",
                "client_payload": {},
                "server_payload": {"repo_id": repo_id, "duration_minutes": 600, "goal_text": goal_display},
            },
            {
                "label": "取消",
                "action": "cancel_action",
                "style": "secondary",
                "client_payload": {},
                "server_payload": {"outcome": cancel_outcome},
            },
        ],
    )
    return prompt_text, card


def _handle_progress_intent(
    settings: Any,
    session_id: str,
    raw_text: str,
) -> tuple[str, dict[str, Any] | None]:
    if not svc.is_enabled(settings):
        return "目前还没有开启项目目标功能，请先检查配置。", None

    repos = _safe_list_repos(settings)
    try:
        goals = goals_mod.list_goals(settings)
    except svc.SupervisorServiceError:
        return "现在没有在跑的目标。", None

    matched_repo = _match_repo_mentioned_in_text(repos, raw_text)
    selected_goal: dict[str, Any] | None = None

    if matched_repo is not None:
        repo_goals = [g for g in goals if g.get("repo_id") == matched_repo.get("id")]
        active_repo_goals = [g for g in repo_goals if g.get("state") in _ACTIVE_GOAL_STATES]
        selected_goal = active_repo_goals[0] if active_repo_goals else (repo_goals[0] if repo_goals else None)
    else:
        active_goals = [g for g in goals if g.get("state") in _ACTIVE_GOAL_STATES]
        if active_goals:
            selected_goal = active_goals[0]

    if selected_goal is None:
        return "现在没有在跑的目标。", None

    try:
        detail = goals_mod.get_goal(settings, str(selected_goal["id"]))
    except svc.SupervisorServiceError:
        detail = selected_goal

    repo = _find_repo_by_id(repos, str(detail.get("repo_id") or ""))
    repo_name = str((repo or {}).get("name") or detail.get("title") or "项目")
    repo_id = str(detail.get("repo_id") or "")
    wt_id = str(detail.get("worktree_id") or "")
    if repo_id:
        _set_recent_repo(settings, session_id, repo_id)

    state = str(detail.get("state") or "")
    stop_reason = str(detail.get("stop_reason") or "")
    state_label = goal_status_label(state, stop_reason)
    rem_text = (
        _format_remaining_zh(detail.get("remaining_seconds"))
        if state in _ACTIVE_GOAL_STATES
        else ""
    )
    rounds_total = int(detail.get("rounds_total") or 0)
    rounds_passed = int(detail.get("rounds_passed") or 0)

    iterations = detail.get("iterations") or []
    last_round_title = ""
    for it in reversed(iterations):
        rt = str(it.get("round_title") or "").strip()
        if rt:
            last_round_title = rt
            break

    parts = [f"{repo_name}（{state_label}）"]
    if rem_text:
        parts.append(rem_text)
    parts.append(f"已跑 {rounds_total} 轮 / 通过 {rounds_passed} 轮")
    line1 = "，".join(parts) + "。"
    if last_round_title:
        text = f"{line1}\n最近一轮：{last_round_title}"
    else:
        text = line1

    card = create_action_card(
        settings,
        session_id=session_id,
        title="目标进度",
        text=text,
        buttons=[
            {
                "label": "查看进度",
                "action": "open_goal_progress",
                "style": "primary",
                "client_payload": {
                    "view": "code",
                    "tab": "goals",
                    "repo_id": repo_id,
                    "worktree_id": wt_id,
                },
                "server_payload": {},
            }
        ],
    )
    return text, card


def _handle_stop_intent(
    settings: Any,
    session_id: str,
    raw_text: str,
) -> tuple[str, dict[str, Any] | None]:
    if not svc.is_enabled(settings):
        return "目前还没有开启项目目标功能，请先检查配置。", None

    repos = _safe_list_repos(settings)
    try:
        goals = goals_mod.list_goals(settings)
    except svc.SupervisorServiceError:
        return "现在没有在跑的目标。", None

    active_goals = [g for g in goals if g.get("state") in _ACTIVE_GOAL_STATES]
    if not active_goals:
        return "现在没有在跑的目标。", None

    matched_repo = _match_repo_mentioned_in_text(repos, raw_text)
    target_goal: dict[str, Any] | None = None
    if matched_repo is not None:
        for g in active_goals:
            if g.get("repo_id") == matched_repo.get("id"):
                target_goal = g
                break
        if target_goal is None:
            return f"「{matched_repo['name']}」现在没有在跑的目标。", None
    else:
        recent_id = _get_session_state(settings, session_id).get("recent_repo_id", "")
        if recent_id:
            for g in active_goals:
                if g.get("repo_id") == recent_id:
                    target_goal = g
                    break
        if target_goal is None:
            target_goal = active_goals[0]

    repo = _find_repo_by_id(repos, str(target_goal.get("repo_id") or ""))
    repo_name = str((repo or {}).get("name") or target_goal.get("title") or "当前项目")
    if repo is not None:
        _set_recent_repo(settings, session_id, str(repo["id"]))

    text = f"要停止 {repo_name} 的目标吗？已经做好的改动会保留。"
    card = create_action_card(
        settings,
        session_id=session_id,
        title="确认停止目标",
        text=text,
        buttons=[
            {
                "label": "停止",
                "action": "confirm_stop_goal",
                "style": "primary",
                "client_payload": {},
                "server_payload": {
                    "goal_id": str(target_goal["id"]),
                    "repo_id": str(target_goal.get("repo_id") or ""),
                    "repo_name": repo_name,
                },
            },
            {
                "label": "先不停",
                "action": "cancel_action",
                "style": "secondary",
                "client_payload": {},
                "server_payload": {"outcome": "先不停止，继续保持当前状态"},
            },
        ],
    )
    return text, card


def _handle_resume_intent(
    settings: Any,
    session_id: str,
    raw_text: str,
) -> tuple[str, dict[str, Any] | None]:
    if not svc.is_enabled(settings):
        return "目前还没有开启项目目标功能，请先检查配置。", None

    repos = _safe_list_repos(settings)
    if not repos:
        return "目前还没有项目，请先创建一个项目再继续。", None

    try:
        goals = goals_mod.list_goals(settings)
    except svc.SupervisorServiceError:
        goals = []

    matched_repo = _match_repo_mentioned_in_text(repos, raw_text)
    target_repo: dict[str, Any] | None = matched_repo
    last_goal: dict[str, Any] | None = None

    if target_repo is not None:
        repo_goals = [g for g in goals if g.get("repo_id") == target_repo.get("id")]
        non_active = [g for g in repo_goals if g.get("state") not in _ACTIVE_GOAL_STATES]
        last_goal = non_active[0] if non_active else (repo_goals[0] if repo_goals else None)
    else:
        recent_id = _get_session_state(settings, session_id).get("recent_repo_id", "")
        if recent_id:
            recent_goals = [g for g in goals if g.get("repo_id") == recent_id]
            if recent_goals:
                non_active = [g for g in recent_goals if g.get("state") not in _ACTIVE_GOAL_STATES]
                last_goal = non_active[0] if non_active else recent_goals[0]
                target_repo = _find_repo_by_id(repos, recent_id)
        if last_goal is None and goals:
            non_active = [g for g in goals if g.get("state") not in _ACTIVE_GOAL_STATES]
            last_goal = non_active[0] if non_active else goals[0]
            target_repo = _find_repo_by_id(repos, str(last_goal.get("repo_id") or ""))

    if target_repo is None or last_goal is None:
        return "之前还没有做过项目目标，请直接告诉我你想在哪个项目上做什么。", None

    _set_recent_repo(settings, session_id, str(target_repo["id"]))
    last_goal_text = str(last_goal.get("outcome") or "").strip() or DEFAULT_TIMED_GOAL_OUTCOME
    short_title = _short_goal_title(last_goal_text)
    prompt_text = f"要继续在 {target_repo['name']} 上做「{short_title}」吗？选一下这次跑多久："
    return _build_duration_picker_card(
        settings,
        session_id,
        target_repo,
        last_goal_text,
        title="继续之前的目标",
        text=prompt_text,
        cancel_outcome="已取消继续目标",
    )


def _handle_decider_timed_goal_intent(
    settings: Any,
    session_id: str,
    raw_text: str,
) -> tuple[str, dict[str, Any] | None]:
    if not svc.is_enabled(settings):
        return "目前还没有开启项目目标功能，请先检查配置。", None

    repos = _safe_list_repos(settings)
    if not repos:
        return _handle_start_timed_goal_intent(
            settings,
            session_id,
            Intent(kind="start_timed_goal", duration_minutes=None, goal_text=raw_text),
            raw_text,
        )

    timed_match = _TIMED_VERB_DURATION_RE.search(raw_text) or _DURATION_VERB_RE.search(raw_text)
    duration_minutes = _parse_duration_match(timed_match) if timed_match else None
    goal_text = _extract_goal_text(raw_text, verb_span=timed_match.span()) if timed_match else _strip_goal_edges(raw_text)

    if duration_minutes is not None:
        return _handle_start_timed_goal_intent(
            settings,
            session_id,
            Intent(
                kind="start_timed_goal",
                duration_minutes=duration_minutes,
                goal_text=goal_text,
                mentions_github=bool(_GITHUB_RE.search(raw_text)),
            ),
            raw_text,
        )

    target_repo = _match_repo_mentioned_in_text(repos, raw_text)
    if target_repo is None:
        recent_id = _get_session_state(settings, session_id).get("recent_repo_id", "")
        if recent_id:
            target_repo = _find_repo_by_id(repos, recent_id)
    if target_repo is None and len(repos) == 1:
        target_repo = repos[0]

    if target_repo is None:
        return _build_pick_project_card(
            settings,
            session_id,
            repos,
            0,
            goal_text or DEFAULT_TIMED_GOAL_OUTCOME,
            prefix_text="在哪个项目上持续改进？",
        )

    _set_recent_repo(settings, session_id, str(target_repo["id"]))
    return _build_duration_picker_card(
        settings,
        session_id,
        target_repo,
        goal_text or DEFAULT_TIMED_GOAL_OUTCOME,
    )


def _dispatch_decider_choice(
    settings: Any,
    session_id: str,
    choice: str,
    raw_text: str,
) -> tuple[str, dict[str, Any] | None]:
    if choice == "progress":
        return _handle_progress_intent(settings, session_id, raw_text)
    if choice == "stop":
        return _handle_stop_intent(settings, session_id, raw_text)
    if choice == "resume":
        return _handle_resume_intent(settings, session_id, raw_text)
    if choice == "create_project":
        return _handle_create_project_intent(
            settings,
            session_id,
            Intent(
                kind="create_project",
                project_name=None,
                mentions_github=bool(_GITHUB_RE.search(raw_text)),
            ),
        )
    if choice == "timed_goal":
        return _handle_decider_timed_goal_intent(settings, session_id, raw_text)
    return "", None


def has_pending_skip_decider(settings: Any, session_id: str, text: str) -> bool:
    """Return True when the owner just rejected a clarifying card for this exact text."""
    try:
        pending = _has_active_pending(settings, session_id)
    except Exception:
        return False
    if pending is None or pending.get("pending_kind") != "skip_decider_once":
        return False
    expected = str((pending.get("pending_payload") or {}).get("text") or "").strip()
    return bool(expected and expected == (text or "").strip())


def has_pending_or_detected_intent(settings: Any, session_id: str, text: str) -> bool:
    """Return True if web chat can handle this message without requiring a chat model."""
    try:
        pending = _has_active_pending(settings, session_id)
        if pending is not None and pending.get("pending_kind") != "skip_decider_once":
            return True
    except Exception:
        logger.exception("chat intent state unavailable")
    return detect_intent(text) is not None


def get_action_card_session(settings: Any, action_id: str) -> str | None:
    """Return the session a stored action button belongs to, or ``None``.

    Phone channels use this to refuse a button that was issued to a different
    chat *before* the one-use claim in :func:`execute_action` runs, so a
    forwarded or replayed callback cannot act on someone else's card.
    """
    if not settings or not action_id:
        return None
    conn = _connect(settings)
    try:
        row = conn.execute(
            "SELECT c.session_id FROM chat_action_buttons b "
            "JOIN chat_action_cards c ON c.id = b.card_id WHERE b.id = ?",
            (str(action_id),),
        ).fetchone()
    finally:
        conn.close()
    return str(row["session_id"]) if row is not None else None


def store_pending_choice(
    settings: Any,
    session_id: str,
    action_ids: list[str],
    *,
    ttl_seconds: int = PENDING_TTL_SECONDS,
) -> None:
    """Remember a numbered choice for a phone channel without live buttons.

    Feishu renders action cards as numbered text; the owner's next message
    (「1」/「2」…) is resolved against these ids within the TTL — the same
    mechanism as the web pending-name flow.
    """
    _set_pending_state(
        settings,
        session_id,
        "awaiting_choice",
        {"action_ids": [str(a) for a in action_ids]},
        ttl_seconds=ttl_seconds,
    )


def pop_pending_choice(settings: Any, session_id: str) -> list[str] | None:
    """Return and clear an active numbered-choice pending, or ``None`` if none."""
    pending = _has_active_pending(settings, session_id)
    if pending is None or pending.get("pending_kind") != "awaiting_choice":
        return None
    payload = pending.get("pending_payload") or {}
    action_ids = payload.get("action_ids")
    _clear_pending_state(settings, session_id)
    if isinstance(action_ids, list):
        return [str(a) for a in action_ids]
    return []


async def resolve_intent_turn(
    settings: Any,
    session_id: str,
    raw: str,
    *,
    model: Any = None,
) -> tuple[bool, str, dict[str, Any] | None, str]:
    """Resolve one chat turn to a reply; the single source shared by web & phone.

    Returns ``(handled, reply_text, card, notice_text)``. When ``handled`` is
    ``False`` the caller falls back to its normal chat path; ``notice_text``
    may still carry a one-off 理解指令-down notice to surface first. Running
    the same patterns → decider → cards here is what lets the phone bots answer
    exactly like the web 对话.
    """
    if not raw:
        return (False, "", None, "")

    # 1. Active pending follow-up state (awaiting project name, or skip_decider_once).
    pending = _has_active_pending(settings, session_id)
    if pending is not None and pending["pending_kind"] == "skip_decider_once":
        expected = str((pending.get("pending_payload") or {}).get("text") or "").strip()
        _clear_pending_state(settings, session_id)
        if expected == raw:
            return (False, "", None, "")
    elif pending is not None and pending["pending_kind"] == "awaiting_project_name":
        _clear_pending_state(settings, session_id)
        if raw in {"取消", "算了", "不用了", "不建了", "cancel"}:
            return (True, "好的，已取消创建项目。", None, "")
        candidate = raw.strip().strip("`\"'「『」』")
        reply_text, card = _execute_create_project_now(settings, session_id, candidate)
        return (True, reply_text, card, "")

    # 2. Deterministic pattern first (always wins).
    direct = detect_intent(raw)
    if direct is not None:
        if direct.kind == "create_project":
            reply_text, card = _handle_create_project_intent(settings, session_id, direct)
            return (True, reply_text, card, "")
        if direct.kind == "start_timed_goal":
            reply_text, card = _handle_start_timed_goal_intent(settings, session_id, direct, raw)
            return (True, reply_text, card, "")
        return (False, "", None, "")

    # 3. Decision model (Gemini Decisions / Jev / custom) if configured.
    if not raw.startswith("/"):
        decider_configured = intent_decider.is_configured(settings)
        decision = await asyncio.to_thread(
            intent_decider.decide_intent,
            settings,
            raw,
            intent_decider.INTENT_OPTIONS,
        )
        if decision is not None:
            if decision.choice == "chat" or decision.confidence < DECIDER_CONFIDENCE_CLARIFY:
                return (False, "", None, "")
            if _is_question_for_decider(raw) or decision.confidence < DECIDER_CONFIDENCE_DIRECT:
                reply_text, card = _build_clarifying_card(settings, session_id, decision.choice, raw)
                return (True, reply_text, card, "")
            reply_text, card = _dispatch_decider_choice(settings, session_id, decision.choice, raw)
            if not reply_text and card is None:
                return (False, "", None, "")
            return (True, reply_text, card, "")
        if decider_configured:
            # Decider was configured and errored/timed out -> fall back to normal
            # chat, saying so once when it is really unhealthy.
            return (False, "", None, _compute_decider_notice(settings, session_id))

    # 4. Legacy optional chat-tier LLM fallback when no decider is configured.
    intent = await classify_intent(raw, settings, model=model)
    if intent is None:
        return (False, "", None, "")
    if intent.kind == "create_project":
        reply_text, card = _handle_create_project_intent(settings, session_id, intent)
        return (True, reply_text, card, "")
    if intent.kind == "start_timed_goal":
        reply_text, card = _handle_start_timed_goal_intent(settings, session_id, intent, raw)
        return (True, reply_text, card, "")
    return (False, "", None, "")


async def maybe_handle_web_chat(
    msg: Any,
    port: Any,
    settings: Any,
    prompt: str,
    *,
    model: Any = None,
) -> bool:
    """Intercept owner web-chat intents and pending follow-ups.

    Returns ``True`` when this module handled the turn and emitted the reply.
    """
    try:
        session_id = str(getattr(port, "durable_session_id", "") or "")
        raw = (prompt or "").strip()
        handled, reply_text, card, notice = await resolve_intent_turn(
            settings, session_id, raw, model=model,
        )
        if notice:
            _emit_decider_notice(port, msg, notice)
        if not handled:
            return False
        if card is not None:
            _emit_card_reply(port, msg, reply_text, card)
        else:
            _emit_plain_reply(port, msg, reply_text)
        return True
    except Exception:
        logger.exception("chat intent handling failed")
        return False


def _decode_json_dict(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def execute_action(
    server: Any,
    settings: Any,
    action_id: str,
) -> tuple[int, dict[str, Any]]:
    """Atomically claim and execute a server-stored action button by ID.

    Only ``action_id`` from the client is trusted; all action routing and
    parameters come from ``chat_action_buttons`` in SQLite. Replays return 409.
    Navigation-only buttons never mark a card used and remain usable even after
    an action button on the same card has been claimed.
    """
    clean_id = (action_id or "").strip()
    if not clean_id:
        return HTTPStatus.BAD_REQUEST, {"error": "缺少操作编号", "error_code": "invalid_action_id"}

    current_card: dict[str, Any] | None = None
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        btn = conn.execute(
            """SELECT b.id AS btn_id, b.card_id, b.label, b.action,
                      b.client_payload_json, b.server_payload_json,
                      c.session_id, c.used, c.outcome
               FROM chat_action_buttons b
               JOIN chat_action_cards c ON c.id = b.card_id
               WHERE b.id = ?""",
            (clean_id,),
        ).fetchone()
        if btn is None:
            conn.rollback()
            return HTTPStatus.NOT_FOUND, {"error": "找不到对应的操作，可能已过期", "error_code": "action_not_found"}

        card_id = str(btn["card_id"])
        session_id = str(btn["session_id"])
        action = str(btn["action"])
        is_nav_only = action in _NAVIGATION_ONLY_ACTIONS

        if is_nav_only:
            current_card = _row_to_card(conn, card_id)
            conn.rollback()
        else:
            if int(btn["used"]) != 0:
                conn.rollback()
                return HTTPStatus.CONFLICT, {
                    "error": "这个操作已经处理过了，不能重复使用",
                    "error_code": "action_already_used",
                }
            # Claim the whole card immediately inside transaction so concurrent requests cannot double-fire.
            cur = conn.execute(
                "UPDATE chat_action_cards SET used = 1, outcome = ? WHERE id = ? AND used = 0",
                (f"已选择「{btn['label']}」", card_id),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return HTTPStatus.CONFLICT, {
                    "error": "这个操作已经处理过了，不能重复使用",
                    "error_code": "action_already_used",
                }
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    client_payload = _decode_json_dict(btn["client_payload_json"])
    if is_nav_only:
        return HTTPStatus.OK, {
            "ok": True,
            "card_id": card_id,
            "outcome": str(btn["outcome"] or ""),
            "card": current_card,
            "client_action": {"type": action, "payload": client_payload},
        }

    server_payload = _decode_json_dict(btn["server_payload_json"])

    outcome = f"已选择「{btn['label']}」"
    follow_up_text = ""
    follow_up_card: dict[str, Any] | None = None
    client_action: dict[str, Any] | None = None
    deferred_report_goal: dict[str, Any] | None = None

    if action == "cancel_action":
        outcome = str(server_payload.get("outcome") or "已取消")
    elif action == "prefill_create_project":
        outcome = "请在输入框中补充项目名称"
        client_action = {"type": "prefill_chat", "text": str(server_payload.get("prompt") or "帮我创建一个项目叫 ")}
    elif action == "prefill_timed_goal":
        repo_id = str(server_payload.get("repo_id") or "")
        repo_name = str(server_payload.get("repo_name") or "")
        if repo_id:
            _set_recent_repo(settings, session_id, repo_id)
        outcome = f"已准备在 {repo_name} 上设定定时任务" if repo_name else "已准备设定定时任务"
        prefix = f"在 {repo_name} 上持续改进 2 小时：" if repo_name else "持续改进 2 小时："
        client_action = {"type": "prefill_chat", "text": prefix}
    elif action == "create_hosted_project":
        pname = str(server_payload.get("project_name") or "").strip()
        if not pname:
            _set_pending_state(settings, session_id, "awaiting_project_name", {})
            outcome = "等待输入项目名称"
            follow_up_text = "项目叫什么名字？（英文字母、数字、短横线）"
        else:
            follow_up_text, follow_up_card = _execute_create_project_now(settings, session_id, pname)
            outcome = f"已处理本地托管项目「{pname}」"
    elif action == "pick_project":
        repo_id = str(server_payload.get("repo_id") or "")
        raw_dur = server_payload.get("duration_minutes")
        duration_minutes = int(raw_dur) if raw_dur is not None else 60
        goal_text = str(server_payload.get("goal_text") or "")
        repo = _find_repo_by_id(_safe_list_repos(settings), repo_id)
        if repo is None:
            outcome = "找不到所选项目"
            follow_up_text = "找不到所选项目，请到「项目」页面确认。"
        else:
            _set_recent_repo(settings, session_id, repo_id)
            outcome = f"已选择项目「{repo['name']}」"
            if duration_minutes <= 0:
                follow_up_text, follow_up_card = _build_duration_picker_card(
                    settings,
                    session_id,
                    repo,
                    goal_text,
                )
            else:
                follow_up_text, follow_up_card = _build_timed_goal_confirmation_card(
                    settings,
                    session_id,
                    repo,
                    duration_minutes,
                    goal_text,
                )
    elif action == "pick_timed_goal_duration":
        repo_id = str(server_payload.get("repo_id") or "")
        duration_minutes = int(server_payload.get("duration_minutes") or 60)
        goal_text = str(server_payload.get("goal_text") or "")
        repo = _find_repo_by_id(_safe_list_repos(settings), repo_id)
        if repo is None:
            outcome = "找不到所选项目"
            follow_up_text = "找不到所选项目，请到「项目」页面确认。"
        else:
            _set_recent_repo(settings, session_id, repo_id)
            outcome = f"已选择持续 {_format_duration_zh(duration_minutes)}"
            follow_up_text, follow_up_card = _build_timed_goal_confirmation_card(
                settings,
                session_id,
                repo,
                duration_minutes,
                goal_text,
            )
    elif action == "confirm_stop_goal":
        goal_id = str(server_payload.get("goal_id") or "")
        repo_name = str(server_payload.get("repo_name") or "当前项目")
        try:
            stopped_goal = goals_mod.stop_goal(server, settings, goal_id, post_report=False)
        except svc.SupervisorServiceError as exc:
            outcome = f"停止失败：{exc.message}"
            follow_up_text = exc.message
        else:
            outcome = f"已停止「{repo_name}」的目标"
            follow_up_text = f"已停止 {repo_name} 的目标，已经做好的改动会保留。"
            deferred_report_goal = stopped_goal
    elif action == "confirm_clarified_intent":
        intent_choice = str(server_payload.get("intent_choice") or "")
        raw_text = str(server_payload.get("raw_text") or "")
        outcome = f"已确认：{intent_decider.INTENT_ACTION_LABELS.get(intent_choice, btn['label'])}"
        follow_up_text, follow_up_card = _dispatch_decider_choice(settings, session_id, intent_choice, raw_text)
        if not follow_up_text and follow_up_card is None:
            follow_up_text = "好的，你可以直接补充更多细节。"
    elif action == "reject_clarified_intent":
        raw_text = str(server_payload.get("raw_text") or "")
        outcome = "好的，按普通对话回答"
        if raw_text:
            _set_pending_state(settings, session_id, "skip_decider_once", {"text": raw_text})
            client_action = {"type": "normal_chat", "text": raw_text}
    elif action == "confirm_start_timed_goal":
        outcome, follow_up_text, follow_up_card = _start_timed_goal_from_action(
            server,
            settings,
            session_id,
            server_payload,
            enable_check_script=True,
        )
    elif action == "confirm_start_timed_goal_manual":
        outcome, follow_up_text, follow_up_card = _start_timed_goal_from_action(
            server,
            settings,
            session_id,
            server_payload,
            enable_check_script=False,
        )
    elif action == "edit_timed_goal":
        outcome = "已转到「项目 › 目标」修改"
        client_action = {"type": "edit_timed_goal", "payload": client_payload}
    elif action == "request_merge_worktree":
        worktree_id = str(server_payload.get("worktree_id") or "")
        repo_id = str(server_payload.get("repo_id") or "")
        branch_display, target_display = _resolve_merge_branches(
            settings,
            worktree_id,
            str(server_payload.get("source_branch") or ""),
            str(server_payload.get("target_branch") or ""),
        )
        outcome = f"准备把 {branch_display} 合并到 {target_display}"
        follow_up_text, follow_up_card = _build_merge_confirmation_card(
            settings,
            session_id,
            worktree_id=worktree_id,
            repo_id=repo_id,
            source_branch=branch_display,
            target_branch=target_display,
        )
    elif action == "confirm_merge_worktree":
        worktree_id = str(server_payload.get("worktree_id") or "")
        source_branch = str(server_payload.get("source_branch") or "当前分支")
        target_branch = str(server_payload.get("target_branch") or "main")
        try:
            merge_res = merge_mod.merge_worktree(settings, worktree_id, {"confirm": True})
        except svc.SupervisorServiceError as exc:
            err_msg = str(exc.message or "合并失败，请到「项目」页面检查后再试。")
            if isinstance(exc, merge_mod.SupervisorMergeError) and exc.extra.get("conflicts"):
                conflicts = "、".join(str(p) for p in exc.extra["conflicts"][:5])
                err_msg = f"{err_msg}（冲突文件：{conflicts}）"
            outcome = f"合并失败：{exc.message}"
            follow_up_text = err_msg
        else:
            merged_target = str(merge_res.get("target_branch") or target_branch)
            files_changed = int(merge_res.get("files_changed") or 0)
            outcome = f"已把 {source_branch} 合并到 {merged_target}"
            follow_up_text = (
                f"已经把 {source_branch} 合并到 {merged_target}（改了 {files_changed} 个文件）。"
                if files_changed > 0
                else f"已经把 {source_branch} 合并到 {merged_target}。"
            )
    elif action == "rerun_goal_one_hour":
        goal_id = str(server_payload.get("goal_id") or "").strip()
        rerun_payload = dict(server_payload)
        rerun_payload["duration_minutes"] = 60
        if goal_id:
            try:
                stored_goal = goals_mod._get_goal_row(settings, goal_id)
                if stored_goal.get("repo_id"):
                    rerun_payload["repo_id"] = str(stored_goal["repo_id"])
                if stored_goal.get("worktree_id"):
                    rerun_payload["worktree_id"] = str(stored_goal["worktree_id"])
                if stored_goal.get("outcome"):
                    rerun_payload["goal_text"] = str(stored_goal["outcome"])
            except Exception:
                pass
        outcome, follow_up_text, follow_up_card = _start_timed_goal_from_action(
            server,
            settings,
            session_id,
            rerun_payload,
            enable_check_script=False,
        )
    else:
        outcome = f"已执行「{btn['label']}」"

    # Update final outcome string on the card.
    conn = _connect(settings)
    try:
        conn.execute("UPDATE chat_action_cards SET outcome = ? WHERE id = ?", (outcome, card_id))
        conn.commit()
        updated_card = _row_to_card(conn, card_id)
    finally:
        conn.close()

    follow_up_msg = None
    if follow_up_text:
        follow_up_msg = _persist_follow_up_message(settings, session_id, follow_up_text, follow_up_card)
    if deferred_report_goal is not None:
        goals_mod.post_finish_report(settings, deferred_report_goal)

    resp: dict[str, Any] = {
        "ok": True,
        "card_id": card_id,
        "outcome": outcome,
        "card": updated_card,
    }
    if follow_up_msg is not None:
        resp["follow_up_message"] = follow_up_msg
    if client_action is not None:
        resp["client_action"] = client_action
    return HTTPStatus.OK, resp
