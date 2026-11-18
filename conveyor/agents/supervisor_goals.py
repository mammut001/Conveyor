"""supervisor_goals.py — 目标驱动的闭环（goal-driven loop）for the Agent Supervisor.

A *goal* sits above supervisor tasks: the user describes the outcome they want,
and the goal loop keeps running task after task on the same managed worktree
until the outcome is reached, the iteration budget runs out, the user stops it,
or a decision is needed from the user.

Design contract (see docs/agent-supervisor.md 「目标闭环（goals）」):
- Goals and their iterations live in their own SQLite tables in a sibling DB
  file under the same supervisor state root (`supervisor_goals.sqlite3`),
  following the `agent_supervisor._connect` / schema-init pattern.
- Every task is created and dispatched through the existing service functions
  (`supervisor_service.create_scoped_task` / `dispatch_scoped_task`), so all
  scope, role-override and worktree-lock validation is reused, never bypassed.
- The evaluator (coordinator chat model) only *proposes* a decision; the hard
  rules in this module are enforced in code and never trusted from the model.
- When a goal reaches `waiting_user` / `achieved` / `exhausted` / `failed` the owner is
  told on the channels Conveyor already has (`goal_notify.notify_goal_change`,
  non-blocking and deduplicated per transition). `stopped` is silent: the owner
  stopped the goal themselves.
- The owner answers from the phone: `/goals`, `/goal_reply`, `/goal_done` and
  `/goal_stop` (`handlers/goal_commands.py`) resolve a short goal id
  (`short_goal_id` / `resolve_goal_id`) and call `reply_goal` / `stop_goal` with
  **no** server handle, so those two wake the goal loop through the module-level
  wake hook `SupervisorWorkerManager` registers when it starts.
- All user-facing strings (question, summary, errors) are Simplified Chinese.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
import math
from pathlib import Path
from typing import Any, Callable

from conveyor.agents import agent_supervisor as ags
from conveyor.agents import agent_supervisor_pool as worker_pool
from conveyor.agents import supervisor_diff as diff_mod
from conveyor.agents import supervisor_service as svc
from conveyor.agents import workers
from conveyor.agents import worker_usage
from conveyor.agents.goal_notify import NOTIFY_STATES as GOAL_NOTIFY_STATES
from conveyor.agents.goal_notify import goal_status_label, notify_goal_change, notify_goal_digest
from conveyor.core.redaction import redact_text, truncate

log = logging.getLogger("conveyor.supervisor_goals")

GOAL_ID_PREFIX = "goal-"

GOAL_STATES = ("running", "waiting_user", "waiting_quota", "achieved", "stopped", "exhausted", "failed")
GOAL_RUNNING = "running"
GOAL_WAITING_USER = "waiting_user"
GOAL_WAITING_QUOTA = "waiting_quota"
GOAL_ACHIEVED = "achieved"
GOAL_STOPPED = "stopped"
GOAL_EXHAUSTED = "exhausted"
GOAL_FAILED = "failed"
GOAL_TERMINAL_STATES = (GOAL_ACHIEVED, GOAL_STOPPED, GOAL_EXHAUSTED, GOAL_FAILED)
MAX_FINISH_REPORT_BULLETS = 8

GOAL_MODE_OUTCOME = "outcome"
GOAL_MODE_CONTINUOUS = "continuous"
GOAL_MODES = (GOAL_MODE_OUTCOME, GOAL_MODE_CONTINUOUS)

STOP_REASON_DEADLINE = "deadline"
STOP_REASON_QUOTA = "quota"

DECISION_CONTINUE = "continue"
DECISION_ACHIEVED = "achieved"
DECISION_ASK_USER = "ask_user"
DECISION_GIVE_UP = "give_up"
DECISIONS = (DECISION_CONTINUE, DECISION_ACHIEVED, DECISION_ASK_USER, DECISION_GIVE_UP)

# 目标标题：取结果描述的第一行，超过 40 个字就截断并加「…」。
# 绝不用 `truncate()` 的 `...[truncated]` 标记——老板只看中文，看不懂那种标记。
MAX_TITLE_CHARS = 40
TITLE_ELLIPSIS = "…"
MAX_OUTCOME_CHARS = 4000
MAX_QUESTION_CHARS = 1000
MAX_SUMMARY_CHARS = 1200
MAX_REASON_CHARS = 500
MAX_NEXT_PROMPT_CHARS = 6000
LATE_ROUND_FRACTION = 0.5
# Phrase in the summary of a goal stopped because the next round would not fit.
LATE_ROUND_MARKER = "不够再跑一轮"
MAX_EVAL_EXCERPT_CHARS = 3000
# Every previous iteration is included (max_iterations is clamped to 20 below).
EVAL_STALE_SECONDS = 1800
DEFAULT_MAX_ITERATIONS = 5
MIN_MAX_ITERATIONS = 1
MAX_MAX_ITERATIONS = 20
MAX_EVAL_ITERATIONS = MAX_MAX_ITERATIONS
MAX_TIMEBOXED_ITERATIONS = 200
MIN_DURATION_MINUTES = 30
MAX_DURATION_MINUTES = 1440
DEFAULT_QUOTA_PAUSE_MINUTES = 30
CONTINUOUS_CONSECUTIVE_FAILURE_LIMIT = 3
MAX_PASSED_ROUND_TITLES_IN_PROMPT = 30
DIGEST_EVERY_ROUNDS = 3
DIGEST_INTERVAL_SECONDS = 3600
DIGEST_RECENT_TITLES = 3
MIN_EXTEND_ROUNDS = 1
MAX_EXTEND_ROUNDS = 10
MAX_EXTEND_MESSAGE_CHARS = 2000
# 手机上打不出 16 位 hex：`/goals` 和通知里只显示 `goal-` 后面的前 6 位。
SHORT_ID_CHARS = 6
MAX_LESSON_ITEMS = 3
MAX_LESSON_CHARS = 200

FIRST_ITERATION_NOTE = "这是目标的第 1 轮。"
CHECK_SCRIPT_PROMPT_NOTE = (
    "自动检查要求（scripts/check.sh）：创建或维护 `scripts/check.sh`（POSIX sh，`set -e`），"
    "在里面运行这个项目真正的测试/构建；代码有问题时必须失败。"
    "禁止敷衍脚本（禁止只写 `exit 0`、`true`、`echo ok`）。"
    "如果项目还没有测试，请为本轮改动补一个最小真实测试，并在 `scripts/check.sh` 中运行它。\n"
    "Validation requirement: Create or maintain `scripts/check.sh` (POSIX `sh`, `set -e`) "
    "to run this project's real tests/build; it must fail when the code is broken. "
    "Never make it a no-op (`exit 0`, `true`, `echo ok`). "
    "If the project has no tests yet, add a minimal real test for this round's changes and run it from `scripts/check.sh`."
)

# Hard-coded Chinese operator question for the zero-validation-commands case:
# the supervisor never fabricates completion, so the human must judge.
NEEDS_VALIDATION_QUESTION = (
    "这一轮的改动已经做完，但没有配置自动验证命令，请检查结果后告诉我是否达成或还要改什么"
)
EVALUATOR_UNAVAILABLE_QUESTION = (
    "评估器这一轮无法给出明确判断（模型不可用或返回内容无法解析），"
    "请你人工确认这一轮的结果是否达成目标，或者告诉我下一步要改什么。"
)
NO_NEXT_PROMPT_QUESTION = (
    "评估器建议继续迭代，但没有给出明确的下一步指令。"
    "请补充这一轮要做的具体事情，我再继续跑。"
)
CONTINUOUS_FAILURES_QUESTION = (
    "已经连续 3 轮没能通过检查了，可能需求方向或环境有问题。"
    "请看看最新的失败原因，告诉我接下来怎么改，或者先停在这里。"
)
QUOTA_EXCEEDED_DEADLINE_SUMMARY = "模型额度用完了，下次可用时间已经超过截止时间，先停在这里。"

_SCHEMA_INIT_LOCK = threading.Lock()
_INITIALIZED_DBS: set[tuple[str, int, int]] = set()
_REPLY_LOCKS_GUARD = threading.Lock()
_REPLY_LOCKS: dict[str, threading.Lock] = {}
_GOAL_INFLIGHT_STATES = svc._INFLIGHT_STATES | frozenset({"pending_dispatch"})
_GOAL_ACTIVE_TASK_STATES = svc._ACTIVE_WORKTREE_STATES | frozenset({"pending_dispatch"})

# 唤醒钩子：跑 `advance_goals` 的 worker 挂在 Web 服务器的
# `SupervisorWorkerManager` 事件循环上，而手机上的一条 `/goal_reply` 是在 bot 进程里
# 发生的，手里根本没有 server 句柄。Manager 启动时把自己注册进来，这样回复之后
# 目标闭环立刻被叫醒，不用等下一个 tick、也不依赖 web 服务是否起着。
_WAKE_HOOKS_GUARD = threading.Lock()
_WAKE_HOOKS: list[Callable[[Any], None]] = []


def _goal_reply_lock(goal_id: str) -> threading.Lock:
    with _REPLY_LOCKS_GUARD:
        lock = _REPLY_LOCKS.get(goal_id)
        if lock is None:
            lock = threading.Lock()
            _REPLY_LOCKS[goal_id] = lock
        return lock

_GOALS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS supervisor_goals (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    worktree_id TEXT NOT NULL,
    title TEXT NOT NULL,
    outcome TEXT NOT NULL,
    max_iterations INTEGER NOT NULL DEFAULT 5,
    role_overrides_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    stop_reason TEXT NOT NULL DEFAULT '',
    last_next_prompt TEXT NOT NULL DEFAULT '',
    deadline_at TEXT,
    mode TEXT NOT NULL DEFAULT 'outcome',
    resume_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    origin_session_id TEXT,
    last_digest_at TEXT,
    last_digest_rounds INTEGER
);

CREATE TABLE IF NOT EXISTS supervisor_goal_iterations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id TEXT NOT NULL,
    n INTEGER NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT '',
    decision_reason TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    task_final_state TEXT NOT NULL DEFAULT '',
    evaluating INTEGER NOT NULL DEFAULT 0,
    evaluating_since TEXT NOT NULL DEFAULT '',
    passed INTEGER NOT NULL DEFAULT 0,
    round_title TEXT NOT NULL DEFAULT '',
    UNIQUE(goal_id, n)
);
CREATE INDEX IF NOT EXISTS idx_sup_goals_state ON supervisor_goals(state);
CREATE INDEX IF NOT EXISTS idx_sup_goal_iters_goal ON supervisor_goal_iterations(goal_id, n);

-- goal_notify.py: which (goal, state, updated_at) transitions the owner was already
-- told about. Survives restarts, so a second tick never repeats a notification.
CREATE TABLE IF NOT EXISTS supervisor_goal_notifications (
    goal_id TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (goal_id, state, updated_at)
);

-- Finish report idempotency: at most one web-chat finish report per goal.
CREATE TABLE IF NOT EXISTS supervisor_goal_reports (
    goal_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT '',
    card_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


_GOAL_SCHEMA_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("supervisor_goals", "deadline_at", "TEXT"),
    ("supervisor_goals", "mode", "TEXT NOT NULL DEFAULT 'outcome'"),
    ("supervisor_goals", "resume_at", "TEXT"),
    ("supervisor_goals", "consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
    ("supervisor_goals", "origin_session_id", "TEXT"),
    ("supervisor_goals", "last_digest_at", "TEXT"),
    ("supervisor_goals", "last_digest_rounds", "INTEGER"),
    ("supervisor_goal_iterations", "passed", "INTEGER NOT NULL DEFAULT 0"),
    ("supervisor_goal_iterations", "round_title", "TEXT NOT NULL DEFAULT ''"),
)


def _now() -> datetime:
    """Return current UTC datetime. Tests patch this function for deterministic clocks."""
    return datetime.now(timezone.utc)


def _format_utc(dt: datetime) -> str:
    utc_dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return utc_dt.isoformat(timespec="seconds")


def _parse_utc(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _utc_now() -> str:
    return _format_utc(_now())


def _deadline_passed(goal: dict[str, Any], *, at: datetime | None = None) -> bool:
    deadline_dt = _parse_utc(goal.get("deadline_at"))
    if deadline_dt is None:
        return False
    now_dt = at or _now()
    return now_dt >= deadline_dt


def _service_error(status: int | HTTPStatus, message: str, *, code: str | None = None) -> svc.SupervisorServiceError:
    """HTTP-mappable error in the exact same JSON shape as every other supervisor route."""
    return svc.SupervisorServiceError(status, message, code=code)


# ---------------------------------------------------------------------------
# SQLite persistence (sibling DB under the same supervisor state root)
# ---------------------------------------------------------------------------

def _db_path(settings: Any) -> Path:
    mem_root = getattr(settings, "codex_memory_root", "") or ""
    if not mem_root:
        raise _service_error(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "supervisor storage root is not configured",
        )
    state_dir = Path(os.path.realpath(os.path.expanduser(str(mem_root)))) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "supervisor_goals.sqlite3"


def _migrate_schema(conn: sqlite3.Connection) -> None:
    table_cols: dict[str, set[str]] = {}
    for table, column, column_def in _GOAL_SCHEMA_MIGRATIONS:
        cols = table_cols.get(table)
        if cols is None:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            cols = {str(r["name"]) for r in rows}
            table_cols[table] = cols
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")
            cols.add(column)


def _connect(settings: Any) -> sqlite3.Connection:
    path = _db_path(settings)
    key = str(path)
    st_before = path.stat() if path.exists() else None
    key_before = (key, st_before.st_dev, st_before.st_ino) if st_before is not None else None
    with _SCHEMA_INIT_LOCK:
        need_init = key_before is None or key_before not in _INITIALIZED_DBS
    conn = sqlite3.connect(key, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    if need_init:
        with _SCHEMA_INIT_LOCK:
            st = path.stat()
            cur_key = (key, st.st_dev, st.st_ino)
            if cur_key not in _INITIALIZED_DBS:
                conn.executescript(_GOALS_SCHEMA_SQL)
                _migrate_schema(conn)
                _INITIALIZED_DBS.add(cur_key)
    return conn


# ---------------------------------------------------------------------------
# Row shaping / validation helpers
# ---------------------------------------------------------------------------

def _decode_json_object(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _goal_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    raw_deadline = row["deadline_at"] if "deadline_at" in keys else None
    raw_mode = row["mode"] if "mode" in keys else GOAL_MODE_OUTCOME
    raw_resume = row["resume_at"] if "resume_at" in keys else None
    raw_consec = row["consecutive_failures"] if "consecutive_failures" in keys else 0
    raw_origin = row["origin_session_id"] if "origin_session_id" in keys else None
    raw_last_digest_at = row["last_digest_at"] if "last_digest_at" in keys else None
    raw_last_digest_rounds = row["last_digest_rounds"] if "last_digest_rounds" in keys else None
    return {
        "id": str(row["id"]),
        "repo_id": str(row["repo_id"]),
        "worktree_id": str(row["worktree_id"]),
        "title": str(row["title"]),
        "outcome": str(row["outcome"]),
        "max_iterations": int(row["max_iterations"]),
        "role_overrides": _decode_json_object(row["role_overrides_json"]),
        "state": str(row["state"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "question": str(row["question"] or ""),
        "summary": str(row["summary"] or ""),
        "stop_reason": str(row["stop_reason"] or ""),
        "last_next_prompt": str(row["last_next_prompt"] or ""),
        "deadline_at": str(raw_deadline) if raw_deadline else None,
        "mode": str(raw_mode or GOAL_MODE_OUTCOME),
        "resume_at": str(raw_resume) if raw_resume else None,
        "consecutive_failures": int(raw_consec or 0),
        "origin_session_id": str(raw_origin).strip() if raw_origin else None,
        "last_digest_at": str(raw_last_digest_at) if raw_last_digest_at else None,
        "last_digest_rounds": int(raw_last_digest_rounds) if raw_last_digest_rounds is not None else None,
    }


def _iteration_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    raw_passed = row["passed"] if "passed" in keys else 0
    raw_title = row["round_title"] if "round_title" in keys else ""
    return {
        "n": int(row["n"]),
        "task_id": str(row["task_id"] or ""),
        "prompt": str(row["prompt"] or ""),
        "decision": str(row["decision"] or ""),
        "decision_reason": str(row["decision_reason"] or ""),
        "started_at": str(row["started_at"] or ""),
        "finished_at": str(row["finished_at"] or ""),
        "task_final_state": str(row["task_final_state"] or ""),
        "passed": bool(raw_passed),
        "round_title": str(raw_title or ""),
    }


def _iteration_ran(iteration: dict[str, Any]) -> bool:
    return bool(
        iteration.get("task_id")
        or iteration.get("decision")
        or iteration.get("task_final_state")
    )


def _iteration_completed(iteration: dict[str, Any]) -> bool:
    return bool(
        iteration.get("finished_at")
        or iteration.get("decision")
        or iteration.get("task_final_state")
    )


def digests_enabled(settings: Any) -> bool:
    """Off switch: ``CONVEYOR_GOAL_DIGESTS=false`` (`Settings.goal_digests_enabled`)."""
    return bool(getattr(settings, "goal_digests_enabled", True))


def _compute_remaining_seconds(deadline_at: str | None) -> int | None:
    deadline_dt = _parse_utc(deadline_at)
    if deadline_dt is None:
        return None
    delta = (deadline_dt - _now()).total_seconds()
    if delta <= 0:
        return 0
    return math.ceil(delta)


def _goal_usage(settings: Any, iterations: list[dict[str, Any]]) -> dict[str, int]:
    """Goal total model usage: sum over the tasks of its rounds.

    Rounds that never produced a task contribute nothing, and tasks whose runs
    reported no usage stay at zero (the per-task counts carry that signal).
    ``runs_without_usage`` and ``runs_before_recording`` are summed too, so the
    goal-level copy can tell "an executor stayed silent" from "that round
    predates usage recording".
    """
    task_ids = [str(it.get("task_id") or "") for it in iterations if it.get("task_id")]
    usage_by_task = ags.tasks_usage(settings, task_ids)
    total = ags.empty_usage()
    for entry in usage_by_task.values():
        for key in total:
            total[key] += int(entry.get(key) or 0)
    return total


def _enrich_goal_dict(goal: dict[str, Any], iterations: list[dict[str, Any]]) -> dict[str, Any]:
    rounds_total = sum(1 for it in iterations if _iteration_ran(it))
    rounds_passed = sum(
        1 for it in iterations
        if bool(it.get("passed")) or it.get("decision") == DECISION_ACHIEVED
    )
    enriched = dict(goal)
    enriched["rounds_passed"] = rounds_passed
    enriched["rounds_total"] = rounds_total
    enriched["remaining_seconds"] = _compute_remaining_seconds(enriched.get("deadline_at"))
    return enriched


def _clamp_duration_minutes(raw: Any) -> int | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        raise _service_error(
            HTTPStatus.BAD_REQUEST,
            f"持续时间必须是 {MIN_DURATION_MINUTES} 到 {MAX_DURATION_MINUTES} 之间的整数（分钟）",
            code="invalid_duration",
        )
    if isinstance(raw, float) and not raw.is_integer():
        raise _service_error(
            HTTPStatus.BAD_REQUEST,
            f"持续时间必须是 {MIN_DURATION_MINUTES} 到 {MAX_DURATION_MINUTES} 之间的整数（分钟）",
            code="invalid_duration",
        )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _service_error(
            HTTPStatus.BAD_REQUEST,
            f"持续时间必须是 {MIN_DURATION_MINUTES} 到 {MAX_DURATION_MINUTES} 之间的整数（分钟）",
            code="invalid_duration",
        )
    if value < MIN_DURATION_MINUTES or value > MAX_DURATION_MINUTES:
        raise _service_error(
            HTTPStatus.BAD_REQUEST,
            f"持续时间必须在 {MIN_DURATION_MINUTES} 到 {MAX_DURATION_MINUTES} 分钟之间（最长 24 小时）",
            code="invalid_duration",
        )
    return value


def _clean_mode(raw: Any) -> str:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return GOAL_MODE_OUTCOME
    mode = str(raw).strip().lower()
    if mode not in GOAL_MODES:
        raise _service_error(
            HTTPStatus.BAD_REQUEST,
            "目标运行模式无效，只支持达标即停或持续改进",
            code="invalid_mode",
        )
    return mode


def _clamp_max_iterations(raw: Any) -> int:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_MAX_ITERATIONS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _service_error(
            HTTPStatus.BAD_REQUEST,
            f"max_iterations must be an integer between {MIN_MAX_ITERATIONS} and {MAX_MAX_ITERATIONS}",
            code="invalid_max_iterations",
        )
    return max(MIN_MAX_ITERATIONS, min(MAX_MAX_ITERATIONS, value))


def _clean_outcome(raw: Any) -> str:
    if not isinstance(raw, str):
        raise _service_error(HTTPStatus.BAD_REQUEST, "outcome is required", code="invalid_outcome")
    outcome = raw.strip()
    if not outcome:
        raise _service_error(HTTPStatus.BAD_REQUEST, "outcome is required", code="invalid_outcome")
    if len(outcome) > MAX_OUTCOME_CHARS:
        raise _service_error(
            HTTPStatus.BAD_REQUEST,
            f"outcome must be 1-{MAX_OUTCOME_CHARS} characters",
            code="invalid_outcome",
        )
    if any(ch in outcome for ch in ("\x00",)):
        raise _service_error(HTTPStatus.BAD_REQUEST, "outcome contains invalid characters", code="invalid_outcome")
    return outcome


def _display_title(text: str) -> str:
    """One-line title: whitespace collapsed, cut at `MAX_TITLE_CHARS` with 「…」.

    Never the `...[truncated]` marker from `truncate()` (an 81-character single-line
    outcome used to collapse into nothing but that marker).
    """
    collapsed = " ".join(redact_text(str(text or "")).split())
    if len(collapsed) > MAX_TITLE_CHARS:
        return collapsed[:MAX_TITLE_CHARS] + TITLE_ELLIPSIS
    return collapsed


def _title_from_outcome(outcome: str) -> str:
    """Title = first non-empty line of the outcome (whitespace collapsed)."""
    raw = str(outcome or "")
    for line in raw.splitlines():
        title = _display_title(line)
        if title:
            return title
    return _display_title(raw)


def _clean_title(raw: Any, outcome: str) -> str:
    if raw is None:
        return _title_from_outcome(outcome)
    if not isinstance(raw, str):
        raise _service_error(HTTPStatus.BAD_REQUEST, "title must be a string", code="invalid_title")
    cleaned = raw.strip()
    if not cleaned:
        return _title_from_outcome(outcome)
    return _display_title(cleaned)


def _get_goal_row(settings: Any, goal_id: Any) -> dict[str, Any]:
    if not isinstance(goal_id, str) or not goal_id.strip():
        raise _service_error(HTTPStatus.BAD_REQUEST, "goal_id is required", code="invalid_goal_id")
    gid = goal_id.strip()
    conn = _connect(settings)
    try:
        row = conn.execute("SELECT * FROM supervisor_goals WHERE id = ?", (gid,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise _service_error(HTTPStatus.NOT_FOUND, f"目标不存在: {gid}", code="goal_not_found")
    return _goal_row_to_dict(row)


def _iterations(settings: Any, goal_id: str) -> list[dict[str, Any]]:
    conn = _connect(settings)
    try:
        rows = conn.execute(
            "SELECT * FROM supervisor_goal_iterations WHERE goal_id = ? ORDER BY n ASC",
            (goal_id,),
        ).fetchall()
    finally:
        conn.close()
    return [_iteration_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Short goal ids (what the owner types on a phone)
# ---------------------------------------------------------------------------

def short_goal_id(goal_id: Any) -> str:
    """The phone-friendly id: the first `SHORT_ID_CHARS` characters after ``goal-``.

    A full id keeps working everywhere a short one is accepted, so a paste from
    the web console is never rejected.
    """
    raw = str(goal_id or "").strip()
    if raw.startswith(GOAL_ID_PREFIX):
        raw = raw[len(GOAL_ID_PREFIX):]
    return raw[:SHORT_ID_CHARS]


def resolve_goal_id(settings: Any, token: Any) -> str:
    """Resolve what the owner typed (short id or full id) to exactly one goal id.

    Raises a Simplified-Chinese `SupervisorServiceError` when nothing matches or
    when the short id is ambiguous — acting on the wrong goal would be worse than
    asking the owner to be more precise.
    """
    raw = str(token or "").strip()
    if not raw:
        raise _service_error(
            HTTPStatus.BAD_REQUEST,
            "请给出目标短编号（/goals 里可以看到）",
            code="invalid_goal_ref",
        )
    svc.require_enabled(settings)
    conn = _connect(settings)
    try:
        rows = conn.execute(
            """SELECT id FROM supervisor_goals
               WHERE id = ? OR lower(substr(id, ?, ?)) = lower(?)
               ORDER BY created_at DESC, id DESC""",
            (raw, len(GOAL_ID_PREFIX) + 1, SHORT_ID_CHARS, raw),
        ).fetchall()
    finally:
        conn.close()
    matches = [str(row["id"]) for row in rows]
    if not matches:
        raise _service_error(
            HTTPStatus.NOT_FOUND,
            f"找不到目标：{raw}。用 /goals 查看短编号。",
            code="goal_not_found",
        )
    if len(matches) > 1:
        raise _service_error(
            HTTPStatus.CONFLICT,
            f"短编号 {raw} 对应 {len(matches)} 个目标，请用完整目标 ID（用 /goals 查看）。",
            code="goal_ref_ambiguous",
        )
    return matches[0]


# ---------------------------------------------------------------------------
# Waking the goal loop without a web server handle
# ---------------------------------------------------------------------------

def set_goal_wake_hook(hook: Callable[[Any], None]) -> Callable[[], None] | None:
    """Register `hook(settings)`, called whenever a goal is answered without a server.

    Returns a callable that unregisters the hook again (used on worker shutdown).
    """
    if not callable(hook):
        return None
    with _WAKE_HOOKS_GUARD:
        if hook not in _WAKE_HOOKS:
            _WAKE_HOOKS.append(hook)

    def _unregister() -> None:
        with _WAKE_HOOKS_GUARD:
            if hook in _WAKE_HOOKS:
                _WAKE_HOOKS.remove(hook)

    return _unregister


def wake_hooks() -> tuple[Callable[[Any], None], ...]:
    with _WAKE_HOOKS_GUARD:
        return tuple(_WAKE_HOOKS)


def wake_goal_loop(settings: Any) -> None:
    """Wake whatever runs `advance_goals` (best effort, never raises)."""
    for hook in wake_hooks():
        try:
            hook(settings)
        except Exception:
            log.debug("goal wake hook failed", exc_info=True)


def _wake_goal_worker(server: Any, settings: Any) -> None:
    """Wake the goal loop after the owner acted on a goal.

    With a server handle the web server's own `SupervisorWorkerManager` is woken
    (and started when it is not running yet). Without one — a Telegram
    `/goal_reply` runs in the bot process, which owns no server — the registered
    wake hook is used instead, so the next iteration starts immediately.
    """
    if server is not None:
        svc.ensure_server_worker(server, settings, wake=True)
    wake_goal_loop(settings)


def _scope_paths(settings: Any, goal: dict[str, Any]) -> tuple[str, str, bool]:
    """Resolve the goal's `(repo_path, worktree_path, managed)` from the web registry."""
    conn = svc._connect(settings)
    try:
        repo = conn.execute(
            "SELECT canonical_path FROM supervisor_repositories WHERE id = ?",
            (goal["repo_id"],),
        ).fetchone()
        wt = conn.execute(
            "SELECT canonical_path, managed FROM supervisor_worktrees WHERE id = ?",
            (goal["worktree_id"],),
        ).fetchone()
    finally:
        conn.close()
    if repo is None or wt is None:
        raise _service_error(
            HTTPStatus.NOT_FOUND,
            "目标的仓库或 worktree 已不存在，无法继续执行",
            code="goal_scope_missing",
        )
    return str(repo["canonical_path"]), str(wt["canonical_path"]), bool(wt["managed"])


def _update_goal(settings: Any, goal_id: str, **fields: Any) -> dict[str, Any]:
    if not fields:
        return _get_goal_row(settings, goal_id)
    allowed = {
        "state",
        "question",
        "summary",
        "stop_reason",
        "last_next_prompt",
        "updated_at",
        "max_iterations",
        "deadline_at",
        "mode",
        "resume_at",
        "consecutive_failures",
        "last_digest_at",
        "last_digest_rounds",
    }
    assignments = []
    values: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            raise KeyError(f"unsupported goal field: {key}")
        assignments.append(f"{key} = ?")
        values.append(value)
    values.append(goal_id)
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"UPDATE supervisor_goals SET {', '.join(assignments)} WHERE id = ?",
            values,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return _get_goal_row(settings, goal_id)


# ---------------------------------------------------------------------------
# Executor preflight (saved 测试 result only; never a live check here)
# ---------------------------------------------------------------------------

EXECUTOR_CHECK_REASON = "执行者上次测试没通过：{message}。请先到 执行者 页面点「测试」，通过后再开始。"


def _goal_roles(settings: Any, role_overrides: dict[str, Any]) -> dict[str, Any]:
    """Roles of this goal: the server defaults with the operator overrides applied.

    Same merge rule `ags.queue_task` uses, so what we check here is what runs.
    """
    roles = ags.load_roles(settings)
    for role_name, spec in (role_overrides or {}).items():
        base = roles.get(role_name)
        if base is None:
            continue
        if isinstance(spec, str):
            exec_name = spec.strip()
            model = None
        elif isinstance(spec, dict):
            exec_name = str(spec.get("executor") or base.executor).strip()
            model = spec.get("model", base.model if exec_name.lower() == base.executor.lower() else None)
        else:
            continue
        if not exec_name:
            continue
        roles[role_name] = ags.RoleConfig(
            name=role_name,
            executor=exec_name,
            model=model,
            tools=list(base.tools),
            location=base.location,
        )
    return roles


def _pool_first_pick_is_pi(settings: Any, role: Any) -> bool:
    """True when the pool would hand this role to Pi first."""
    try:
        from conveyor.agents.agent_supervisor_pool import PoolExecutor

        candidates = PoolExecutor(settings).candidates(role)
    except Exception:
        log.debug("pool first pick lookup failed", exc_info=True)
        return False
    return bool(candidates) and str(candidates[0].get("id") or "") == "pi"


def _goal_uses_pi(settings: Any, role_overrides: dict[str, Any]) -> bool:
    try:
        roles = _goal_roles(settings, role_overrides)
    except Exception:
        log.debug("goal role lookup failed", exc_info=True)
        return False
    for role in roles.values():
        name = str(getattr(role, "executor", "") or "").strip().lower()
        if name == "pi":
            return True
        if name == "pool" and _pool_first_pick_is_pi(settings, role):
            return True
    return False


def _require_executor_check(settings: Any, role_overrides: dict[str, Any]) -> None:
    """Refuse to start a goal whose Pi executor last failed its 测试.

    Only the SAVED result is read: a live check would block the owner's click
    for up to a minute. No saved result means nothing is known yet → allow.
    """
    from conveyor.agents import workers as workers_mod

    try:
        state = workers_mod.load_workers_state(settings)
    except Exception:
        log.debug("workers state lookup failed", exc_info=True)
        return
    last = (state.get("last_check") or {}).get("pi")
    if not isinstance(last, dict) or last.get("ok") is not False:
        return
    if not _goal_uses_pi(settings, role_overrides):
        return
    raise _service_error(
        HTTPStatus.CONFLICT,
        EXECUTOR_CHECK_REASON.format(
            message=str(last.get("message") or "").strip().rstrip("。")
        ),
        code="executor_check_failed",
    )


# ---------------------------------------------------------------------------
# Public API: create / list / get / stop / reply
# ---------------------------------------------------------------------------

def create_goal(server: Any, settings: Any, body: dict[str, Any]) -> dict[str, Any]:
    """Create a `running` goal. The first iteration/task is dispatched by `advance_goals`."""
    svc.require_enabled(settings)
    if not isinstance(body, dict):
        raise _service_error(HTTPStatus.BAD_REQUEST, "request body must be an object", code="invalid_body")

    outcome = _clean_outcome(body.get("outcome"))
    title = _clean_title(body.get("title"), outcome)
    duration_minutes = _clamp_duration_minutes(body.get("duration_minutes"))
    mode = _clean_mode(body.get("mode"))
    if duration_minutes is not None:
        max_iterations = MAX_TIMEBOXED_ITERATIONS
        deadline_at: str | None = _format_utc(_now() + timedelta(minutes=duration_minutes))
    else:
        max_iterations = _clamp_max_iterations(body.get("max_iterations"))
        deadline_at = None
    role_overrides = svc._sanitize_role_overrides(body.get("role_overrides")) or {}
    raw_origin = body.get("origin_session_id")
    origin_session_id: str | None = None
    if raw_origin is not None:
        if not isinstance(raw_origin, str):
            raise _service_error(HTTPStatus.BAD_REQUEST, "origin_session_id must be a string", code="invalid_origin_session_id")
        cleaned_origin = raw_origin.strip()
        if cleaned_origin:
            if len(cleaned_origin) > 256 or any(ch in cleaned_origin for ch in ("\x00", "\r", "\n")):
                raise _service_error(HTTPStatus.BAD_REQUEST, "origin_session_id is invalid", code="invalid_origin_session_id")
            origin_session_id = cleaned_origin

    repo, wt = svc._resolve_repo_and_worktree(
        settings,
        repo_id=body.get("repo_id"),
        worktree_id=body.get("worktree_id"),
        legacy_repo_path=body.get("repo_path"),
        legacy_worktree_path=body.get("worktree_path"),
    )
    if wt["is_main"] or not wt["managed"]:
        raise _service_error(
            HTTPStatus.CONFLICT,
            "目标只能在托管的 git worktree 里执行；主工作区和外部 worktree 是只读的",
            code="unmanaged_worktree",
        )
    ok_wt, reason_wt = ags.validate_worktree(settings, repo["path"], wt["path"])
    if not ok_wt:
        raise _service_error(
            HTTPStatus.BAD_REQUEST,
            f"worktree 校验失败: {reason_wt}",
            code="invalid_worktree",
        )
    _require_executor_check(settings, role_overrides)

    goal_id = f"{GOAL_ID_PREFIX}{os.urandom(8).hex()}"
    now = _utc_now()
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO supervisor_goals
               (id, repo_id, worktree_id, title, outcome, max_iterations,
                role_overrides_json, state, created_at, updated_at,
                question, summary, stop_reason, last_next_prompt,
                deadline_at, mode, resume_at, consecutive_failures, origin_session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '', '', ?, ?, NULL, 0, ?)""",
            (
                goal_id,
                str(repo["id"]),
                str(wt["id"]),
                title,
                outcome,
                max_iterations,
                json.dumps(role_overrides, ensure_ascii=False),
                GOAL_RUNNING,
                now,
                now,
                deadline_at,
                mode,
                origin_session_id,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    goal = _enrich_goal_dict(_get_goal_row(settings, goal_id), [])
    goal["usage"] = _goal_usage(settings, [])
    goal["iterations"] = []
    goal["iterations_used"] = 0
    svc.ensure_server_worker(server, settings, wake=True)
    return goal


def list_goals(settings: Any) -> list[dict[str, Any]]:
    svc.require_enabled(settings)
    conn = _connect(settings)
    try:
        rows = conn.execute(
            "SELECT * FROM supervisor_goals ORDER BY created_at DESC, id DESC"
        ).fetchall()
    finally:
        conn.close()
    goals: list[dict[str, Any]] = []
    for row in rows:
        raw_goal = _goal_row_to_dict(row)
        iters = _iterations(settings, raw_goal["id"])
        goal = _enrich_goal_dict(raw_goal, iters)
        goal["usage"] = _goal_usage(settings, iters)
        goal["iterations_used"] = len(iters)
        goals.append(goal)
    return goals


def get_goal(settings: Any, goal_id: str) -> dict[str, Any]:
    svc.require_enabled(settings)
    raw_goal = _get_goal_row(settings, goal_id)
    iterations = _iterations(settings, raw_goal["id"])
    for item in iterations:
        task_id = item["task_id"]
        if not task_id:
            item["task_state"] = ""
            continue
        task = ags.get_task(settings, task_id)
        item["task_state"] = str(task.get("state") or "") if task else "missing"
    goal = _enrich_goal_dict(raw_goal, iterations)
    goal["usage"] = _goal_usage(settings, iterations)
    goal["iterations"] = iterations
    goal["iterations_used"] = len(iterations)
    return goal


def get_goal_diff(settings: Any, goal_id: str) -> dict[str, Any]:
    """Whole-goal diff: base of the FIRST iteration task -> current worktree content.

    Because every iteration runs on the same managed worktree, diffing from the
    first task's `base_sha` shows everything the goal changed, not just the last
    iteration. 404 when the goal has not produced a task yet.
    """
    svc.require_enabled(settings)
    goal = _get_goal_row(settings, goal_id)
    iterations = _iterations(settings, goal["id"])
    first_task_id = next((str(it.get("task_id") or "") for it in iterations if it.get("task_id")), "")
    if not first_task_id:
        raise _service_error(
            HTTPStatus.NOT_FOUND,
            "目标还没有产生任何任务，暂无 diff 可读",
            code="goal_no_task",
        )
    task = ags.get_task(settings, first_task_id)
    if task is None:
        raise _service_error(
            HTTPStatus.NOT_FOUND,
            f"目标的首个任务不存在: {first_task_id}",
            code="goal_task_missing",
        )
    _repo_path, worktree_path, managed = _scope_paths(settings, goal)
    if not managed:
        raise _service_error(
            HTTPStatus.FORBIDDEN,
            "目标只能在托管的 git worktree 里读取 diff",
            code="unmanaged_worktree",
        )
    meta = task.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
    payload = diff_mod.build_worktree_diff(
        settings,
        worktree_path=worktree_path,
        base_sha=str(task.get("base_sha") or ""),
        fallback_base_sha=str(meta.get("queued_target_sha") or ""),
    )
    return {"goal_id": goal["id"], **payload}


def stop_goal(
    server: Any,
    settings: Any,
    goal_id: str,
    *,
    post_report: bool = True,
) -> dict[str, Any]:
    """User stops the goal: state → `stopped`, and the active task (if any) is cancelled."""
    svc.require_enabled(settings)
    goal = _get_goal_row(settings, goal_id)
    if goal["state"] in (GOAL_STOPPED, GOAL_ACHIEVED, GOAL_FAILED, GOAL_EXHAUSTED):
        return get_goal(settings, goal_id)

    reason = "用户手动停止了目标"
    updated = _update_goal(
        settings,
        goal_id,
        state=GOAL_STOPPED,
        question="",
        stop_reason=reason,
        resume_at=None,
        updated_at=_utc_now(),
    )

    iterations = _iterations(settings, goal_id)
    for item in reversed(iterations):
        tid = str(item.get("task_id") or "")
        if not tid:
            continue
        task = ags.get_task(settings, tid)
        if task is not None and str(task.get("state") or "") in _GOAL_ACTIVE_TASK_STATES:
            try:
                svc.cancel_scoped_task(server, settings, tid, reason=reason)
            except svc.SupervisorServiceError:
                log.warning("goal %s: cancelling task %s failed", goal_id, tid, exc_info=True)

    if post_report:
        _on_goal_terminal(settings, updated)
    _wake_goal_worker(server, settings)
    return get_goal(settings, goal_id)


def _is_reply_already_processing(goal: dict[str, Any], iterations: list[dict[str, Any]]) -> bool:
    if goal.get("state") == GOAL_RUNNING and iterations:
        last = iterations[-1]
        if str(last.get("decision_reason") or "") == "用户补充说明后继续" and not last.get("decision"):
            return True
    return False


def reply_goal(server: Any, settings: Any, goal_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Answer a `waiting_user` goal: confirm achievement, or push it forward with guidance."""
    svc.require_enabled(settings)
    if not isinstance(body, dict):
        raise _service_error(HTTPStatus.BAD_REQUEST, "request body must be an object", code="invalid_body")

    pre_goal = _get_goal_row(settings, goal_id)
    gid = pre_goal["id"]
    saw_waiting = pre_goal["state"] == GOAL_WAITING_USER

    raw_achieved = body.get("achieved")
    achieved = bool(raw_achieved) if isinstance(raw_achieved, bool) else False

    raw_message = body.get("message")
    message = ""
    if raw_message is not None:
        if not isinstance(raw_message, str):
            raise _service_error(HTTPStatus.BAD_REQUEST, "message must be a string", code="invalid_message")
        message = redact_text(raw_message).strip()

    lock = _goal_reply_lock(gid)
    if not lock.acquire(blocking=False):
        if saw_waiting:
            raise _service_error(
                HTTPStatus.CONFLICT,
                "已经在处理你的回复",
                code="goal_reply_in_progress",
            )
        lock.acquire()

    try:
        goal = _get_goal_row(settings, gid)
        iterations = _iterations(settings, gid)
        if goal["state"] != GOAL_WAITING_USER:
            if saw_waiting or _is_reply_already_processing(goal, iterations):
                raise _service_error(
                    HTTPStatus.CONFLICT,
                    "已经在处理你的回复",
                    code="goal_reply_in_progress",
                )
            raise _service_error(
                HTTPStatus.CONFLICT,
                "目标当前没有在等待用户回复",
                code="goal_not_waiting_user",
            )

        if achieved:
            now = _utc_now()
            conn = _connect(settings)
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    """UPDATE supervisor_goals
                       SET state = ?, stop_reason = '用户确认已达成', question = '', updated_at = ?
                       WHERE id = ? AND state = ?""",
                    (GOAL_ACHIEVED, now, gid, GOAL_WAITING_USER),
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    raise _service_error(
                        HTTPStatus.CONFLICT,
                        "已经在处理你的回复",
                        code="goal_reply_in_progress",
                    )
                conn.commit()
            except svc.SupervisorServiceError:
                raise
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            updated = _get_goal_row(settings, gid)
            notify_goal_change(settings, updated, GOAL_ACHIEVED)
            _on_goal_terminal(settings, updated)
            _wake_goal_worker(server, settings)
            return get_goal(settings, gid)

        if not message:
            raise _service_error(
                HTTPStatus.BAD_REQUEST,
                "请用 message 描述这一轮还要改什么（或把 achieved 设为 true 确认达成）",
                code="invalid_message",
            )

        if _deadline_passed(goal):
            raise _service_error(
                HTTPStatus.CONFLICT,
                "这个目标的运行时间已经到了，不能继续跑下一轮",
                code="goal_deadline_passed",
            )

        current = iterations[-1] if iterations else None
        next_n = len(iterations) + 1
        # Documented exception: when the budget is used up the user's explicit reply
        # buys exactly ONE extra iteration; anything beyond that is refused.
        # Check budget BEFORE cancelling the waiting task so a 409 never kills it.
        if next_n > int(goal["max_iterations"]) + 1:
            raise _service_error(
                HTTPStatus.CONFLICT,
                "迭代预算已经用完（用户补充也只允许多跑一轮），请调高 max_iterations 后重试",
                code="goal_iterations_exhausted",
            )

        base_prompt = str(goal.get("last_next_prompt") or "").strip()
        prompt = _reply_prompt(goal, next_n, base_prompt, message, settings=settings)

        now = _utc_now()
        conn = _connect(settings)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """UPDATE supervisor_goals
                   SET state = ?, question = '', resume_at = NULL,
                       consecutive_failures = 0, updated_at = ?
                   WHERE id = ? AND state = ?""",
                (GOAL_RUNNING, now, gid, GOAL_WAITING_USER),
            )
            if cur.rowcount != 1:
                conn.rollback()
                raise _service_error(
                    HTTPStatus.CONFLICT,
                    "已经在处理你的回复",
                    code="goal_reply_in_progress",
                )
            conn.execute(
                """INSERT INTO supervisor_goal_iterations
                   (goal_id, n, task_id, prompt, decision, decision_reason,
                    started_at, finished_at, task_final_state, evaluating, evaluating_since)
                   VALUES (?, ?, '', ?, '', '用户补充说明后继续', ?, '', '', 0, '')""",
                (gid, next_n, prompt, now),
            )
            conn.commit()
        except svc.SupervisorServiceError:
            raise
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise _service_error(
                HTTPStatus.CONFLICT,
                "已经在处理你的回复",
                code="goal_reply_in_progress",
            ) from exc
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        # Release the worktree lock held by a task that is itself waiting for the user.
        if current and current["task_id"]:
            task = ags.get_task(settings, current["task_id"])
            if task is not None and str(task.get("state") or "") == "waiting_user":
                try:
                    svc.cancel_scoped_task(
                        server, settings, current["task_id"], reason="用户回复目标，取消等待中的任务"
                    )
                except svc.SupervisorServiceError:
                    log.warning(
                        "goal %s: cancelling waiting task %s failed",
                        gid,
                        current["task_id"],
                        exc_info=True,
                    )

        _wake_goal_worker(server, settings)
        return get_goal(settings, gid)
    finally:
        lock.release()


def extend_goal(server: Any, settings: Any, goal_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """「再试几轮」 for an `exhausted` or `failed` goal: raise `max_iterations` by
    `rounds` (1-10), set state back to `running`, and start the next round."""
    svc.require_enabled(settings)
    if not isinstance(body, dict):
        raise _service_error(HTTPStatus.BAD_REQUEST, "请求体必须是 JSON 对象", code="invalid_body")

    raw_rounds = body.get("rounds")
    if (
        isinstance(raw_rounds, bool)
        or not isinstance(raw_rounds, int)
        or raw_rounds < MIN_EXTEND_ROUNDS
        or raw_rounds > MAX_EXTEND_ROUNDS
    ):
        raise _service_error(
            HTTPStatus.BAD_REQUEST,
            f"再试轮数必须是 {MIN_EXTEND_ROUNDS} 到 {MAX_EXTEND_ROUNDS} 之间的整数",
            code="invalid_rounds",
        )
    rounds = int(raw_rounds)

    raw_message = body.get("message")
    message = ""
    if raw_message is not None:
        if not isinstance(raw_message, str):
            raise _service_error(
                HTTPStatus.BAD_REQUEST,
                f"补充说明必须是不超过 {MAX_EXTEND_MESSAGE_CHARS} 个字的字符串",
                code="invalid_message",
            )
        if "\x00" in raw_message:
            raise _service_error(
                HTTPStatus.BAD_REQUEST,
                "补充说明包含无效字符",
                code="invalid_message",
            )
        cleaned_message = raw_message.strip()
        if len(cleaned_message) > MAX_EXTEND_MESSAGE_CHARS or (
            not cleaned_message and len(raw_message) > MAX_EXTEND_MESSAGE_CHARS
        ):
            raise _service_error(
                HTTPStatus.BAD_REQUEST,
                f"补充说明不能超过 {MAX_EXTEND_MESSAGE_CHARS} 个字",
                code="invalid_message",
            )
        message = redact_text(cleaned_message).strip()

    goal = _get_goal_row(settings, goal_id)
    if goal["state"] not in (GOAL_EXHAUSTED, GOAL_FAILED):
        raise _service_error(
            HTTPStatus.CONFLICT,
            "只有已用完轮数或失败的目标才能再试几轮",
            code="goal_not_extendable",
        )
    if _deadline_passed(goal):
        raise _service_error(
            HTTPStatus.CONFLICT,
            "这个目标的运行时间已经到了，无法继续再试",
            code="goal_deadline_passed",
        )

    _repo_path, wt_path, managed = _scope_paths(settings, goal)
    if not managed:
        raise _service_error(
            HTTPStatus.CONFLICT,
            "目标只能在托管的 git worktree 里继续执行",
            code="unmanaged_worktree",
        )
    if ags.has_active_task_for_worktree(settings, wt_path):
        raise _service_error(
            HTTPStatus.CONFLICT,
            "当前工作目录还有正在执行的任务，等它结束后再试",
            code="worktree_busy",
        )
    _require_executor_check(settings, goal.get("role_overrides") or {})

    iterations = _iterations(settings, goal["id"])
    next_n = len(iterations) + 1
    new_max_iterations = int(goal["max_iterations"]) + rounds
    base_prompt = str(goal.get("last_next_prompt") or "").strip()
    prompt = _extend_prompt(goal, next_n, base_prompt, message, settings=settings)

    now = _utc_now()
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO supervisor_goal_iterations
               (goal_id, n, task_id, prompt, decision, decision_reason,
                started_at, finished_at, task_final_state, evaluating, evaluating_since)
               VALUES (?, ?, '', ?, '', '', ?, '', '', 0, '')""",
            (goal["id"], next_n, prompt, now),
        )
        conn.execute(
            """UPDATE supervisor_goals
               SET state = ?, max_iterations = ?, question = '', stop_reason = '',
                   resume_at = NULL, consecutive_failures = 0, updated_at = ?
               WHERE id = ?""",
            (GOAL_RUNNING, new_max_iterations, now, goal["id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    updated_goal = _get_goal_row(settings, goal["id"])
    _start_iteration(server, settings, updated_goal, next_n, prompt)
    svc.ensure_server_worker(server, settings, wake=True)
    return get_goal(settings, goal["id"])


# ---------------------------------------------------------------------------
# Iteration persistence helpers
# ---------------------------------------------------------------------------

def _ensure_iteration(settings: Any, goal_id: str, n: int, prompt: str) -> dict[str, Any]:
    """Insert iteration `n` when absent (idempotent), then return the stored row."""
    now = _utc_now()
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT OR IGNORE INTO supervisor_goal_iterations
               (goal_id, n, task_id, prompt, decision, decision_reason,
                started_at, finished_at, task_final_state, evaluating, evaluating_since)
               VALUES (?, ?, '', ?, '', '', ?, '', '', 0, '')""",
            (goal_id, n, prompt, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM supervisor_goal_iterations WHERE goal_id = ? AND n = ?",
            (goal_id, n),
        ).fetchone()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if row is None:  # pragma: no cover - defensive
        raise _service_error(HTTPStatus.INTERNAL_SERVER_ERROR, "无法记录目标轮次")
    return _iteration_row_to_dict(row)


def _set_iteration_task(settings: Any, goal_id: str, n: int, task_id: str) -> None:
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE supervisor_goal_iterations SET task_id = ? WHERE goal_id = ? AND n = ?",
            (task_id, goal_id, n),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _clear_iteration_task(settings: Any, goal_id: str, n: int) -> None:
    """Forget a task that never ran, so the next tick can create a fresh one."""
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE supervisor_goal_iterations SET task_id = '' WHERE goal_id = ? AND n = ?",
            (goal_id, n),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _clear_iteration_for_retry(settings: Any, goal_id: str, n: int) -> None:
    """Reset task_id and claim flags so the same iteration `n` can retry after quota pause."""
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE supervisor_goal_iterations
               SET task_id = '', evaluating = 0, evaluating_since = ''
               WHERE goal_id = ? AND n = ?""",
            (goal_id, n),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _claim_iteration(settings: Any, goal_id: str, n: int) -> bool:
    """Atomically claim an iteration for evaluation.

    Returns False when another tick is already evaluating it, or when it already
    carries a decision (evaluation is idempotent: never re-evaluated). A claim
    left behind by a crashed tick is taken over once it is older than
    `EVAL_STALE_SECONDS`.
    """
    now = _utc_now()
    try:
        stale_cutoff = _format_utc(
            datetime.fromtimestamp(_now().timestamp() - EVAL_STALE_SECONDS, tz=timezone.utc)
        )
    except Exception:  # pragma: no cover - defensive
        stale_cutoff = ""
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """UPDATE supervisor_goal_iterations
               SET evaluating = 1, evaluating_since = ?
               WHERE goal_id = ? AND n = ?
                 AND (decision = '' OR decision IS NULL)
                 AND (evaluating = 0 OR evaluating_since = '' OR evaluating_since < ?)""",
            (now, goal_id, n, stale_cutoff),
        )
        claimed = cur.rowcount == 1
        if claimed:
            conn.commit()
        else:
            conn.rollback()
        return claimed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _record_decision(
    settings: Any,
    goal_id: str,
    n: int,
    *,
    decision: str,
    reason: str,
    task_state: str,
    passed: bool = False,
    round_title: str = "",
) -> None:
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE supervisor_goal_iterations
               SET decision = ?, decision_reason = ?, task_final_state = ?,
                   finished_at = ?, passed = ?, round_title = ?,
                   evaluating = 0, evaluating_since = ''
               WHERE goal_id = ? AND n = ?""",
            (
                decision,
                truncate(redact_text(str(reason or "")), MAX_REASON_CHARS),
                str(task_state or ""),
                _utc_now(),
                1 if passed else 0,
                _display_title(round_title) if round_title else "",
                goal_id,
                n,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_GOAL_REPORTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS supervisor_goal_reports (
    goal_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT '',
    card_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
)
"""


def _positive_int(raw: Any) -> int:
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int) and raw > 0:
        return raw
    if isinstance(raw, str) and raw.strip().isdigit() and int(raw.strip()) > 0:
        return int(raw.strip())
    return 0


def _clip_report_detail(text: Any, limit: int = 200) -> str:
    collapsed = " ".join(redact_text(str(text or "")).split())
    if len(collapsed) > limit:
        return collapsed[:limit] + "…"
    return collapsed


def _format_zh_hm(elapsed_seconds: int) -> str:
    total_mins = max(0, int(elapsed_seconds)) // 60
    if total_mins <= 0:
        return "不到 1 分钟"
    hours, mins = divmod(total_mins, 60)
    if hours == 0:
        return f"{mins} 分"
    if mins == 0:
        return f"{hours} 小时"
    return f"{hours} 小时 {mins} 分"


def _format_elapsed_zh(created_at: Any, updated_at: Any) -> str:
    start_dt = _parse_utc(created_at)
    if start_dt is None:
        return "用时不到 1 分钟"
    end_dt = _parse_utc(updated_at) or _now()
    elapsed_seconds = max(0, int((end_dt - start_dt).total_seconds()))
    hm = _format_zh_hm(elapsed_seconds)
    if hm == "不到 1 分钟":
        return "用时不到 1 分钟"
    return f"用时 {hm}"


def _resolve_report_iterations(settings: Any, goal: dict[str, Any]) -> list[dict[str, Any]]:
    goal_id = str(goal.get("id") or "").strip()
    if goal_id and settings is not None and getattr(settings, "codex_memory_root", ""):
        try:
            db_iters = _iterations(settings, goal_id)
            if db_iters:
                return db_iters
        except Exception:
            pass
    raw = goal.get("iterations")
    if isinstance(raw, list):
        return [it for it in raw if isinstance(it, dict)]
    return []


def _is_check_script_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("name") == "check-script":
        return True
    argv = entry.get("argv")
    if isinstance(argv, (list, tuple)) and tuple(argv) == ("sh", "scripts/check.sh"):
        return True
    return "scripts/check.sh" in str(entry.get("command") or "")


_CHECK_SCRIPT_STATUS_LINES = {
    "passed": "自动检查：scripts/check.sh（最后一轮 通过）",
    "failed": "自动检查：scripts/check.sh（最后一轮 没通过）",
    "not_run": "自动检查：scripts/check.sh（最后一轮没有跑到检查就停了）",
    "never_ran": "自动检查：scripts/check.sh（还没有跑过检查）",
}


def _task_check_script_passed(settings: Any, task_id: str) -> bool | None:
    """Return the stored `check-script` outcome for `task_id`, or None if validation did not run."""
    tid = str(task_id or "").strip()
    if not tid or settings is None or not getattr(settings, "codex_memory_root", ""):
        return None
    try:
        acc = ags.latest_acceptance(settings, tid)
    except Exception:
        return None
    if not isinstance(acc, dict):
        return None
    checks = acc.get("executed_checks") or []
    if not isinstance(checks, list):
        return None
    for chk in checks:
        if _is_check_script_entry(chk):
            return bool(chk.get("passed"))
    return None


def _last_round_check_script_status(settings: Any, iterations: list[dict[str, Any]]) -> str:
    """Classify `scripts/check.sh` outcome using stored validation results only."""
    ran = [it for it in iterations if _iteration_ran(it)]
    if not ran:
        return "never_ran"
    last_result = _task_check_script_passed(settings, str(ran[-1].get("task_id") or ""))
    if last_result is True:
        return "passed"
    if last_result is False:
        return "failed"
    return "not_run"


def _last_round_check_script_passed(settings: Any, iterations: list[dict[str, Any]]) -> bool | None:
    status = _last_round_check_script_status(settings, iterations)
    if status == "passed":
        return True
    if status == "failed":
        return False
    return None


def _resolve_report_scope(
    settings: Any,
    goal: dict[str, Any],
    iterations: list[dict[str, Any]],
) -> dict[str, Any]:
    repo_id = str(goal.get("repo_id") or "").strip()
    worktree_id = str(goal.get("worktree_id") or "").strip()
    repo_name = str(goal.get("repo_name") or "").strip()
    repo_path = ""
    worktree_path = ""
    branch = ""
    target_branch = ""
    commits_ahead = 0

    if settings is not None and getattr(settings, "codex_memory_root", "") and repo_id and worktree_id:
        try:
            conn = svc._connect(settings)
            try:
                repo_row = conn.execute(
                    "SELECT name, canonical_path FROM supervisor_repositories WHERE id = ?",
                    (repo_id,),
                ).fetchone()
                wt_row = conn.execute(
                    "SELECT canonical_path, branch FROM supervisor_worktrees WHERE id = ?",
                    (worktree_id,),
                ).fetchone()
            finally:
                conn.close()
            if repo_row is not None:
                repo_name = repo_name or str(repo_row["name"] or "").strip()
                repo_path = str(repo_row["canonical_path"] or "")
            if wt_row is not None:
                worktree_path = str(wt_row["canonical_path"] or "")
                branch = str(wt_row["branch"] or "").strip()
        except Exception:
            pass

    folder_present = bool(worktree_path) and Path(worktree_path).is_dir()
    if worktree_path and repo_path and Path(repo_path).is_dir() and (folder_present or branch):
        try:
            from conveyor.agents import supervisor_merge as merge_mod

            if not branch:
                branch = merge_mod._current_branch(settings, worktree_path)
            target_branch = merge_mod._target_branch(
                settings,
                {"main_path": repo_path, "worktree_path": worktree_path},
            )
            if folder_present:
                if target_branch and merge_mod._branch_exists(settings, worktree_path, target_branch):
                    commits_ahead = merge_mod._commits_ahead(
                        settings,
                        {"worktree_path": worktree_path},
                        target_branch,
                    )
                else:
                    first_task_id = next(
                        (str(it.get("task_id") or "") for it in iterations if it.get("task_id")),
                        "",
                    )
                    first_task = ags.get_task(settings, first_task_id) if first_task_id else None
                    base_sha = str((first_task or {}).get("base_sha") or "").strip()
                    if base_sha:
                        rc, out, _ = merge_mod._git(
                            settings,
                            worktree_path,
                            ["rev-list", "--count", f"{base_sha}..HEAD"],
                        )
                        if rc == 0:
                            commits_ahead = int(out.strip() or "0")
            elif branch and target_branch and merge_mod._branch_exists(settings, repo_path, branch):
                # 文件夹丢了（例如整个 worktrees/supervisor 被删）：分支还在，
                # 提交数从主工作目录那边数，报告照样说清楚改动在哪。
                rc, out, _ = merge_mod._git(
                    settings,
                    repo_path,
                    ["rev-list", "--count", f"refs/heads/{target_branch}..refs/heads/{branch}"],
                )
                if rc == 0:
                    commits_ahead = int(out.strip() or "0")
        except Exception:
            pass

    if not repo_name:
        repo_name = str(goal.get("title") or "").strip() or "当前项目"
    if not target_branch:
        target_branch = "main"

    return {
        "repo_id": repo_id,
        "worktree_id": worktree_id,
        "repo_name": repo_name,
        "branch": branch,
        "target_branch": target_branch,
        "commits_ahead": max(0, int(commits_ahead)),
    }


def _build_finish_report_bundle(
    settings: Any,
    goal: dict[str, Any],
    *,
    state: str | None = None,
) -> tuple[str, dict[str, Any]]:
    eff_state = str(state if state is not None else goal.get("state") or "").strip()
    stop_reason = str(goal.get("stop_reason") or "").strip()
    label = goal_status_label(eff_state, stop_reason)
    if stop_reason == STOP_REASON_DEADLINE and LATE_ROUND_MARKER in str(goal.get("summary") or ""):
        label = "时间快到了，剩下的时间不够再跑一轮"

    iterations = _resolve_report_iterations(settings, goal)
    if iterations:
        enriched = _enrich_goal_dict(goal, iterations)
        rounds_total = int(enriched["rounds_total"])
        rounds_passed = int(enriched["rounds_passed"])
    else:
        rounds_total = _positive_int(goal.get("rounds_total")) or _positive_int(goal.get("iterations_used"))
        rounds_passed = _positive_int(goal.get("rounds_passed"))

    scope = _resolve_report_scope(settings, goal, iterations)
    elapsed_zh = _format_elapsed_zh(goal.get("created_at"), goal.get("updated_at"))

    usage = goal.get("usage")
    if not isinstance(usage, dict):
        # Older callers (and terminal snapshots taken before usage was attached)
        # may not carry it; never invent a zero in that case, just read it.
        usage = _goal_usage(settings, iterations)
    usage_phrase = ags.usage_summary_phrase(usage)
    rounds_line = f"跑了 {rounds_total} 轮，通过 {rounds_passed} 轮，{elapsed_zh}"
    if usage_phrase:
        rounds_line += f"，{usage_phrase}"

    lines: list[str] = [
        f"{scope['repo_name']} 的目标结束了：{label}。",
        f"{rounds_line}。",
    ]

    passed_titles: list[str] = []
    for it in iterations:
        if bool(it.get("passed")) or it.get("decision") == DECISION_ACHIEVED:
            rt = str(it.get("round_title") or "").strip()
            if rt:
                passed_titles.append(rt)
    for rt in passed_titles[-MAX_FINISH_REPORT_BULLETS:]:
        lines.append(f"- {rt}")

    if eff_state == GOAL_FAILED and stop_reason:
        lines.append(f"原因：{_clip_report_detail(stop_reason)}")

    if _repo_uses_check_script(settings, goal):
        check_status = _last_round_check_script_status(settings, iterations)
        lines.append(_CHECK_SCRIPT_STATUS_LINES[check_status])

    if scope["commits_ahead"] > 0 and scope["branch"]:
        lines.append(f"改动在工作目录 {scope['branch']}，还没有合并到主分支。")
    else:
        lines.append("没有产生改动。")

    return "\n".join(lines), scope


def build_finish_report_text(
    settings: Any,
    goal: dict[str, Any],
    *,
    state: str | None = None,
) -> str:
    """Build the plain Simplified-Chinese finish report shared by web chat and notifications."""
    text, _scope = _build_finish_report_bundle(settings, goal, state=state)
    return text


def _default_report_session_id() -> str:
    from conveyor.agents import agents
    return agents.session_id_for(agents.DEFAULT_AGENT_ID)


def _claim_finish_report(settings: Any, goal_id: str, session_id: str) -> bool:
    if settings is None or not goal_id:
        return False
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(_GOAL_REPORTS_SCHEMA_SQL)
        cur = conn.execute(
            """INSERT OR IGNORE INTO supervisor_goal_reports
               (goal_id, session_id, card_id, created_at)
               VALUES (?, ?, '', ?)""",
            (goal_id, session_id, _utc_now()),
        )
        claimed = cur.rowcount == 1
        conn.commit()
        return claimed
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


def _record_finish_report_card(settings: Any, goal_id: str, card_id: str) -> None:
    if settings is None or not goal_id or not card_id:
        return
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE supervisor_goal_reports SET card_id = ? WHERE goal_id = ?",
            (card_id, goal_id),
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


def _report_target_session_id(goal: dict[str, Any]) -> str:
    return str(goal.get("origin_session_id") or "").strip() or _default_report_session_id()


def _resolve_repo_name(settings: Any, goal: dict[str, Any]) -> str:
    repo_name = str(goal.get("repo_name") or "").strip()
    if repo_name:
        return repo_name
    repo_id = str(goal.get("repo_id") or "").strip()
    if settings is not None and getattr(settings, "codex_memory_root", "") and repo_id:
        try:
            conn = svc._connect(settings)
            try:
                repo_row = conn.execute(
                    "SELECT name FROM supervisor_repositories WHERE id = ?",
                    (repo_id,),
                ).fetchone()
            finally:
                conn.close()
            if repo_row is not None:
                repo_name = str(repo_row["name"] or "").strip()
        except Exception:
            pass
    return repo_name or str(goal.get("title") or "").strip() or "当前项目"


def _format_digest_remaining_zh(deadline_dt: datetime, now_dt: datetime) -> str:
    rem_seconds = max(0, int((deadline_dt - now_dt).total_seconds()))
    if rem_seconds < 60:
        return "还剩不到 1 分钟"
    rem_mins = math.ceil(rem_seconds / 60)
    if rem_mins < 60:
        return f"还剩约 {rem_mins} 分钟"
    hours, mins = divmod(rem_mins, 60)
    if mins == 0:
        return f"还剩约 {hours} 小时"
    return f"还剩约 {hours} 小时 {mins} 分"


def _digest_usage_line(
    settings: Any,
    goal: dict[str, Any],
    iterations: list[dict[str, Any]],
) -> str:
    usage = goal.get("usage")
    if not isinstance(usage, dict):
        usage = _goal_usage(settings, iterations)
    if not isinstance(usage, dict) or int(usage.get("runs_with_usage") or 0) <= 0:
        return ""
    phrase = ags.usage_summary_phrase(usage)
    if not phrase:
        return ""
    if phrase.startswith("用了"):
        return f"已用{phrase[2:]}。"
    return f"{phrase}。"


def build_progress_digest_text(
    settings: Any,
    goal: dict[str, Any],
    iterations: list[dict[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> str:
    """Build one short Simplified-Chinese progress digest for a running time-boxed goal."""
    now_dt = now or _now()
    iters = iterations if iterations is not None else _resolve_report_iterations(settings, goal)
    completed = [it for it in iters if _iteration_completed(it)]
    rounds_total = len(completed)
    rounds_passed = sum(
        1 for it in completed
        if bool(it.get("passed")) or it.get("decision") == DECISION_ACHIEVED
    )

    repo_name = _resolve_repo_name(settings, goal)
    start_dt = _parse_utc(goal.get("created_at"))
    if start_dt is None:
        elapsed_phrase = "已经不到 1 分钟"
    else:
        hm = _format_zh_hm(max(0, int((now_dt - start_dt).total_seconds())))
        elapsed_phrase = "已经不到 1 分钟" if hm == "不到 1 分钟" else f"已经 {hm}"

    parts = [elapsed_phrase, f"跑了 {rounds_total} 轮、通过 {rounds_passed} 轮"]
    deadline_dt = _parse_utc(goal.get("deadline_at"))
    if deadline_dt is not None:
        parts.append(_format_digest_remaining_zh(deadline_dt, now_dt))

    head = f"{repo_name} 还在跑：{'，'.join(parts)}。"

    last_three_failed = len(completed) >= 3 and all(
        not (bool(it.get("passed")) or it.get("decision") == DECISION_ACHIEVED)
        for it in completed[-3:]
    )
    if last_three_failed:
        body = f"{head}最近 3 轮都没通过检查，正在继续修。"
    else:
        passed_titles = [
            str(it.get("round_title") or "").strip()
            for it in completed
            if (bool(it.get("passed")) or it.get("decision") == DECISION_ACHIEVED)
            and str(it.get("round_title") or "").strip()
        ]
        recent_titles = passed_titles[-DIGEST_RECENT_TITLES:]
        if recent_titles:
            body = f"{head}最近完成：{'、'.join(recent_titles)}。"
        else:
            body = head

    usage_line = _digest_usage_line(settings, goal, completed)
    if usage_line:
        return f"{body}\n{usage_line}"
    return body


def _claim_progress_digest(
    settings: Any,
    goal_id: str,
    *,
    digest_at: str,
    rounds: int,
) -> bool:
    if settings is None or not goal_id or rounds <= 0:
        return False
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """UPDATE supervisor_goals
               SET last_digest_at = ?, last_digest_rounds = ?
               WHERE id = ?
                 AND state = ?
                 AND deadline_at IS NOT NULL
                 AND TRIM(deadline_at) != ''
                 AND COALESCE(last_digest_rounds, 0) < ?""",
            (digest_at, int(rounds), goal_id, GOAL_RUNNING, int(rounds)),
        )
        claimed = cur.rowcount == 1
        conn.commit()
        return claimed
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


def _maybe_post_progress_digest(
    settings: Any,
    goal: dict[str, Any],
    iterations: list[dict[str, Any]],
) -> bool:
    """Post a short progress digest when a running time-boxed goal hits 3 rounds or 60 minutes."""
    if settings is None or not isinstance(goal, dict):
        return False
    if not getattr(settings, "codex_memory_root", "") or not digests_enabled(settings):
        return False
    goal_id = str(goal.get("id") or "").strip()
    if not goal_id or str(goal.get("state") or "") != GOAL_RUNNING:
        return False
    now_dt = _now()
    deadline_dt = _parse_utc(goal.get("deadline_at"))
    if deadline_dt is None or now_dt >= deadline_dt:
        return False

    completed = [it for it in iterations if _iteration_completed(it)]
    completed_rounds = len(completed)
    last_rounds = int(goal.get("last_digest_rounds") or 0)
    if completed_rounds <= last_rounds:
        return False

    baseline_at = _parse_utc(goal.get("last_digest_at")) or _parse_utc(goal.get("created_at"))
    elapsed_since_last = (now_dt - baseline_at).total_seconds() if baseline_at is not None else 0.0
    if (completed_rounds - last_rounds) < DIGEST_EVERY_ROUNDS and elapsed_since_last < DIGEST_INTERVAL_SECONDS:
        return False

    digest_at = _format_utc(now_dt)
    if not _claim_progress_digest(settings, goal_id, digest_at=digest_at, rounds=completed_rounds):
        return False

    text = build_progress_digest_text(settings, goal, completed, now=now_dt)
    target_session_id = _report_target_session_id(goal)
    try:
        from conveyor.agents import chat_intents

        chat_intents.post_goal_progress_digest(
            settings,
            session_id=target_session_id,
            text=text,
        )
    except Exception:
        log.warning("goal %s: failed to post progress digest", goal_id, exc_info=True)

    notify_goal_digest(settings, goal, text, rounds=completed_rounds, digest_at=digest_at)
    return True


def _on_goal_terminal(settings: Any, goal: dict[str, Any]) -> None:
    """Post the finish report once to the origin (or default main) web-chat session."""
    if settings is None or not isinstance(goal, dict):
        return
    if not getattr(settings, "codex_memory_root", ""):
        return
    goal_id = str(goal.get("id") or "").strip()
    state = str(goal.get("state") or "").strip()
    if not goal_id or state not in GOAL_TERMINAL_STATES:
        return
    target_session_id = _report_target_session_id(goal)
    if not _claim_finish_report(settings, goal_id, target_session_id):
        return
    try:
        from conveyor.agents import chat_intents

        report_text, scope = _build_finish_report_bundle(settings, goal, state=state)
        card = chat_intents.post_goal_finish_report(
            settings,
            session_id=target_session_id,
            report_text=report_text,
            goal_id=goal_id,
            repo_id=scope["repo_id"],
            worktree_id=scope["worktree_id"],
            source_branch=scope["branch"],
            target_branch=scope["target_branch"],
            outcome=str(goal.get("outcome") or ""),
        )
        if isinstance(card, dict) and card.get("id"):
            _record_finish_report_card(settings, goal_id, str(card["id"]))
    except Exception:
        log.warning("goal %s: failed to post finish report", goal_id, exc_info=True)


def post_finish_report(settings: Any, goal: dict[str, Any]) -> None:
    """Idempotently post the goal finish report to web chat."""
    _on_goal_terminal(settings, goal)


def _finish_goal(
    settings: Any,
    goal: dict[str, Any],
    state: str,
    *,
    summary: str,
    question: str = "",
    stop_reason: str = "",
) -> dict[str, Any]:
    now = _utc_now()
    clean_question = truncate(redact_text(question or ""), MAX_QUESTION_CHARS)
    clean_summary = truncate(redact_text(summary or ""), MAX_SUMMARY_CHARS)
    clean_stop_reason = truncate(redact_text(stop_reason or ""), MAX_REASON_CHARS)
    if state in GOAL_TERMINAL_STATES:
        terminal_snapshot = dict(goal)
        terminal_snapshot.update(
            {
                "state": state,
                "question": clean_question,
                "summary": clean_summary,
                "stop_reason": clean_stop_reason,
                "resume_at": None,
                "updated_at": now,
            }
        )
        _on_goal_terminal(settings, terminal_snapshot)
    updated = _update_goal(
        settings,
        goal["id"],
        state=state,
        question=clean_question,
        summary=clean_summary,
        stop_reason=clean_stop_reason,
        resume_at=None,
        updated_at=now,
    )
    updated["usage"] = _goal_usage(settings, _iterations(settings, goal["id"]))
    log.info("goal %s -> %s (%s)", goal["id"], state, updated.get("stop_reason") or updated.get("question") or "")
    # Tell the owner on Telegram / Feishu / Web Push. Non-blocking and idempotent;
    # `stopped` is skipped because the owner stopped the goal themselves.
    if state in GOAL_NOTIFY_STATES:
        notify_goal_change(settings, updated, state)
    return updated


def _min_round_start_seconds() -> int:
    raw = os.getenv("CONVEYOR_GOAL_MIN_ROUND_START_SECONDS", "300")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 300


def _zh_minutes(seconds: float) -> str:
    minutes = int(seconds // 60)
    return "不到 1 分钟" if minutes < 1 else f"约 {minutes} 分钟"


def _late_round_note(
    goal: dict[str, Any], iterations: list[dict[str, Any]], *, first_round: bool = False,
) -> str | None:
    """Plain-Chinese stop note when a time-boxed goal has too little time left to
    start another round; None when the round should start."""
    min_seconds = _min_round_start_seconds()
    deadline_dt = _parse_utc(goal.get("deadline_at"))
    if min_seconds <= 0 or deadline_dt is None:
        return None
    remaining = (deadline_dt - _now()).total_seconds()
    durations: list[float] = []
    for item in iterations:
        start, end = _parse_utc(item.get("started_at")), _parse_utc(item.get("finished_at"))
        if start is not None and end is not None and end > start:
            durations.append((end - start).total_seconds())
    durations.sort()
    typical = durations[len(durations) // 2] if durations else 0.0
    threshold = min_seconds if first_round else max(min_seconds, LATE_ROUND_FRACTION * typical)
    if remaining >= threshold:
        return None
    note = f"时间快到了：只剩{_zh_minutes(remaining)}，不够再跑一轮"
    if typical and not first_round:
        note += f"（每轮大约 {max(1, round(typical / 60))} 分钟）"
    note += "，就在这里停了。"
    if not first_round:
        enriched = _enrich_goal_dict(goal, iterations)
        note += f"一共跑了 {int(enriched['rounds_total'])} 轮，其中 {int(enriched['rounds_passed'])} 轮通过检查。"
    return note


def _finish_deadline(
    settings: Any,
    goal: dict[str, Any],
    iterations: list[dict[str, Any]] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    iters = iterations if iterations is not None else _iterations(settings, str(goal["id"]))
    enriched = _enrich_goal_dict(goal, iters)
    total = int(enriched["rounds_total"])
    passed = int(enriched["rounds_passed"])
    summary = note or f"时间到了：一共跑了 {total} 轮，其中 {passed} 轮通过检查。"
    return _finish_goal(
        settings,
        goal,
        GOAL_EXHAUSTED,
        summary=summary,
        stop_reason=STOP_REASON_DEADLINE,
    )


def _fail_goal(settings: Any, goal: dict[str, Any], reason: str) -> dict[str, Any]:
    return _finish_goal(
        settings,
        goal,
        GOAL_FAILED,
        summary=f"目标已失败：{reason}",
        stop_reason=reason,
    )


def _running_goals(settings: Any) -> list[dict[str, Any]]:
    conn = _connect(settings)
    try:
        rows = conn.execute(
            "SELECT * FROM supervisor_goals WHERE state = ? ORDER BY created_at ASC, id ASC",
            (GOAL_RUNNING,),
        ).fetchall()
    finally:
        conn.close()
    return [_goal_row_to_dict(r) for r in rows]


def _waiting_quota_goals(settings: Any) -> list[dict[str, Any]]:
    conn = _connect(settings)
    try:
        rows = conn.execute(
            "SELECT * FROM supervisor_goals WHERE state = ? ORDER BY created_at ASC, id ASC",
            (GOAL_WAITING_QUOTA,),
        ).fetchall()
    finally:
        conn.close()
    return [_goal_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Task creation / dispatch through the existing service functions
# ---------------------------------------------------------------------------

def _repo_uses_check_script(settings: Any, goal: dict[str, Any] | None) -> bool:
    if settings is None or not isinstance(goal, dict):
        return False
    repo_id = str(goal.get("repo_id") or "").strip()
    if not repo_id:
        return False
    try:
        from conveyor.agents import supervisor_validation

        info = supervisor_validation.get_repo_validation(settings, repo_id)
    except Exception:
        return False
    effective = info.get("effective") or []
    return any(
        isinstance(item, dict)
        and (
            item.get("name") == "check-script"
            or tuple(item.get("argv") or ()) == ("sh", "scripts/check.sh")
        )
        for item in effective
    )


def _append_check_script_note(
    raw: str,
    *,
    settings: Any = None,
    goal: dict[str, Any] | None = None,
) -> str:
    if not _repo_uses_check_script(settings, goal):
        return truncate(raw, svc.MAX_GOAL_CHARS)
    suffix = f"\n\n{CHECK_SCRIPT_PROMPT_NOTE}"
    body_budget = max(0, svc.MAX_GOAL_CHARS - len(suffix))
    body = truncate(raw, body_budget) if len(raw) > body_budget else raw
    return truncate(f"{body}{suffix}", svc.MAX_GOAL_CHARS)


def _first_iteration_prompt(goal: dict[str, Any], *, settings: Any = None) -> str:
    raw = f"{goal['outcome']}\n\n{FIRST_ITERATION_NOTE}"
    return _append_check_script_note(raw, settings=settings, goal=goal)


def _continuation_prompt(
    goal: dict[str, Any],
    n: int,
    next_prompt: str,
    *,
    settings: Any = None,
) -> str:
    raw = f"这是目标「{goal['title']}」的第 {n} 轮。目标：{goal['outcome']}\n\n本轮要做：{next_prompt}"
    return _append_check_script_note(raw, settings=settings, goal=goal)


def _continuous_next_prompt(
    goal: dict[str, Any],
    n: int,
    iterations: list[dict[str, Any]],
    next_suggestion: str = "",
    *,
    settings: Any = None,
) -> str:
    """Build next-round prompt in continuous mode, listing passed improvements."""
    passed_items = [
        it for it in iterations
        if bool(it.get("passed")) or it.get("decision") == DECISION_ACHIEVED
    ]
    lines: list[str] = []
    for item in passed_items[-MAX_PASSED_ROUND_TITLES_IN_PROMPT:]:
        title = str(item.get("round_title") or "").strip() or f"第 {item['n']} 轮改进"
        lines.append(f"- 第 {item['n']} 轮：{title}")

    suggestion = str(next_suggestion or "").strip()
    if suggestion:
        action = (
            f"{suggestion}\n"
            "请保持本轮改动聚焦（只做一个小而有价值的改进，确保所有检查和测试通过）。"
        )
    else:
        action = (
            "请基于已完成的改进，评估当前项目状态并完成下一个最值得做的小改进（持续向目标推进）。"
            "每轮只做一个清晰的小改进，并确保所有测试和检查通过。"
        )

    header = f"这是目标「{goal['title']}」的第 {n} 轮。目标：{goal['outcome']}\n\n"
    footer = f"本轮要做：{action}"
    done_block = (
        "已完成改进（请勿重复，继续做下一个有价值的改进）：\n"
        + "\n".join(lines)
        + "\n\n"
        if lines
        else ""
    )
    suffix = f"\n\n{CHECK_SCRIPT_PROMPT_NOTE}" if _repo_uses_check_script(settings, goal) else ""
    # Keep header + done_block + footer + suffix within svc.MAX_GOAL_CHARS while preserving footer and suffix.
    budget_for_done = svc.MAX_GOAL_CHARS - len(header) - len(footer) - len(suffix)
    if len(done_block) > max(0, budget_for_done):
        done_block = truncate(done_block, max(0, budget_for_done - 2)) + "\n\n"
    raw = f"{header}{done_block}{footer}{suffix}"
    return truncate(raw, svc.MAX_GOAL_CHARS)


def _reply_prompt(
    goal: dict[str, Any],
    n: int,
    base_next_prompt: str,
    message: str,
    *,
    settings: Any = None,
) -> str:
    """Prompt for the iteration started by an operator reply: the evaluator's last
    `next_prompt` (when there is one) plus the operator's own clarification."""
    header = f"这是目标「{goal['title']}」的第 {n} 轮。目标：{goal['outcome']}\n\n"
    if base_next_prompt:
        raw = f"{header}本轮要做：{base_next_prompt}\n\n用户补充：{message}"
    else:
        raw = f"{header}本轮要做：请按用户下面的补充继续。\n\n用户补充：{message}"
    return _append_check_script_note(raw, settings=settings, goal=goal)


def _extend_prompt(
    goal: dict[str, Any],
    n: int,
    base_next_prompt: str,
    message: str,
    *,
    settings: Any = None,
) -> str:
    """Prompt for the iteration started by `extend_goal`: builds on the evaluator's
    last `next_prompt` (if any) plus `用户补充：<message>` when given."""
    header = f"这是目标「{goal['title']}」的第 {n} 轮。目标：{goal['outcome']}\n\n"
    if base_next_prompt and message:
        raw = f"{header}本轮要做：{base_next_prompt}\n\n用户补充：{message}"
    elif base_next_prompt:
        raw = f"{header}本轮要做：{base_next_prompt}"
    elif message:
        raw = f"{header}本轮要做：请按用户下面的补充继续。\n\n用户补充：{message}"
    else:
        raw = f"{header}本轮要做：请基于前面的进展继续推进，完成目标。"
    return _append_check_script_note(raw, settings=settings, goal=goal)


def _adopt_orphan_task(settings: Any, worktree_path: str, prompt: str) -> str:
    """Crash recovery: adopt an already-active task on this worktree whose goal
    text matches the iteration prompt exactly (a task created just before a crash)."""
    try:
        tasks = ags.list_tasks(settings, limit=500)
    except Exception:
        return ""
    for task in tasks:
        if str(task.get("state") or "") not in _GOAL_ACTIVE_TASK_STATES:
            continue
        if str(task.get("goal") or "") != prompt:
            continue
        wp = str(task.get("worktree_path") or "")
        if wp and os.path.realpath(wp) != os.path.realpath(worktree_path):
            continue
        return str(task.get("id") or "")
    return ""


def _goal_is_running(settings: Any, goal_id: str) -> bool:
    try:
        return str(_get_goal_row(settings, goal_id).get("state") or "") == GOAL_RUNNING
    except Exception:
        return False


def _cancel_task_for_stopped_goal(server: Any, settings: Any, goal_id: str, task_id: str) -> None:
    if not task_id:
        return
    try:
        svc.cancel_scoped_task(server, settings, task_id, reason="目标已停止，取消本轮任务")
    except svc.SupervisorServiceError:
        log.warning("goal %s: cancel of task %s after stop failed", goal_id, task_id, exc_info=True)


def _redispatch_iteration_task(
    server: Any,
    settings: Any,
    goal: dict[str, Any],
    n: int,
    task_id: str,
) -> str:
    """Dispatch an iteration task that was created (`pending_dispatch`) but never dispatched."""
    goal_id = str(goal["id"])
    if not _goal_is_running(settings, goal_id):
        _cancel_task_for_stopped_goal(server, settings, goal_id, task_id)
        return ""
    if _deadline_passed(goal):
        _cancel_task_for_stopped_goal(server, settings, goal_id, task_id)
        _clear_iteration_task(settings, goal_id, n)
        _finish_deadline(settings, goal)
        return ""

    try:
        _repo_path, wt_path, managed = _scope_paths(settings, goal)
    except svc.SupervisorServiceError as exc:
        _fail_goal(settings, goal, str(exc.message))
        return ""
    if not managed:
        _fail_goal(settings, goal, "目标的 worktree 不再是托管状态，无法继续执行")
        return ""

    if ags.has_active_task_for_worktree(settings, wt_path, exclude_task_id=task_id):
        return ""

    roles = goal.get("role_overrides") or {}
    try:
        svc.dispatch_scoped_task(
            server,
            settings,
            task_id,
            {"roles": roles} if roles else {},
        )
    except svc.SupervisorServiceError as exc:
        log.warning("goal %s iteration %s: task re-dispatch failed: %s", goal_id, n, exc.message)
        if exc.status == HTTPStatus.CONFLICT:
            return ""
        try:
            svc.cancel_scoped_task(server, settings, task_id, reason="目标派发失败，取消本轮任务")
        except svc.SupervisorServiceError:
            log.warning("goal %s: cleanup of task %s failed", goal_id, task_id, exc_info=True)
        _clear_iteration_task(settings, goal_id, n)
        _fail_goal(settings, goal, f"第 {n} 轮任务派发失败：{exc.message}")
        return ""
    except Exception as exc:
        log.exception("goal %s iteration %s: unexpected re-dispatch error", goal_id, n)
        _fail_goal(settings, goal, f"第 {n} 轮任务派发异常：{type(exc).__name__}")
        return ""

    if not _goal_is_running(settings, goal_id):
        _cancel_task_for_stopped_goal(server, settings, goal_id, task_id)
        return ""

    log.info("goal %s iteration %s re-dispatched pending task %s", goal_id, n, task_id)
    return task_id


def _start_iteration(server: Any, settings: Any, goal: dict[str, Any], n: int, prompt: str) -> str:
    """Ensure iteration `n` has a dispatched task. Returns the task id ('' when deferred)."""
    goal_id = str(goal["id"])
    if not _goal_is_running(settings, goal_id):
        return ""

    iteration = _ensure_iteration(settings, goal_id, n, prompt)
    if iteration["task_id"]:
        existing_tid = str(iteration["task_id"])
        existing_task = ags.get_task(settings, existing_tid)
        if existing_task is not None and str(existing_task.get("state") or "") == "pending_dispatch":
            return _redispatch_iteration_task(server, settings, goal, n, existing_tid)
        return existing_tid

    fresh_goal = _get_goal_row(settings, goal_id)
    if fresh_goal["state"] != GOAL_RUNNING:
        return ""
    if _deadline_passed(fresh_goal):
        _finish_deadline(settings, fresh_goal)
        return ""

    try:
        _repo_path, wt_path, managed = _scope_paths(settings, goal)
    except svc.SupervisorServiceError as exc:
        _fail_goal(settings, goal, str(exc.message))
        return ""
    if not managed:
        _fail_goal(settings, goal, "目标的 worktree 不再是托管状态，无法继续执行")
        return ""

    # Never run two tasks of one goal at once, and never start a new task while
    # the worktree still has an active task: just wait for the next tick.
    if ags.has_active_task_for_worktree(settings, wt_path):
        adopted = _adopt_orphan_task(settings, wt_path, prompt)
        if adopted:
            _set_iteration_task(settings, goal_id, n, adopted)
            if not _goal_is_running(settings, goal_id):
                _cancel_task_for_stopped_goal(server, settings, goal_id, adopted)
                return ""
            return adopted
        return ""

    if not _goal_is_running(settings, goal_id):
        return ""

    roles = goal.get("role_overrides") or {}
    task_id = ""
    try:
        task, _created = svc.create_scoped_task(
            settings,
            {
                "goal": prompt,
                "repo_id": goal["repo_id"],
                "worktree_id": goal["worktree_id"],
            },
        )
        task_id = str(task["id"])
        _set_iteration_task(settings, goal_id, n, task_id)
        if not _goal_is_running(settings, goal_id):
            _cancel_task_for_stopped_goal(server, settings, goal_id, task_id)
            return ""
        svc.dispatch_scoped_task(
            server,
            settings,
            task_id,
            {"roles": roles} if roles else {},
        )
        if not _goal_is_running(settings, goal_id):
            _cancel_task_for_stopped_goal(server, settings, goal_id, task_id)
            return ""
    except svc.SupervisorServiceError as exc:
        log.warning("goal %s iteration %s: task dispatch failed: %s", goal_id, n, exc.message)
        if task_id:
            # Task row exists but never ran: drop it so the next tick can retry cleanly.
            try:
                svc.cancel_scoped_task(server, settings, task_id, reason="目标派发失败，取消本轮任务")
            except svc.SupervisorServiceError:
                log.warning("goal %s: cleanup of task %s failed", goal_id, task_id, exc_info=True)
            _clear_iteration_task(settings, goal_id, n)
        if exc.status == HTTPStatus.CONFLICT:
            adopted = _adopt_orphan_task(settings, wt_path, prompt)
            if adopted:
                _set_iteration_task(settings, goal_id, n, adopted)
                if not _goal_is_running(settings, goal_id):
                    _cancel_task_for_stopped_goal(server, settings, goal_id, adopted)
                    return ""
                return adopted
            return ""
        _fail_goal(settings, goal, f"第 {n} 轮任务派发失败：{exc.message}")
        return ""
    except Exception as exc:  # defensive: never let one goal kill the loop
        log.exception("goal %s iteration %s: unexpected dispatch error", goal_id, n)
        _fail_goal(settings, goal, f"第 {n} 轮任务派发异常：{type(exc).__name__}")
        return ""
    log.info("goal %s iteration %s dispatched task %s", goal_id, n, task_id)
    return task_id


# ---------------------------------------------------------------------------
# Evaluation (coordinator chat model proposes; code decides)
# ---------------------------------------------------------------------------

def _bounded(text: str, limit: int = MAX_EVAL_EXCERPT_CHARS) -> str:
    return truncate(redact_text(str(text or "")), limit)


def _task_evidence_excerpt(settings: Any, task_id: str) -> str:
    """Bounded (<=3000 chars) excerpt of task summary / acceptance / reviewer issues."""
    parts: list[str] = []
    try:
        summary_text = ags.summary(settings, task_id)
        if summary_text:
            parts.append(f"任务摘要:\n{summary_text}")
    except Exception:
        pass
    try:
        task = ags.get_task(settings, task_id) or {}
        meta = task.get("metadata") or {}
        if meta.get("changed_files"):
            parts.append("改动文件: " + ", ".join(str(f) for f in list(meta["changed_files"])[:30]))
        if task.get("error"):
            parts.append(f"任务错误: {task['error']}")
    except Exception:
        pass
    try:
        acc = ags.effective_acceptance(settings, task_id)
        stale = acc is None
        if stale:
            acc = ags.latest_acceptance(settings, task_id)
        if isinstance(acc, dict):
            issues = acc.get("issues")
            if issues is None and isinstance(acc.get("issues_json"), str):
                try:
                    decoded = json.loads(acc["issues_json"])
                    issues = decoded if isinstance(decoded, list) else []
                except Exception:
                    issues = []
            if not isinstance(issues, list):
                issues = []
            passed = bool(acc.get("passed"))
            parts.append(
                ("验收已失效，历史结果: " if stale else "验收: ")
                + ("通过" if passed else "未通过")
                + (f"；问题: {'; '.join(str(i) for i in issues)}" if issues else "")
            )
    except Exception:
        pass
    try:
        handoffs = ags.list_handoffs(settings, task_id)
        reviewer_notes = [
            str(h.get("summary") or "")
            for h in handoffs[-6:]
            if str(h.get("to_role") or "") in ("reviewer", "user", "implementer")
        ]
        reviewer_notes = [n for n in reviewer_notes if n]
        if reviewer_notes:
            parts.append("最近的交接/评审记录:\n" + "\n".join(f"- {n}" for n in reviewer_notes))
    except Exception:
        pass
    return _bounded("\n\n".join(parts))


def _build_eval_prompt(
    goal: dict[str, Any],
    iterations: list[dict[str, Any]],
    settings: Any,
    retry_message: str | None = None,
) -> str:
    is_continuous = str(goal.get("mode") or GOAL_MODE_OUTCOME) == GOAL_MODE_CONTINUOUS
    lines: list[str] = [
        "你是 Conveyor Agent Supervisor 的目标评估器（coordinator）。",
        "请根据「目标」和「历史轮次」判断最新一轮的结果并决定下一步。",
        "只输出一个 JSON 对象：不要输出任何解释文字，不要使用 Markdown 代码块。",
        'JSON 字段：{"decision": "continue"|"achieved"|"ask_user"|"give_up", '
        '"reason": "...", "next_prompt": "...", "question": "...", "summary": "...", '
        '"round_title": "...", "lessons": ["..."]}',
        "- decision=achieved 仅当最新一轮任务状态为 completed 且本轮检查/验收确实通过；",
        "- decision=continue 必须在 next_prompt 里给出下一轮的具体中文指令；",
        "- decision=ask_user 必须在 question 里给出需要用户决策的中文问题；",
        "- decision=give_up 表示目标无法达成，reason 说明原因；",
        "- round_title 是一句简短中文（<=40字），概括最新一轮完成了什么改进（仅当本轮通过时填写）；",
        "- summary 是一段中文进展摘要（当前进展、还差什么）；",
        "- lessons（可选）：最多 3 条简短字符串（每条 <= 200 字），记录本轮学到的关于这个仓库的长期事实（测试怎么跑、代码约定、常见坑），不要写任务进度。",
        "",
        f"目标标题：{goal['title']}",
        f"运行模式：{'持续改进（每次通过检查后继续做下一个最有价值的小改进，直到截止时间）' if is_continuous else '达标即停'}",
    ]
    if retry_message:
        lines.append("")
        lines.append("--- 评估出错，请重试 ---")
        lines.append(retry_message)
        lines.append("-------------------------")

    if goal.get("deadline_at"):
        lines.append(f"截止时间（UTC）：{goal['deadline_at']}")
    lines.extend([
        f"最大轮次：{goal['max_iterations']}（已用 {len(iterations)} 轮）",
        "目标（用户期望的结果）：",
        goal["outcome"],
        "",
        "历史轮次：",
    ])
    for item in iterations[-MAX_EVAL_ITERATIONS:]:
        n = item["n"]
        lines.append(f"--- 第 {n} 轮 ---")
        lines.append("本轮指令：")
        lines.append(_bounded(item["prompt"], MAX_EVAL_EXCERPT_CHARS))
        if item["task_id"]:
            task = ags.get_task(settings, item["task_id"]) or {}
            lines.append(
                f"任务状态：{task.get('state') or '(未知)'} / 阶段：{task.get('stage') or '(未知)'}"
            )
            excerpt = _task_evidence_excerpt(settings, item["task_id"])
            if excerpt:
                lines.append(excerpt)
        else:
            lines.append("任务状态：尚未派发")
        lines.append("")
    return "\n".join(lines)


def _strip_code_fences(text: str) -> str:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
        if cleaned[:4].lower() == "json":
            cleaned = cleaned[4:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip()


def _parse_decision(raw_output: Any) -> dict[str, Any] | None:
    """Robustly parse the evaluator's JSON object (fences / prose tolerated)."""
    if not isinstance(raw_output, str):
        return None
    text = _strip_code_fences(raw_output)
    start = text.find("{")
    if start < 0:
        return None
    decoder = json.JSONDecoder()
    for candidate in (text[start:], text):
        try:
            obj, _end = decoder.raw_decode(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _normalize_decision(parsed: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        return {
            "decision": "",
            "reason": "",
            "next_prompt": "",
            "question": "",
            "summary": "",
            "round_title": "",
            "lessons": [],
        }
    raw_decision = str(parsed.get("decision") or "").strip().lower()
    if raw_decision not in DECISIONS:
        raw_decision = ""

    def _text(key: str) -> str:
        value = parsed.get(key)
        if value is None:
            return ""
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        return redact_text(value).strip()

    raw_lessons = parsed.get("lessons")
    lessons: list[Any] = list(raw_lessons) if isinstance(raw_lessons, list) else []

    result = {
        "decision": raw_decision,
        "reason": _text("reason"),
        "next_prompt": _text("next_prompt"),
        "question": _text("question"),
        "summary": _text("summary"),
        "round_title": _text("round_title"),
        "lessons": lessons,
    }
    if "_raw_output" in parsed:
        result["_raw_output"] = parsed["_raw_output"]
    return result


def _derive_round_title(
    decision: dict[str, Any],
    iteration: dict[str, Any],
    task: dict[str, Any],
    n: int,
) -> str:
    candidate = str(decision.get("round_title") or "").strip()
    if not candidate:
        raw_summary = str(decision.get("summary") or "").strip()
        if raw_summary and not raw_summary.startswith(f"第 {n} 轮已结束"):
            first_line = raw_summary.splitlines()[0].strip()
            candidate = first_line
    if not candidate:
        prompt_text = str(iteration.get("prompt") or "")
        marker = "本轮要做："
        idx = prompt_text.find(marker)
        if idx >= 0:
            candidate = prompt_text[idx + len(marker):].strip().splitlines()[0].strip()
    if not candidate:
        goal_text = str(task.get("goal") or "").strip()
        if goal_text:
            candidate = goal_text.splitlines()[0].strip()
    if not candidate:
        candidate = f"第 {n} 轮改进"
    return _display_title(candidate)


# Executions the Supervisor runs itself (repo checks), not a model executor.
# Their error text is command output: tracebacks and random worktree paths
# ("sup-<ms-timestamp>-<hex>"), which must never be read as a quota message.
NON_PROVIDER_EXECUTORS = frozenset({"operator-validation"})


def _task_failure_texts(
    settings: Any, task: dict[str, Any], *, provider_only: bool = False
) -> list[str]:
    """Texts that may explain a failed round.

    ``provider_only`` keeps only what a model executor itself reported (the task
    error plus its executions' errors, minus the Supervisor's own repo checks).
    Repo check output and reviewer issues quote tracebacks and random paths, so
    they are not evidence about the model's quota.
    """
    texts: list[str] = []
    err = str(task.get("error") or "").strip()
    if err:
        texts.append(err)
    task_id = str(task.get("id") or "")
    if task_id:
        try:
            for ex in ags.list_executions(settings, task_id=task_id, limit=10):
                if provider_only and str(ex.get("executor") or "") in NON_PROVIDER_EXECUTORS:
                    continue
                for key in ("error", "error_message", "stderr"):
                    val = str(ex.get(key) or "").strip()
                    if val:
                        texts.append(val)
        except Exception:
            pass
        if provider_only:
            return texts
        try:
            acc = ags.latest_acceptance(settings, task_id)
            if isinstance(acc, dict):
                issues = acc.get("issues")
                if issues is None and isinstance(acc.get("issues_json"), str):
                    try:
                        decoded = json.loads(acc["issues_json"])
                        issues = decoded if isinstance(decoded, list) else []
                    except Exception:
                        issues = []
                if isinstance(issues, list):
                    for item in issues:
                        s = str(item or "").strip()
                        if s:
                            texts.append(s)
        except Exception:
            pass
    return texts


def _load_workers_state_for_clock(settings: Any) -> dict[str, Any]:
    """Read workers state, preferring raw JSON file so patched `_now()` comparisons stay accurate."""
    try:
        state = workers.load_workers_state(settings)
    except Exception:
        state = {}
    try:
        state_path = workers.get_state_path(settings)
        if state_path.is_file():
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                merged = dict(state)
                if isinstance(raw.get("cooling"), dict):
                    merged["cooling"] = raw["cooling"]
                if isinstance(raw.get("thresholds"), dict):
                    merged["thresholds"] = raw["thresholds"]
                if isinstance(raw.get("enabled"), list):
                    merged["enabled"] = raw["enabled"]
                return merged
    except Exception:
        pass
    return state


def _has_cooling_or_over_threshold_worker(settings: Any, now_ts: float) -> bool:
    state = _load_workers_state_for_clock(settings)
    cooling = state.get("cooling") or {}
    if isinstance(cooling, dict):
        for entry in cooling.values():
            if not isinstance(entry, dict):
                continue
            try:
                if float(entry.get("reset_at") or 0) > now_ts:
                    return True
            except (TypeError, ValueError):
                continue
    thresholds = state.get("thresholds") or {}
    enabled = state.get("enabled") or worker_usage.USAGE_WORKER_IDS
    for wid in enabled:
        try:
            raw_thresh = thresholds.get(str(wid)) if isinstance(thresholds, dict) else None
            threshold = int(raw_thresh) if raw_thresh is not None else 100
            usage = worker_usage.get_usage(str(wid))
            if workers.usage_over_threshold(usage, threshold, now_ts):
                return True
        except Exception:
            continue
    return False


def _is_quota_failure(settings: Any, task: dict[str, Any]) -> bool:
    """Whether a failed round failed because the model pool is out of quota.

    Quota is a fact about the pool, so it is only inferred from structured pool
    signals (the pool's own markers, cooling / over-threshold workers) or from a
    provider's own quota message. Arbitrary failure text — repo check output,
    tracebacks, paths — is never enough.
    """
    task_state = str(task.get("state") or "")
    if task_state not in ("failed", "blocked"):
        return False
    texts = _task_failure_texts(settings, task)
    if not texts:
        return False
    now_ts = _now().timestamp()
    for text in texts:
        # Structured pool signals: the pool writes these itself, so they cannot
        # show up by accident inside a check output or a random worktree path.
        if worker_pool.ALL_QUOTA_PREFIX in text:
            return True
        if "冷却中" in text or "用量阈值" in text or "over_threshold" in text:
            return True
        if worker_pool.NO_WORKER_REASON in text and _has_cooling_or_over_threshold_worker(settings, now_ts):
            return True
    # A provider quota message is only read from what a model executor itself
    # reported. `workers.classify_failure` is a blunt keyword match (its bare
    # `429` also sits inside `sup-<ms-timestamp>-<hex>` worktree names and
    # timestamps), so running it over repo-check output would pause a healthy
    # goal as if the pool were out of quota.
    for text in _task_failure_texts(settings, task, provider_only=True):
        if workers.classify_failure(text) is not None:
            return True
    return False


def _earliest_quota_reset(settings: Any, task: dict[str, Any]) -> datetime:
    now_dt = _now()
    now_ts = now_dt.timestamp()
    candidates: list[float] = []

    state = _load_workers_state_for_clock(settings)
    cooling = state.get("cooling") or {}
    if isinstance(cooling, dict):
        for entry in cooling.values():
            if not isinstance(entry, dict):
                continue
            try:
                reset_ts = float(entry.get("reset_at") or 0)
            except (TypeError, ValueError):
                continue
            if reset_ts > now_ts:
                candidates.append(reset_ts)

    for text in _task_failure_texts(settings, task, provider_only=True):
        parsed_sec = workers.parse_reset_seconds(text)
        if parsed_sec is not None and parsed_sec > 0:
            candidates.append(now_ts + float(parsed_sec))

    thresholds = state.get("thresholds") or {}
    enabled = state.get("enabled") or worker_usage.USAGE_WORKER_IDS
    for wid in enabled:
        try:
            usage = worker_usage.get_usage(str(wid))
            raw_thresh = thresholds.get(str(wid)) if isinstance(thresholds, dict) else None
            threshold = int(raw_thresh) if raw_thresh is not None else None
            for window in (usage or {}).get("windows") or []:
                reset_raw = window.get("resets_at")
                if not isinstance(reset_raw, (int, float)) or isinstance(reset_raw, bool):
                    continue
                if float(reset_raw) <= now_ts:
                    continue
                used = window.get("used_percent")
                if threshold is not None and isinstance(used, (int, float)) and used < threshold:
                    continue
                candidates.append(float(reset_raw))
        except Exception:
            continue

    if candidates:
        return datetime.fromtimestamp(min(candidates), tz=timezone.utc)
    return now_dt + timedelta(minutes=DEFAULT_QUOTA_PAUSE_MINUTES)


def _format_relative_resume_zh(resume_dt: datetime, *, now: datetime | None = None) -> str:
    """Server-safe relative pause summary for `waiting_quota` (never uses server clock HH:MM)."""
    ref = now or _now()
    seconds = max(0.0, (resume_dt - ref).total_seconds())
    minutes = max(1, int(round(seconds / 60.0))) if seconds > 0 else 0
    if minutes <= 0:
        return "模型额度暂时用完，稍后会自动继续。"
    if minutes < 60:
        return f"模型额度暂时用完，大约 {minutes} 分钟后自动继续。"
    hours, rem = divmod(minutes, 60)
    if rem == 0:
        return f"模型额度暂时用完，大约 {hours} 小时后自动继续。"
    return f"模型额度暂时用完，大约 {hours} 小时 {rem} 分后自动继续。"


def _resume_waiting_quota_goal(settings: Any, goal_id: str) -> bool:
    now = _utc_now()
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """UPDATE supervisor_goals
               SET state = ?, resume_at = NULL, updated_at = ?
               WHERE id = ? AND state = ?""",
            (GOAL_RUNNING, now, goal_id, GOAL_WAITING_QUOTA),
        )
        resumed = cur.rowcount == 1
        if resumed:
            conn.commit()
        else:
            conn.rollback()
        return resumed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _resume_due_waiting_quota(settings: Any) -> list[str]:
    """Check every `waiting_quota` goal and resume those whose pause is due.

    Driven directly by each tick of the existing goal loop
    (`SupervisorWorkerManager` → `advance_goals`, every 0.5 s) using the
    persisted `resume_at` column, so a pause survives a server restart with
    nothing extra to recreate.

    - deadline already passed → finish with `stop_reason='deadline'`;
    - `resume_at` reached (or missing) → back to `running`, the next tick
      re-dispatches the same round (its task id was cleared when it paused);
    - otherwise the goal keeps waiting; its id is returned.
    """
    if settings is None or not svc.is_enabled(settings):
        return []
    pending: list[str] = []
    for goal in _waiting_quota_goals(settings):
        goal_id = str(goal["id"])
        if _deadline_passed(goal):
            _finish_deadline(settings, goal)
            continue
        resume_dt = _parse_utc(goal.get("resume_at"))
        if resume_dt is None or _now() >= resume_dt:
            if _resume_waiting_quota_goal(settings, goal_id):
                log.info("goal %s: resumed from waiting_quota", goal_id)
        else:
            pending.append(goal_id)
    return pending


def recover_on_startup(server: Any, settings: Any) -> list[str]:
    """Startup pass for `waiting_quota` goals (called once by `SupervisorWorkerManager`).

    Overdue pauses resume and expired deadlines finish right away; the rest
    keep waiting via their stored `resume_at`, which every later tick checks.
    `running` goals need nothing here: the same tick resumes them as before.
    Returns the ids of goals that are still waiting for quota.
    """
    if settings is None or not svc.is_enabled(settings):
        return []
    try:
        pending = _resume_due_waiting_quota(settings)
    except Exception:
        log.exception("goal quota pause recovery failed")
        return []
    if pending:
        log.info("goal loop: %d goal(s) still waiting for model quota after restart", len(pending))
    return pending


def _maybe_handle_quota_failure(
    server: Any,
    settings: Any,
    goal: dict[str, Any],
    iteration: dict[str, Any],
    iterations: list[dict[str, Any]],
    task: dict[str, Any],
) -> bool:
    goal_id = str(goal["id"])
    n = int(iteration["n"])
    fresh_goal = _get_goal_row(settings, goal_id)
    if fresh_goal["state"] != GOAL_RUNNING:
        # Stopped / answered meanwhile: the normal evaluation path records that the
        # round's result was discarded (and keeps the round's task id for the diff).
        return False
    task_id = str(iteration.get("task_id") or "")
    fresh_task = (ags.get_task(settings, task_id) if task_id else None) or task
    if not _is_quota_failure(settings, fresh_task):
        return False

    if _deadline_passed(fresh_goal):
        _record_decision(
            settings,
            goal_id,
            n,
            decision=DECISION_CONTINUE,
            reason="已到截止时间",
            task_state=str(fresh_task.get("state") or ""),
            passed=False,
        )
        _finish_deadline(settings, fresh_goal, _iterations(settings, goal_id))
        return True

    resume_dt = _earliest_quota_reset(settings, fresh_task)
    deadline_dt = _parse_utc(fresh_goal.get("deadline_at"))
    if deadline_dt is not None and resume_dt >= deadline_dt:
        _record_decision(
            settings,
            goal_id,
            n,
            decision=DECISION_GIVE_UP,
            reason="模型额度用完，下次可用时间已超过截止时间",
            task_state=str(fresh_task.get("state") or ""),
            passed=False,
        )
        _finish_goal(
            settings,
            fresh_goal,
            GOAL_EXHAUSTED,
            summary=QUOTA_EXCEEDED_DEADLINE_SUMMARY,
            stop_reason=STOP_REASON_QUOTA,
        )
        return True

    # Pause without burning a round. Only a goal that is still `running` may move
    # to `waiting_quota` (never overwrite a stop the owner just made).
    resume_iso = _format_utc(resume_dt)
    relative_summary = _format_relative_resume_zh(resume_dt)
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """UPDATE supervisor_goals
               SET state = ?, resume_at = ?, question = '', stop_reason = '',
                   summary = ?, updated_at = ?
               WHERE id = ? AND state = ?""",
            (
                GOAL_WAITING_QUOTA,
                resume_iso,
                relative_summary,
                _utc_now(),
                goal_id,
                GOAL_RUNNING,
            ),
        )
        paused = cur.rowcount == 1
        if paused:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if not paused:
        return False
    # Clear the round's task id so the same `n` and prompt are retried on resume.
    _clear_iteration_for_retry(settings, goal_id, n)
    log.info("goal %s: paused in waiting_quota until %s", goal_id, resume_iso)
    return True


def _store_goal_lessons(settings: Any, goal: dict[str, Any], raw_lessons: Any) -> None:
    """Persist up to 3 short repository lessons when a goal ends as achieved.

    Model output derived from repository content is marked ``source="untrusted"``.
    Duplicates, cap overflows, secret rejections, and storage errors are skipped
    silently (logging a redacted warning) and never fail the goal.
    """
    if not isinstance(raw_lessons, list) or not raw_lessons:
        return
    repo_id = str(goal.get("repo_id") or "").strip()
    if not repo_id:
        return
    try:
        from conveyor.agents import agent_memory
    except Exception:
        return
    if getattr(settings, "agent_notes_enabled", None) is False:
        return
    scope = f"{agent_memory.REPO_PREFIX}{repo_id}"
    if len(raw_lessons) > MAX_LESSON_ITEMS:
        log.warning(
            "goal %s: evaluator returned %d lessons; capping to %d",
            goal.get("id"),
            len(raw_lessons),
            MAX_LESSON_ITEMS,
        )
    seen: set[str] = set()
    count = 0
    for item in raw_lessons:
        if count >= MAX_LESSON_ITEMS:
            break
        if not isinstance(item, str):
            continue
        count += 1
        text = item.strip()
        if not text:
            continue
        if len(text) > MAX_LESSON_CHARS:
            log.warning(
                "goal %s: skipping lesson over %d chars (%d chars): %s",
                goal.get("id"),
                MAX_LESSON_CHARS,
                len(text),
                truncate(redact_text(text), 120),
            )
            continue
        norm = " ".join(text.split())
        if norm in seen:
            continue
        seen.add(norm)
        try:
            agent_memory.add_note(settings, scope, text, source="untrusted")
        except agent_memory.AgentMemoryError as exc:
            log.warning(
                "goal %s: skipping lesson (%s): %s",
                goal.get("id"),
                exc.code,
                truncate(redact_text(text), 120),
            )
        except Exception as exc:
            log.warning(
                "goal %s: failed to store lesson (%s)",
                goal.get("id"),
                type(exc).__name__,
            )


def _prepare_evaluator_call(
    settings: Any,
    goal: dict[str, Any],
    iterations: list[dict[str, Any]],
    retry_message: str | None = None,
) -> tuple[bool, str, Any, Any, str, int]:
    roles = ags.load_roles(settings)
    coordinator = roles["coordinator"]
    executor = ags.get_executor(settings, "chat")
    ok, reason = executor.available()
    if not ok:
        return False, str(reason or ""), None, None, "", 0
    prompt = _build_eval_prompt(goal, iterations, settings, retry_message=retry_message)
    timeout = max(30, min(int(ags.task_timeout(settings)), 600))
    return True, "", executor, coordinator, prompt, timeout


async def _to_thread(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run a synchronous goal/DB step in a worker thread.

    If the caller task is cancelled while the thread step is in flight, wait for
    it to finish before propagating ``CancelledError`` so ``SupervisorWorkerManager.stop()``
    never returns while SQLite writes are still active.
    """
    fut = asyncio.ensure_future(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        while not fut.done():
            try:
                await asyncio.shield(fut)
            except asyncio.CancelledError:
                pass
            except BaseException:
                break
        raise


async def _ask_evaluator(
    settings: Any,
    goal: dict[str, Any],
    iterations: list[dict[str, Any]],
    *,
    worktree_path: str,
    repo_path: str,
    task_id: str,
    retry_message: str | None = None,
) -> dict[str, str]:
    """Ask the coordinator chat model for a decision. Never raises."""
    empty = _normalize_decision(None)
    try:
        ok, reason, executor, coordinator, prompt, timeout = await _to_thread(
            _prepare_evaluator_call,
            settings,
            goal,
            iterations,
            retry_message=retry_message,
        )
        if not ok:
            log.warning(
                "goal %s: evaluator chat unavailable: %s",
                goal["id"],
                truncate(redact_text(str(reason or "")), MAX_REASON_CHARS),
            )
            return empty
        ctx = ags.ExecutionContext(
            task_id=task_id,
            repo_path=repo_path,
            worktree_path=worktree_path,
            stage="goal_evaluation",
            metadata={"goal_id": goal["id"]},
        )
        result = await ags._invoke_role_executor(
            executor,
            role=coordinator,
            prompt=prompt,
            worktree_path=worktree_path,
            timeout_seconds=timeout,
            task_id=task_id,
            context=ctx,
        )
    except Exception as exc:
        log.warning("goal %s: evaluator call failed: %s", goal["id"], type(exc).__name__)
        return empty
    if not getattr(result, "success", False):
        safe_error = truncate(redact_text(str(getattr(result, "error", "") or "")), MAX_REASON_CHARS)
        log.warning("goal %s: evaluator returned failure: %s", goal["id"], safe_error)
        return empty
    parsed = _parse_decision(getattr(result, "output", ""))
    if parsed is None:
        log.warning("goal %s: evaluator output was not parsable JSON", goal["id"])
        # Return the actual output for retry logic
        return _normalize_decision({"_raw_output": getattr(result, "output", "")})
    return _normalize_decision(parsed)


def _stage_eval_fields(
    settings: Any,
    goal_id: str,
    *,
    summary: str = "",
    question: str | None = None,
    last_next_prompt: str | None = None,
) -> None:
    """Persist evaluation metadata on the goal row before `_record_decision` commits,
    so `_reapply_decision` has full fidelity if a later write fails."""
    updates: dict[str, Any] = {
        "summary": truncate(redact_text(summary), MAX_SUMMARY_CHARS),
        "updated_at": _utc_now(),
    }
    if question is not None:
        updates["question"] = truncate(redact_text(question), MAX_QUESTION_CHARS)
    if last_next_prompt is not None:
        updates["last_next_prompt"] = truncate(redact_text(last_next_prompt), MAX_NEXT_PROMPT_CHARS)
    _update_goal(settings, goal_id, **updates)


def _completion_is_current(settings: Any, task_id: str) -> bool:
    """A finished task can certify a goal only while its accepted snapshot matches."""
    if not task_id:
        return False
    try:
        task = ags.get_task(settings, task_id)
        if not task or task.get("state") != "completed":
            return False
        acceptance = ags.effective_acceptance(settings, task_id)
        if not acceptance or acceptance.get("passed") is not True:
            return False
        if (acceptance.get("evidence_source") != "operator_validation+independent_review"
                or acceptance.get("provenance_verified") is not True):
            return False
        checks = acceptance.get("executed_checks")
        return bool(isinstance(checks, list) and checks and all(
            isinstance(check, dict) and check.get("passed") is True
            and bool(check.get("execution_id"))
            and check.get("target_sha") == acceptance.get("sha")
            and check.get("target_fingerprint") == acceptance.get("fingerprint")
            for check in checks
        ))
    except Exception:
        log.exception("goal completion acceptance check failed for task %s", task_id)
        return False


_STALE_ACCEPTANCE_REASON = "本轮验收已失效或无法确认，当前代码需要重新验证。"
_STALE_ACCEPTANCE_NEXT = "上一次验收后代码可能发生变化。检查当前工作目录和目标，保留有效改动，重新运行必要验证并完成独立审阅。"


def _advance_to_next_iteration_or_stop(
    server: Any,
    settings: Any,
    goal: dict[str, Any],
    *,
    next_n: int,
    summary: str,
    next_prompt: str,
    iterations: list[dict[str, Any]],
    round_passed: bool,
) -> None:
    goal_id = str(goal["id"])
    fresh_goal = _get_goal_row(settings, goal_id)
    if fresh_goal["state"] != GOAL_RUNNING:
        return
    if _deadline_passed(fresh_goal):
        _finish_deadline(settings, fresh_goal, iterations)
        return

    late_note = _late_round_note(fresh_goal, iterations)
    if late_note:
        _finish_deadline(settings, fresh_goal, iterations, note=late_note)
        return

    used = len(iterations)
    max_iterations = int(fresh_goal["max_iterations"])
    if used >= max_iterations:
        if next_prompt and len(next_prompt) <= MAX_NEXT_PROMPT_CHARS:
            _update_goal(settings, goal_id, last_next_prompt=next_prompt)
        _finish_goal(
            settings,
            fresh_goal,
            GOAL_EXHAUSTED,
            summary=summary,
            stop_reason=f"迭代预算已用完（{max_iterations} 轮），目标尚未达成",
        )
        return

    is_continuous = str(fresh_goal.get("mode") or GOAL_MODE_OUTCOME) == GOAL_MODE_CONTINUOUS
    if is_continuous and round_passed:
        prompt = _continuous_next_prompt(
            fresh_goal, next_n, iterations, next_suggestion=next_prompt, settings=settings
        )
    else:
        prompt = _continuation_prompt(fresh_goal, next_n, next_prompt, settings=settings)

    updates: dict[str, Any] = {
        "summary": truncate(redact_text(summary), MAX_SUMMARY_CHARS),
        "updated_at": _utc_now(),
    }
    if next_prompt and len(next_prompt) <= MAX_NEXT_PROMPT_CHARS:
        updates["last_next_prompt"] = next_prompt
    _update_goal(settings, goal_id, **updates)
    _start_iteration(server, settings, fresh_goal, next_n, prompt)
    _maybe_post_progress_digest(settings, _get_goal_row(settings, goal_id), iterations)


_INFRA_ERROR_KINDS = frozenset({
    "pi_length",
    "length",
    "max_tokens",
    "executor_timeout",
    "timeout",
    "quota",
    "rate_limit",
    "executor_unavailable",
    "unavailable",
    "runtime_error",
    "lock_error",
    "unexpected_error",
    "worktree_unavailable",
    "executor_error",
    "runtime",
    "review_format",
})
_NON_INFRA_ERROR_KINDS = frozenset({
    "code_error",
    "validation_failed",
    "review_rejected",
    "worktree_mutated",
    "invalid_input",
    "invalid_config",
})
_INFRA_EXECUTION_SUBTYPES = frozenset({
    "timeout",
    "aborted",
    "missing_result",
    "malformed_result",
    "ambiguous_result",
    "stream_error",
    "parser_exception",
})
_RUNTIME_ERROR_MARKERS = (
    "unavailable",
    "timed out",
    "timeout",
    "failed to spawn",
    "rate limit",
    "quota",
    "usage limit",
    "output byte limit",
    "analysis failed:",
    "cannot resolve",
    "lock",
)
_NON_RUNTIME_SUPERVISOR_MARKERS = (
    "changed during",
    "mutated worktree",
    "discussion_snapshot",
    "unsupported location",
    "unsupported tool",
    "invalid model",
    "cannot be configured with write",
)


def _execution_is_infra_failure(execution: dict[str, Any]) -> bool:
    if not isinstance(execution, dict):
        return False
    status = str(execution.get("status") or "").strip().lower()
    if status == "timeout":
        return True
    meta = execution.get("meta")
    if not isinstance(meta, dict):
        meta = _decode_json_object(execution.get("metadata_json"))
    err_kind = str(meta.get("error_kind") or "").strip().lower()
    if err_kind in _INFRA_ERROR_KINDS:
        return True
    if err_kind in _NON_INFRA_ERROR_KINDS:
        return False
    if str(meta.get("stop_reason") or "").strip().lower() == "length":
        return True
    if str(meta.get("finished_reason") or "").strip().lower() in ("length", "max_tokens"):
        return True
    subtype = str(meta.get("subtype") or "").strip().lower()
    if status == "failed" and subtype in _INFRA_EXECUTION_SUBTYPES:
        return True
    err = str(execution.get("error") or "").strip().lower()
    if status == "failed" and err and any(tok in err for tok in _RUNTIME_ERROR_MARKERS):
        return True
    return False


def _latest_round_has_failed_validation(settings: Any, task_id: str) -> bool:
    if not task_id or settings is None or not getattr(settings, "codex_memory_root", ""):
        return False
    try:
        acc = ags.latest_acceptance(settings, task_id)
    except Exception:
        return False
    if not isinstance(acc, dict):
        return False
    checks = acc.get("executed_checks")
    if checks is None and isinstance(acc.get("executed_checks_json"), str):
        try:
            decoded = json.loads(acc["executed_checks_json"])
            checks = decoded if isinstance(decoded, list) else []
        except Exception:
            checks = []
    if isinstance(checks, list) and any(isinstance(c, dict) and not c.get("passed") for c in checks):
        return True
    return False


def _is_infrastructure_failure(
    settings: Any,
    task: dict[str, Any],
    executions: list[dict[str, Any]] | None = None,
) -> bool:
    """Classify from structured task/execution fields whether a failed round was
    caused by the executor/runtime (not the code). Never matches Chinese text."""
    if not isinstance(task, dict):
        return False
    state = str(task.get("state") or "").strip().lower()
    if state not in ("failed", "blocked"):
        return False

    meta = task.get("metadata")
    if not isinstance(meta, dict):
        meta = _decode_json_object(task.get("metadata_json"))
    error_kind = str(meta.get("error_kind") or "").strip().lower()
    if error_kind in _NON_INFRA_ERROR_KINDS:
        return False
    if error_kind in _INFRA_ERROR_KINDS:
        return True

    if _is_quota_failure(settings, task):
        return True

    stage = str(task.get("stage") or "").strip().lower()
    if stage == "unexpected_error":
        return True

    task_id = str(task.get("id") or "").strip()
    if executions is None:
        if task_id and settings is not None and getattr(settings, "codex_memory_root", ""):
            try:
                executions = ags.list_executions(settings, task_id=task_id, limit=20)
            except Exception:
                executions = []
        else:
            executions = []

    provider_execs = [
        ex for ex in executions
        if isinstance(ex, dict) and str(ex.get("executor") or "").strip().lower() not in NON_PROVIDER_EXECUTORS
    ]
    latest_provider_infra = bool(provider_execs) and _execution_is_infra_failure(provider_execs[0])

    if stage == "repair_limit_exceeded":
        if _latest_round_has_failed_validation(settings, task_id):
            return False
        return latest_provider_infra

    if latest_provider_infra:
        return True

    task_error = str(task.get("error") or "").strip().lower()
    if stage == "supervisor_error":
        if any(tok in task_error for tok in _NON_RUNTIME_SUPERVISOR_MARKERS):
            return False
        return any(tok in task_error for tok in _RUNTIME_ERROR_MARKERS) or "worktree" in task_error

    if stage in ("analysis", "implementation"):
        return any(tok in task_error for tok in _RUNTIME_ERROR_MARKERS)

    return False


def _last_failure_reason(
    settings: Any,
    task: dict[str, Any],
    decision: dict[str, Any],
) -> str:
    reason = str(decision.get("reason") or "").strip()
    if reason:
        return reason
    summary = str(decision.get("summary") or "").strip()
    if summary and not (summary.startswith("第 ") and "任务状态：" in summary):
        return summary
    task_id = str(task.get("id") or "").strip()
    if task_id and settings is not None and getattr(settings, "codex_memory_root", ""):
        try:
            acc = ags.latest_acceptance(settings, task_id)
        except Exception:
            acc = None
        if isinstance(acc, dict):
            issues = acc.get("issues")
            if isinstance(issues, list):
                joined = "；".join(str(i).strip() for i in issues if str(i or "").strip())
                if joined:
                    return joined
    err = str(task.get("error") or "").strip()
    if err:
        return err
    return ""


def _continuous_failures_question(
    settings: Any,
    task: dict[str, Any],
    decision: dict[str, Any],
) -> str:
    detail = _clip_report_detail(_last_failure_reason(settings, task, decision), 200)
    if detail:
        return f"最近 {CONTINUOUS_CONSECUTIVE_FAILURE_LIMIT} 轮都没通过检查：{detail}"
    return CONTINUOUS_FAILURES_QUESTION


def _reapply_decision(
    server: Any,
    settings: Any,
    goal: dict[str, Any],
    iteration: dict[str, Any],
    iterations: list[dict[str, Any]],
) -> None:
    """Idempotently finish applying a stored iteration decision when a goal is still
    `running` (recovery after a failure between `_record_decision` and goal transition)."""
    goal_id = str(goal["id"])
    goal = _get_goal_row(settings, goal_id)
    if goal["state"] != GOAL_RUNNING:
        return

    n = int(iteration["n"])
    is_continuous = str(goal.get("mode") or GOAL_MODE_OUTCOME) == GOAL_MODE_CONTINUOUS
    stored_decision = str(iteration.get("decision") or "").strip().lower()
    reason = str(iteration.get("decision_reason") or "")
    state = str(iteration.get("task_final_state") or "")
    if not state and iteration.get("task_id"):
        task = ags.get_task(settings, str(iteration["task_id"])) or {}
        state = str(task.get("state") or "")
    summary = str(goal.get("summary") or "") or f"第 {n} 轮已结束，任务状态：{state}。"

    if state == "cancelled" and not stored_decision:
        _finish_goal(
            settings,
            goal,
            GOAL_STOPPED,
            summary=summary,
            stop_reason="本轮任务被取消，目标已停止",
        )
        return

    if stored_decision == DECISION_ACHIEVED:
        task_id = str(iteration.get("task_id") or "")
        task = ags.get_task(settings, task_id) if task_id else None
        if task is None:
            if _deadline_passed(goal):
                _finish_deadline(settings, goal, iterations)
                return
            _fail_goal(settings, goal, f"第 {n} 轮的任务记录缺失，无法确认目标结果")
            return
        if not _completion_is_current(settings, task_id):
            _stage_eval_fields(settings, goal_id, summary=_STALE_ACCEPTANCE_REASON,
                               last_next_prompt=_STALE_ACCEPTANCE_NEXT)
            _record_decision(settings, goal_id, n, decision=DECISION_CONTINUE,
                             reason=_STALE_ACCEPTANCE_REASON, task_state=state)
            updated = dict(iteration, decision=DECISION_CONTINUE, decision_reason=_STALE_ACCEPTANCE_REASON)
            _reapply_decision(server, settings, _get_goal_row(settings, goal_id), updated, iterations)
            return
    if stored_decision == DECISION_ACHIEVED and not is_continuous:
        _finish_goal(
            settings,
            goal,
            GOAL_ACHIEVED,
            summary=summary,
            stop_reason=reason or "评估器判断目标已达成",
        )
        return

    if _deadline_passed(goal):
        _finish_deadline(settings, goal, iterations)
        return

    if stored_decision == DECISION_GIVE_UP:
        _finish_goal(
            settings,
            goal,
            GOAL_FAILED,
            summary=summary,
            stop_reason=reason or "评估器判断目标无法达成",
        )
        return

    if stored_decision == DECISION_ASK_USER:
        question = str(goal.get("question") or "") or reason or EVALUATOR_UNAVAILABLE_QUESTION
        _finish_goal(settings, goal, GOAL_WAITING_USER, summary=summary, question=question)
        return

    if stored_decision == DECISION_CONTINUE or (stored_decision == DECISION_ACHIEVED and is_continuous):
        if _deadline_passed(goal):
            _finish_deadline(settings, goal, iterations)
            return

        used = len(iterations)
        max_iterations = int(goal["max_iterations"])
        if used >= max_iterations:
            _finish_goal(
                settings,
                goal,
                GOAL_EXHAUSTED,
                summary=summary,
                stop_reason=f"迭代预算已用完（{max_iterations} 轮），目标尚未达成",
            )
            return

        round_passed = bool(iteration.get("passed")) or (state == "completed" and is_continuous)
        next_prompt = str(goal.get("last_next_prompt") or "").strip()
        if not (is_continuous and round_passed) and (not next_prompt or len(next_prompt) > MAX_NEXT_PROMPT_CHARS):
            _stage_eval_fields(settings, goal_id, summary=summary, question=NO_NEXT_PROMPT_QUESTION)
            _record_decision(
                settings,
                goal_id,
                n,
                decision=DECISION_ASK_USER,
                reason="continue 缺少可用的 next_prompt",
                task_state=state,
                passed=round_passed,
                round_title=str(iteration.get("round_title") or ""),
            )
            _finish_goal(
                settings,
                goal,
                GOAL_WAITING_USER,
                summary=summary,
                question=NO_NEXT_PROMPT_QUESTION,
            )
            return

        _advance_to_next_iteration_or_stop(
            server,
            settings,
            goal,
            next_n=n + 1,
            summary=summary,
            next_prompt=next_prompt,
            iterations=iterations,
            round_passed=round_passed,
        )


def _apply_evaluation_decision(
    server: Any,
    settings: Any,
    goal: dict[str, Any],
    iteration: dict[str, Any],
    iterations: list[dict[str, Any]],
    task: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    goal_id = str(goal["id"])
    n = int(iteration["n"])
    task_id = str(iteration["task_id"])
    state = str(task.get("state") or "")
    stage = str(task.get("stage") or "")

    # The operator may have stopped / resolved the goal while we were evaluating.
    fresh_goal = _get_goal_row(settings, goal_id)
    if str(fresh_goal.get("state") or "") != GOAL_RUNNING:
        _record_decision(
            settings, goal_id, n,
            decision="", reason="评估期间目标状态已变化，本轮结论已丢弃", task_state=state,
        )
        return

    # Re-read the task: it may have been cancelled while the evaluator was running.
    fresh = ags.get_task(settings, task_id) or {}
    fresh_state = str(fresh.get("state") or "") or state
    fresh_stage = str(fresh.get("stage") or "") or stage
    state, stage = fresh_state, fresh_stage

    is_continuous = str(fresh_goal.get("mode") or GOAL_MODE_OUTCOME) == GOAL_MODE_CONTINUOUS
    summary = decision.get("summary") or f"第 {n} 轮已结束，任务状态：{state}。"

    # ---- hard rules enforced in code, never trusted from the model -------
    if state == "cancelled":
        # No evaluator decision is recorded: the round never produced a result.
        _stage_eval_fields(settings, goal_id, summary=summary)
        _record_decision(
            settings, goal_id, n,
            decision="", reason="本轮任务已被取消，未产生评估结论", task_state=state,
        )
        _finish_goal(
            settings, goal, GOAL_STOPPED,
            summary=summary,
            stop_reason="本轮任务被取消，目标已停止",
        )
        return

    if state == "waiting_user":
        if _deadline_passed(fresh_goal):
            _stage_eval_fields(settings, goal_id, summary=summary)
            _record_decision(
                settings, goal_id, n,
                decision=DECISION_CONTINUE, reason="已到截止时间", task_state=state, passed=False,
            )
            _finish_deadline(settings, fresh_goal, _iterations(settings, goal_id))
            return
        if stage == "needs_validation":
            question = NEEDS_VALIDATION_QUESTION
        else:
            question = decision["question"] or (
                f"第 {n} 轮的任务在等待用户处理（阶段：{stage or 'waiting_user'}）。"
                "请检查结果后告诉我是否达成，或者还要改什么。"
            )
        _stage_eval_fields(settings, goal_id, summary=summary, question=question)
        _record_decision(
            settings, goal_id, n,
            decision=DECISION_ASK_USER, reason=question, task_state=state,
        )
        _finish_goal(settings, goal, GOAL_WAITING_USER, summary=summary, question=question)
        return

    if not decision["decision"]:
        if _deadline_passed(fresh_goal):
            _stage_eval_fields(settings, goal_id, summary=summary)
            _record_decision(
                settings, goal_id, n,
                decision=DECISION_CONTINUE, reason="已到截止时间", task_state=state, passed=False,
            )
            _finish_deadline(settings, fresh_goal, _iterations(settings, goal_id))
            return
        # Parse failure / chat unavailable / unusable decision payload.
        # Mark as unparseable for streak logic, but ask user if it's an outcome goal.
        decision = _normalize_decision({"decision": DECISION_ASK_USER, "reason": "evaluator_unparseable"})
        question = EVALUATOR_UNAVAILABLE_QUESTION
        _stage_eval_fields(settings, goal_id, summary=summary, question=question)
        _record_decision(
            settings, goal_id, n,
            decision=DECISION_ASK_USER, reason="评估器无法给出可用的判断", task_state=state,
        )
        if not is_continuous:
            _finish_goal(settings, goal, GOAL_WAITING_USER, summary=summary, question=question)
        else:
            # For continuous goals, continue despite unparseable evaluator.
            # Infra failure, not counted in failure streak.
            _advance_to_next_iteration_or_stop(server, settings, fresh_goal, next_n=n + 1, summary=summary,
                                               next_prompt="评估器解析失败，自动重试。", iterations=_iterations(settings, goal_id),
                                               round_passed=False)
        return

    if decision["decision"] == DECISION_ACHIEVED and state != "completed":
        # Downgrade: only a genuinely completed task may count as achieved.
        downgrade_reason = decision["reason"] or (
            f"模型判断已达成，但本轮任务状态是 {state}（不是 completed），已降级处理。"
        )
        decision = dict(decision)
        decision["decision"] = DECISION_CONTINUE
        decision["reason"] = downgrade_reason

    if decision["decision"] == DECISION_ACHIEVED and not _completion_is_current(settings, task_id):
        decision = dict(decision)
        decision["decision"] = DECISION_CONTINUE
        decision["reason"] = _STALE_ACCEPTANCE_REASON
        decision["summary"] = _STALE_ACCEPTANCE_REASON
        decision["next_prompt"] = _STALE_ACCEPTANCE_NEXT
        summary = _STALE_ACCEPTANCE_REASON

    round_passed = state == "completed" and decision["decision"] in (DECISION_ACHIEVED, DECISION_CONTINUE)
    round_title = _derive_round_title(decision, iteration, fresh or task, n) if round_passed else ""

    infra_failure = False
    if not round_passed and state in ("failed", "blocked"):
        infra_failure = _is_infrastructure_failure(settings, fresh or task) or "evaluator_unparseable" in decision["reason"]

    if round_passed:
        consec_failures = 0
    elif infra_failure:
        consec_failures = int(fresh_goal.get("consecutive_failures") or 0)
    elif state in ("failed", "blocked") or (state != "completed" and decision["decision"] != DECISION_ASK_USER):
        consec_failures = int(fresh_goal.get("consecutive_failures") or 0) + 1
    else:
        consec_failures = int(fresh_goal.get("consecutive_failures") or 0)
    _update_goal(settings, goal_id, consecutive_failures=consec_failures, updated_at=_utc_now())

    if decision["decision"] == DECISION_ACHIEVED and not is_continuous:
        reason = decision["reason"] or "评估器判断目标已达成"
        _stage_eval_fields(settings, goal_id, summary=summary)
        _record_decision(
            settings, goal_id, n,
            decision=DECISION_ACHIEVED, reason=reason, task_state=state,
            passed=True, round_title=round_title,
        )
        # Recheck the snapshot after persisting the decision as well.  A crash
        # or concurrent edit in this window uses the same recovery path.
        recorded = dict(iteration, decision=DECISION_ACHIEVED, decision_reason=reason,
                        task_final_state=state)
        _reapply_decision(server, settings, goal, recorded, iterations)
        if _get_goal_row(settings, goal_id)["state"] == GOAL_ACHIEVED:
            _store_goal_lessons(settings, goal, decision.get("lessons"))
        return

    if _deadline_passed(fresh_goal):
        stored_decision = (
            DECISION_ACHIEVED if (decision["decision"] == DECISION_ACHIEVED and is_continuous) else DECISION_CONTINUE
        )
        _stage_eval_fields(settings, goal_id, summary=summary)
        _record_decision(
            settings,
            goal_id,
            n,
            decision=stored_decision,
            reason=decision["reason"] or "已到截止时间",
            task_state=state,
            passed=round_passed,
            round_title=round_title,
        )
        if round_passed and is_continuous:
            _store_goal_lessons(settings, goal, decision.get("lessons"))
        _finish_deadline(settings, fresh_goal, _iterations(settings, goal_id))
        return

    if is_continuous and not round_passed and consec_failures >= CONTINUOUS_CONSECUTIVE_FAILURE_LIMIT and decision["decision"] != DECISION_ASK_USER:
        next_suggestion = decision["next_prompt"].strip()
        streak_question = _continuous_failures_question(settings, fresh or task, decision)
        _stage_eval_fields(
            settings,
            goal_id,
            summary=summary,
            question=streak_question,
            last_next_prompt=next_suggestion if next_suggestion else None,
        )
        _record_decision(
            settings,
            goal_id,
            n,
            decision=DECISION_ASK_USER,
            reason=f"连续 {consec_failures} 轮未通过检查，暂停等待用户指导",
            task_state=state,
            passed=False,
        )
        _finish_goal(
            settings,
            goal,
            GOAL_WAITING_USER,
            summary=summary,
            question=streak_question,
        )
        return

    if decision["decision"] == DECISION_GIVE_UP:
        reason = decision["reason"] or "评估器判断目标无法达成"
        _stage_eval_fields(settings, goal_id, summary=summary)
        _record_decision(
            settings, goal_id, n,
            decision=DECISION_GIVE_UP, reason=reason, task_state=state, passed=False,
        )
        if decision["next_prompt"].strip() and len(decision["next_prompt"].strip()) <= MAX_NEXT_PROMPT_CHARS:
            _update_goal(settings, goal_id, last_next_prompt=decision["next_prompt"].strip())
        _finish_goal(
            settings, goal, GOAL_FAILED,
            summary=summary,
            stop_reason=reason,
        )
        return

    if decision["decision"] == DECISION_ASK_USER:
        question = decision["question"] or (
            "评估器需要你确认这一轮的结果，请检查后告诉我是否达成或还要改什么。"
        )
        next_suggestion = decision["next_prompt"].strip()
        _stage_eval_fields(
            settings,
            goal_id,
            summary=summary,
            question=question,
            last_next_prompt=next_suggestion if next_suggestion else None,
        )
        _record_decision(
            settings, goal_id, n,
            decision=DECISION_ASK_USER, reason=decision["reason"] or question,
            task_state=state, passed=round_passed, round_title=round_title,
        )
        # Keep the evaluator's suggestion so an operator reply can build on it.
        if next_suggestion:
            _update_goal(settings, goal_id, last_next_prompt=next_suggestion)
        _finish_goal(settings, goal, GOAL_WAITING_USER, summary=summary, question=question)
        return

    # decision == continue (or achieved in continuous mode: keep improving!)
    used = len(iterations)
    max_iterations = int(fresh_goal["max_iterations"])
    next_prompt = decision["next_prompt"].strip()
    if len(next_prompt) > MAX_NEXT_PROMPT_CHARS:
        next_prompt = ""
    if infra_failure and not next_prompt:
        next_prompt = "上一轮因为执行器或运行环境异常未能完成，请重试并继续完成上一轮的工作。"

    if not (is_continuous and round_passed) and used < max_iterations and not _deadline_passed(fresh_goal) and not next_prompt:
        _stage_eval_fields(settings, goal_id, summary=summary, question=NO_NEXT_PROMPT_QUESTION)
        _record_decision(
            settings, goal_id, n,
            decision=DECISION_ASK_USER, reason="continue 缺少可用的 next_prompt",
            task_state=state, passed=round_passed, round_title=round_title,
        )
        _finish_goal(
            settings, goal, GOAL_WAITING_USER,
            summary=summary,
            question=NO_NEXT_PROMPT_QUESTION,
        )
        return

    stored_decision = (
        DECISION_ACHIEVED if (decision["decision"] == DECISION_ACHIEVED and is_continuous) else DECISION_CONTINUE
    )
    _stage_eval_fields(
        settings,
        goal_id,
        summary=summary,
        last_next_prompt=next_prompt if (used < max_iterations and next_prompt) else None,
    )
    _record_decision(
        settings, goal_id, n,
        decision=stored_decision,
        reason=decision["reason"] or ("本轮改进已通过检查，继续下一轮改进" if round_passed and is_continuous else ""),
        task_state=state,
        passed=round_passed,
        round_title=round_title,
    )
    if round_passed and is_continuous:
        _store_goal_lessons(settings, goal, decision.get("lessons"))

    updated_iterations = _iterations(settings, goal_id)
    _advance_to_next_iteration_or_stop(
        server,
        settings,
        fresh_goal,
        next_n=n + 1,
        summary=summary,
        next_prompt=next_prompt,
        iterations=updated_iterations,
        round_passed=round_passed,
    )


async def _evaluate_iteration(
    server: Any,
    settings: Any,
    goal: dict[str, Any],
    iteration: dict[str, Any],
    iterations: list[dict[str, Any]],
    task: dict[str, Any],
) -> None:
    goal_id = goal["id"]
    n = int(iteration["n"])
    task_id = str(iteration["task_id"])

    claimed = await _to_thread(_claim_iteration, settings, goal_id, n)
    if not claimed:
        return  # another tick owns it, or it was already evaluated

    handled_quota = await _to_thread(
        _maybe_handle_quota_failure, server, settings, goal, iteration, iterations, task
    )
    if handled_quota:
        return

    try:
        repo_path, wt_path, _managed = await _to_thread(_scope_paths, settings, goal)
    except svc.SupervisorServiceError as exc:
        await _to_thread(_fail_goal, settings, goal, str(exc.message))
        return

    decision = await _ask_evaluator(
        settings,
        goal,
        iterations,
        worktree_path=wt_path,
        repo_path=repo_path,
        task_id=task_id,
    )

    # Retry logic
    retry_count = 0
    while not decision.get("decision") and retry_count < 2:
        retry_count += 1
        raw_output = decision.get("_raw_output") or ""
        retry_message = (
            "Your previous reply could not be parsed as the required JSON decision. "
            "Reply with ONLY the JSON object with keys decision, reason, next_prompt, question, summary "
            "(decision is one of achieved, continue, give_up, ask_user). "
            f"Previous reply: {truncate(raw_output, 2000)}"
        )
        decision = await _ask_evaluator(
            settings,
            goal,
            iterations,
            worktree_path=wt_path,
            repo_path=repo_path,
            task_id=task_id,
            retry_message=retry_message,
        )

    # Fallback for continuous goals
    is_continuous = str(goal.get("mode") or GOAL_MODE_OUTCOME) == GOAL_MODE_CONTINUOUS
    if not decision.get("decision") and is_continuous:
        state = str(task.get("state") or "")
        passed = state == "completed" and _completion_is_current(settings, task_id)
        
        reason = "evaluator_unparseable: 评估器没给出可用判断，按验收通过继续下一轮" if passed else "evaluator_unparseable: 评估器没给出可用判断，按失败轮次继续"
        decision = _normalize_decision({
            "decision": DECISION_CONTINUE,
            "reason": reason,
            "summary": f"评估器无响应，按自动逻辑处理，状态：{state}",
            "next_prompt": "上一轮评估器无法解析，根据自动兜底策略进入下一轮。"
        })
        # Add metadata for failure classification
        # Need to record this somewhere so _is_infrastructure_failure sees it.
        # I will use the reason or a special flag if I could, but I can only pass decision dict.
        # Maybe I can add it to the reason and parse it in _is_infrastructure_failure?

    await _to_thread(
        _apply_evaluation_decision,
        server,
        settings,
        goal,
        iteration,
        iterations,
        task,
        decision,
    )


def _recover_interrupted_iteration(
    server: Any, settings: Any, goal: dict[str, Any], iteration: dict[str, Any],
    iterations: list[dict[str, Any]], task: dict[str, Any],
) -> None:
    """Continue after a verified stopped writer, without replaying its task."""
    goal_id, task_id, n = str(goal["id"]), str(iteration["task_id"]), int(iteration["n"])
    if not _claim_iteration(settings, goal_id, n):
        return
    try:
        _, worktree_path, managed = _scope_paths(settings, goal)
        task_path = str(task.get("worktree_path") or "")
        safe = bool(managed and task_path and os.path.realpath(task_path) == os.path.realpath(worktree_path))
        safe = safe and ags.lock_release_safe(settings, worktree_path, task_id)
        if safe:
            safe = ags.release_worktree_lock(settings, worktree_path, task_id)
    except (OSError, svc.SupervisorServiceError, ags.SupervisorStateError):
        safe = False

    if safe:
        decision = _normalize_decision({
            "decision": DECISION_CONTINUE,
            "reason": "上一轮执行中断，已确认写入进程停止；保留现有改动并继续。",
            "summary": f"第 {n} 轮执行中断，已保留工作目录中的改动。",
            "next_prompt": "上一轮执行中断。先检查工作目录中的现有改动和任务目标，保留已有有效成果；继续完成尚未完成的工作并运行验证。",
        })
    else:
        decision = _normalize_decision({
            "decision": DECISION_ASK_USER,
            "reason": "上一轮执行中断，暂时无法确认原进程已停止或工作目录可安全使用。",
            "summary": f"第 {n} 轮执行中断，现有改动已保留。",
            "question": "无法确认上一轮的写入进程已停止，已暂停继续执行。请检查原进程和工作目录后再继续。",
        })
    _apply_evaluation_decision(server, settings, goal, iteration, iterations, task, decision)


async def _advance_one_goal(server: Any, settings: Any, goal: dict[str, Any]) -> None:
    goal_id = goal["id"]
    goal = await _to_thread(_get_goal_row, settings, goal_id)
    if goal["state"] != GOAL_RUNNING:
        return

    iterations = await _to_thread(_iterations, settings, goal_id)
    if not iterations:
        if _deadline_passed(goal):
            await _to_thread(_finish_deadline, settings, goal, iterations)
            return

        late_note = _late_round_note(goal, iterations, first_round=True)
        if late_note:
            await _to_thread(_finish_deadline, settings, goal, iterations, note=late_note)
            return

        await _to_thread(
            _start_iteration,
            server,
            settings,
            goal,
            1,
            _first_iteration_prompt(goal, settings=settings),
        )
        return

    current = iterations[-1]
    if current.get("decision") or current.get("task_final_state") == "cancelled":
        await _to_thread(_reapply_decision, server, settings, goal, current, iterations)
        return

    if not current["task_id"]:
        # Crash between iteration insert and task creation, or resumed from waiting_quota:
        # enforce deadline before dispatching a new task.
        if _deadline_passed(goal):
            await _to_thread(_finish_deadline, settings, goal, iterations)
            return
        await _to_thread(_start_iteration, server, settings, goal, int(current["n"]), current["prompt"])
        return

    task = await _to_thread(ags.get_task, settings, current["task_id"])
    if task is None:
        await _to_thread(
            _fail_goal,
            settings,
            goal,
            f"第 {current['n']} 轮的任务记录缺失（task_id={current['task_id']}），目标无法继续",
        )
        return

    task_state = str(task.get("state") or "")
    if task_state == "pending_dispatch":
        await _to_thread(
            _redispatch_iteration_task,
            server,
            settings,
            goal,
            int(current["n"]),
            str(current["task_id"]),
        )
        return

    if task_state in _GOAL_INFLIGHT_STATES:
        return  # still active (even if deadline passed, let running round finish)

    if task_state == "interrupted":
        await _to_thread(_recover_interrupted_iteration, server, settings, goal, current, iterations, task)
        return

    await _evaluate_iteration(server, settings, goal, current, iterations, task)


async def advance_goals(server: Any, settings: Any) -> None:
    """Drive every `running` goal forward and resume `waiting_quota` goals whose
    pause is over. Called on each SupervisorWorkerManager tick."""
    if settings is None or not svc.is_enabled(settings):
        return
    try:
        await _to_thread(_resume_due_waiting_quota, settings)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("failed to check waiting_quota goals")

    try:
        goals = await _to_thread(_running_goals, settings)
    except Exception:
        log.exception("failed to list running goals")
        return
    for goal in goals:
        try:
            await _advance_one_goal(server, settings, goal)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("goal %s: advance failed", goal.get("id"))
