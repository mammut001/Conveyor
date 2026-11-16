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
from conveyor.agents.goal_notify import notify_goal_change
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
    consecutive_failures INTEGER NOT NULL DEFAULT 0
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
"""


_GOAL_SCHEMA_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("supervisor_goals", "deadline_at", "TEXT"),
    ("supervisor_goals", "mode", "TEXT NOT NULL DEFAULT 'outcome'"),
    ("supervisor_goals", "resume_at", "TEXT"),
    ("supervisor_goals", "consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
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


def _compute_remaining_seconds(deadline_at: str | None) -> int | None:
    deadline_dt = _parse_utc(deadline_at)
    if deadline_dt is None:
        return None
    delta = (deadline_dt - _now()).total_seconds()
    if delta <= 0:
        return 0
    return math.ceil(delta)


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
                deadline_at, mode, resume_at, consecutive_failures)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '', '', ?, ?, NULL, 0)""",
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
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    goal = _enrich_goal_dict(_get_goal_row(settings, goal_id), [])
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


def stop_goal(server: Any, settings: Any, goal_id: str) -> dict[str, Any]:
    """User stops the goal: state → `stopped`, and the active task (if any) is cancelled."""
    svc.require_enabled(settings)
    goal = _get_goal_row(settings, goal_id)
    if goal["state"] in (GOAL_STOPPED, GOAL_ACHIEVED, GOAL_FAILED, GOAL_EXHAUSTED):
        return get_goal(settings, goal_id)

    reason = "用户手动停止了目标"
    _update_goal(
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


def _finish_goal(
    settings: Any,
    goal: dict[str, Any],
    state: str,
    *,
    summary: str,
    question: str = "",
    stop_reason: str = "",
) -> dict[str, Any]:
    updated = _update_goal(
        settings,
        goal["id"],
        state=state,
        question=truncate(redact_text(question or ""), MAX_QUESTION_CHARS),
        summary=truncate(redact_text(summary or ""), MAX_SUMMARY_CHARS),
        stop_reason=truncate(redact_text(stop_reason or ""), MAX_REASON_CHARS),
        resume_at=None,
        updated_at=_utc_now(),
    )
    log.info("goal %s -> %s (%s)", goal["id"], state, updated.get("stop_reason") or updated.get("question") or "")
    # Tell the owner on Telegram / Feishu / Web Push. Non-blocking and idempotent;
    # `stopped` is skipped because the owner stopped the goal themselves.
    if state in GOAL_NOTIFY_STATES:
        notify_goal_change(settings, updated, state)
    return updated


def _finish_deadline(
    settings: Any,
    goal: dict[str, Any],
    iterations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    iters = iterations if iterations is not None else _iterations(settings, str(goal["id"]))
    enriched = _enrich_goal_dict(goal, iters)
    total = int(enriched["rounds_total"])
    passed = int(enriched["rounds_passed"])
    summary = f"时间到了：一共跑了 {total} 轮，其中 {passed} 轮通过检查。"
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

    return {
        "decision": raw_decision,
        "reason": _text("reason"),
        "next_prompt": _text("next_prompt"),
        "question": _text("question"),
        "summary": _text("summary"),
        "round_title": _text("round_title"),
        "lessons": lessons,
    }


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


def _task_failure_texts(settings: Any, task: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    err = str(task.get("error") or "").strip()
    if err:
        texts.append(err)
    task_id = str(task.get("id") or "")
    if task_id:
        try:
            for ex in ags.list_executions(settings, task_id=task_id, limit=10):
                for key in ("error", "error_message", "stderr"):
                    val = str(ex.get(key) or "").strip()
                    if val:
                        texts.append(val)
        except Exception:
            pass
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
    task_state = str(task.get("state") or "")
    if task_state not in ("failed", "blocked"):
        return False
    texts = _task_failure_texts(settings, task)
    if not texts:
        return False
    now_ts = _now().timestamp()
    for text in texts:
        if worker_pool.ALL_QUOTA_PREFIX in text:
            return True
        if workers.classify_failure(text) is not None:
            return True
        if "冷却中" in text or "用量阈值" in text or "over_threshold" in text:
            return True
        if worker_pool.NO_WORKER_REASON in text and _has_cooling_or_over_threshold_worker(settings, now_ts):
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

    for text in _task_failure_texts(settings, task):
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
) -> tuple[bool, str, Any, Any, str, int]:
    roles = ags.load_roles(settings)
    coordinator = roles["coordinator"]
    executor = ags.get_executor(settings, "chat")
    ok, reason = executor.available()
    if not ok:
        return False, str(reason or ""), None, None, "", 0
    prompt = _build_eval_prompt(goal, iterations, settings)
    timeout = max(30, min(int(ags.task_timeout(settings)), 600))
    return True, "", executor, coordinator, prompt, timeout


async def _ask_evaluator(
    settings: Any,
    goal: dict[str, Any],
    iterations: list[dict[str, Any]],
    *,
    worktree_path: str,
    repo_path: str,
    task_id: str,
) -> dict[str, str]:
    """Ask the coordinator chat model for a decision. Never raises."""
    empty = _normalize_decision(None)
    try:
        ok, reason, executor, coordinator, prompt, timeout = await asyncio.to_thread(
            _prepare_evaluator_call,
            settings,
            goal,
            iterations,
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
        return empty
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

    if stored_decision == DECISION_GIVE_UP:
        _finish_goal(
            settings,
            goal,
            GOAL_FAILED,
            summary=summary,
            stop_reason=reason or "评估器判断目标无法达成",
        )
        return

    if stored_decision == DECISION_ACHIEVED:
        task_id = str(iteration.get("task_id") or "")
        task = ags.get_task(settings, task_id) if task_id else None
        if task is None:
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
    summary = decision["summary"] or f"第 {n} 轮已结束，任务状态：{state}。"

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
        # Parse failure / chat unavailable / unusable decision payload.
        question = EVALUATOR_UNAVAILABLE_QUESTION
        _stage_eval_fields(settings, goal_id, summary=summary, question=question)
        _record_decision(
            settings, goal_id, n,
            decision=DECISION_ASK_USER, reason="评估器无法给出可用的判断", task_state=state,
        )
        _finish_goal(settings, goal, GOAL_WAITING_USER, summary=summary, question=question)
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

    if round_passed:
        consec_failures = 0
    elif state in ("failed", "blocked") or (state != "completed" and decision["decision"] != DECISION_ASK_USER):
        consec_failures = int(fresh_goal.get("consecutive_failures") or 0) + 1
    else:
        consec_failures = int(fresh_goal.get("consecutive_failures") or 0)
    _update_goal(settings, goal_id, consecutive_failures=consec_failures, updated_at=_utc_now())

    if is_continuous and not round_passed and consec_failures >= CONTINUOUS_CONSECUTIVE_FAILURE_LIMIT and decision["decision"] != DECISION_ASK_USER:
        next_suggestion = decision["next_prompt"].strip()
        _stage_eval_fields(
            settings,
            goal_id,
            summary=summary,
            question=CONTINUOUS_FAILURES_QUESTION,
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
            question=CONTINUOUS_FAILURES_QUESTION,
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

    claimed = await asyncio.to_thread(_claim_iteration, settings, goal_id, n)
    if not claimed:
        return  # another tick owns it, or it was already evaluated

    handled_quota = await asyncio.to_thread(
        _maybe_handle_quota_failure, server, settings, goal, iteration, iterations, task
    )
    if handled_quota:
        return

    try:
        repo_path, wt_path, _managed = await asyncio.to_thread(_scope_paths, settings, goal)
    except svc.SupervisorServiceError as exc:
        await asyncio.to_thread(_fail_goal, settings, goal, str(exc.message))
        return

    decision = await _ask_evaluator(
        settings,
        goal,
        iterations,
        worktree_path=wt_path,
        repo_path=repo_path,
        task_id=task_id,
    )

    await asyncio.to_thread(
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
    goal = await asyncio.to_thread(_get_goal_row, settings, goal_id)
    if goal["state"] != GOAL_RUNNING:
        return

    iterations = await asyncio.to_thread(_iterations, settings, goal_id)
    if not iterations:
        if _deadline_passed(goal):
            await asyncio.to_thread(_finish_deadline, settings, goal, iterations)
            return
        await asyncio.to_thread(
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
        await asyncio.to_thread(_reapply_decision, server, settings, goal, current, iterations)
        return

    if not current["task_id"]:
        # Crash between iteration insert and task creation, or resumed from waiting_quota:
        # enforce deadline before dispatching a new task.
        if _deadline_passed(goal):
            await asyncio.to_thread(_finish_deadline, settings, goal, iterations)
            return
        await asyncio.to_thread(_start_iteration, server, settings, goal, int(current["n"]), current["prompt"])
        return

    task = await asyncio.to_thread(ags.get_task, settings, current["task_id"])
    if task is None:
        await asyncio.to_thread(
            _fail_goal,
            settings,
            goal,
            f"第 {current['n']} 轮的任务记录缺失（task_id={current['task_id']}），目标无法继续",
        )
        return

    task_state = str(task.get("state") or "")
    if task_state == "pending_dispatch":
        await asyncio.to_thread(
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
        await asyncio.to_thread(_recover_interrupted_iteration, server, settings, goal, current, iterations, task)
        return

    await _evaluate_iteration(server, settings, goal, current, iterations, task)


async def advance_goals(server: Any, settings: Any) -> None:
    """Drive every `running` goal forward and resume `waiting_quota` goals whose
    pause is over. Called on each SupervisorWorkerManager tick."""
    if settings is None or not svc.is_enabled(settings):
        return
    try:
        await asyncio.to_thread(_resume_due_waiting_quota, settings)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("failed to check waiting_quota goals")

    try:
        goals = await asyncio.to_thread(_running_goals, settings)
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
