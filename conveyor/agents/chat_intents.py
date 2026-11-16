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

import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

from conveyor.agents import agent_supervisor as ags
from conveyor.agents import supervisor_goals as goals_mod
from conveyor.agents import supervisor_service as svc
from conveyor.core.transcript_store import get_transcript_store

logger = logging.getLogger(__name__)

# The parallel timed-goal backend task adds duration_minutes (30..1440) and
# mode ('outcome' | 'continuous'). Unknown keys are accepted safely either way.
GOAL_TIMEBOX_SUPPORTED = True

PENDING_TTL_SECONDS = 10 * 60
MIN_DURATION_MINUTES = 30
MAX_DURATION_MINUTES = 1440
DEFAULT_TIMED_GOAL_OUTCOME = "按你的项目笔记找最值得做的改进"

_DB_INIT_LOCK = threading.Lock()
_INITIALIZED_DBS: set[str] = set()

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
    key = str(path.resolve())
    conn = sqlite3.connect(str(path), timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    if key not in _INITIALIZED_DBS:
        with _DB_INIT_LOCK:
            if key not in _INITIALIZED_DBS:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(_SCHEMA_SQL)
                conn.commit()
                _INITIALIZED_DBS.add(key)
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
) -> dict[str, Any]:
    """Persist an interactive action card and return its client-safe representation."""
    card_id = f"card_{os.urandom(8).hex()}"
    now = time.time()
    client_buttons: list[dict[str, Any]] = []

    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO chat_action_cards (id, session_id, title, text, used, outcome, created_at)
               VALUES (?, ?, ?, ?, 0, '', ?)""",
            (card_id, session_id, title, text, now),
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

    return {
        "id": card_id,
        "title": title,
        "text": text,
        "buttons": client_buttons,
        "used": False,
        "outcome": "",
    }


def _row_to_card(conn: sqlite3.Connection, card_id: str) -> dict[str, Any] | None:
    card_row = conn.execute(
        "SELECT id, title, text, used, outcome FROM chat_action_cards WHERE id = ?",
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
    return {
        "id": str(card_row["id"]),
        "title": str(card_row["title"]),
        "text": str(card_row["text"]),
        "buttons": buttons,
        "used": bool(card_row["used"]),
        "outcome": str(card_row["outcome"] or ""),
    }


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
    text = (
        f"准备在 {repo_name} 上持续改进 {duration_label}：{goal_display}。"
        f"{roles_summary}。时间到或者模型额度用完会自动停。"
    )
    card = create_action_card(
        settings,
        session_id=session_id,
        title="确认开始持续改进",
        text=text,
        buttons=[
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
        ],
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


def _format_deadline_hhmm(deadline_at: Any) -> str | None:
    if deadline_at is None or isinstance(deadline_at, bool):
        return None
    if isinstance(deadline_at, (int, float)):
        try:
            return datetime.fromtimestamp(float(deadline_at)).astimezone().strftime("%H:%M")
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
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%H:%M")
    return None


def _start_timed_goal_from_action(
    server: Any,
    settings: Any,
    session_id: str,
    payload: dict[str, Any],
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
        worktree = _ensure_ready_worktree(settings, repo)
        body: dict[str, Any] = {
            "repo_id": str(repo["id"]),
            "worktree_id": str(worktree["id"]),
            "title": _short_goal_title(goal_text),
            "outcome": goal_text,
            "role_overrides": role_overrides,
        }
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
    end_hhmm = _format_deadline_hhmm(created_goal.get("deadline_at"))
    if end_hhmm:
        reply_text = f"已经开始了，预计 {end_hhmm} 结束。随时在 项目 › 目标 里看进度或停止。"
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


def has_pending_or_detected_intent(settings: Any, session_id: str, text: str) -> bool:
    """Return True if web chat can handle this message without requiring a chat model."""
    try:
        if _has_active_pending(settings, session_id) is not None:
            return True
    except Exception:
        logger.exception("chat intent state unavailable")
    return detect_intent(text) is not None


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
        if not raw:
            return False

        # 1. Check active pending follow-up state (e.g. awaiting project name).
        pending = _has_active_pending(settings, session_id)
        if pending is not None and pending["pending_kind"] == "awaiting_project_name":
            _clear_pending_state(settings, session_id)
            if raw in {"取消", "算了", "不用了", "不建了", "cancel"}:
                _emit_plain_reply(port, msg, "好的，已取消创建项目。")
                return True
            candidate = raw.strip().strip("`\"'「『」』")
            reply_text, card = _execute_create_project_now(settings, session_id, candidate)
            if card is not None:
                _emit_card_reply(port, msg, reply_text, card)
            else:
                _emit_plain_reply(port, msg, reply_text)
            return True

        # 2. Deterministic pattern first; optional chat-tier LLM fallback on ambiguous inputs.
        intent = await classify_intent(raw, settings, model=model)
        if intent is None:
            return False

        if intent.kind == "create_project":
            reply_text, card = _handle_create_project_intent(settings, session_id, intent)
        elif intent.kind == "start_timed_goal":
            reply_text, card = _handle_start_timed_goal_intent(settings, session_id, intent, raw)
        else:
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
        duration_minutes = int(server_payload.get("duration_minutes") or 60)
        goal_text = str(server_payload.get("goal_text") or "")
        repo = _find_repo_by_id(_safe_list_repos(settings), repo_id)
        if repo is None:
            outcome = "找不到所选项目"
            follow_up_text = "找不到所选项目，请到「项目」页面确认。"
        else:
            _set_recent_repo(settings, session_id, repo_id)
            outcome = f"已选择项目「{repo['name']}」"
            follow_up_text, follow_up_card = _build_timed_goal_confirmation_card(
                settings,
                session_id,
                repo,
                duration_minutes,
                goal_text,
            )
    elif action == "confirm_start_timed_goal":
        outcome, follow_up_text, follow_up_card = _start_timed_goal_from_action(
            server,
            settings,
            session_id,
            server_payload,
        )
    elif action == "edit_timed_goal":
        outcome = "已转到「项目 › 目标」修改"
        client_action = {"type": "edit_timed_goal", "payload": client_payload}
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
