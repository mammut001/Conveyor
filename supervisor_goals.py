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
- All user-facing strings (question, summary, errors) are Simplified Chinese.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any

import agent_supervisor as ags
import supervisor_diff as diff_mod
import supervisor_service as svc
from redaction import redact_text, truncate

log = logging.getLogger("conveyor.supervisor_goals")

GOAL_ID_PREFIX = "goal-"

GOAL_STATES = ("running", "waiting_user", "achieved", "stopped", "exhausted", "failed")
GOAL_RUNNING = "running"
GOAL_WAITING_USER = "waiting_user"
GOAL_ACHIEVED = "achieved"
GOAL_STOPPED = "stopped"
GOAL_EXHAUSTED = "exhausted"
GOAL_FAILED = "failed"

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

FIRST_ITERATION_NOTE = "这是目标的第 1 轮。"

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

_SCHEMA_INIT_LOCK = threading.Lock()
_INITIALIZED_DBS: set[str] = set()

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
    last_next_prompt TEXT NOT NULL DEFAULT ''
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
    UNIQUE(goal_id, n)
);
CREATE INDEX IF NOT EXISTS idx_sup_goals_state ON supervisor_goals(state);
CREATE INDEX IF NOT EXISTS idx_sup_goal_iters_goal ON supervisor_goal_iterations(goal_id, n);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _connect(settings: Any) -> sqlite3.Connection:
    path = _db_path(settings)
    key = str(path)
    with _SCHEMA_INIT_LOCK:
        need_init = key not in _INITIALIZED_DBS or not path.exists()
    conn = sqlite3.connect(key, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    if need_init:
        with _SCHEMA_INIT_LOCK:
            if key not in _INITIALIZED_DBS or not path.exists():
                conn.executescript(_GOALS_SCHEMA_SQL)
                _INITIALIZED_DBS.add(key)
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
    }


def _iteration_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "n": int(row["n"]),
        "task_id": str(row["task_id"] or ""),
        "prompt": str(row["prompt"] or ""),
        "decision": str(row["decision"] or ""),
        "decision_reason": str(row["decision_reason"] or ""),
        "started_at": str(row["started_at"] or ""),
        "finished_at": str(row["finished_at"] or ""),
        "task_final_state": str(row["task_final_state"] or ""),
    }


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
# Public API: create / list / get / stop / reply
# ---------------------------------------------------------------------------

def create_goal(server: Any, settings: Any, body: dict[str, Any]) -> dict[str, Any]:
    """Create a `running` goal. The first iteration/task is dispatched by `advance_goals`."""
    svc.require_enabled(settings)
    if not isinstance(body, dict):
        raise _service_error(HTTPStatus.BAD_REQUEST, "request body must be an object", code="invalid_body")

    outcome = _clean_outcome(body.get("outcome"))
    title = _clean_title(body.get("title"), outcome)
    max_iterations = _clamp_max_iterations(body.get("max_iterations"))
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

    goal_id = f"{GOAL_ID_PREFIX}{os.urandom(8).hex()}"
    now = _utc_now()
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO supervisor_goals
               (id, repo_id, worktree_id, title, outcome, max_iterations,
                role_overrides_json, state, created_at, updated_at,
                question, summary, stop_reason, last_next_prompt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '', '')""",
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
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    goal = _get_goal_row(settings, goal_id)
    goal["iterations"] = []
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
        goal = _goal_row_to_dict(row)
        goal["iterations_used"] = len(_iterations(settings, goal["id"]))
        goals.append(goal)
    return goals


def get_goal(settings: Any, goal_id: str) -> dict[str, Any]:
    svc.require_enabled(settings)
    goal = _get_goal_row(settings, goal_id)
    iterations = _iterations(settings, goal["id"])
    for item in iterations:
        task_id = item["task_id"]
        if not task_id:
            item["task_state"] = ""
            continue
        task = ags.get_task(settings, task_id)
        item["task_state"] = str(task.get("state") or "") if task else "missing"
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

    iterations = _iterations(settings, goal_id)
    current = iterations[-1] if iterations else None
    reason = "用户手动停止了目标"
    if current and current["task_id"]:
        task = ags.get_task(settings, current["task_id"])
        if task is not None and str(task.get("state") or "") in svc._ACTIVE_WORKTREE_STATES:
            try:
                svc.cancel_scoped_task(server, settings, current["task_id"], reason=reason)
            except svc.SupervisorServiceError:
                log.warning("goal %s: cancelling task %s failed", goal_id, current["task_id"], exc_info=True)

    _update_goal(
        settings,
        goal_id,
        state=GOAL_STOPPED,
        question="",
        stop_reason=reason,
        updated_at=_utc_now(),
    )
    svc.ensure_server_worker(server, settings, wake=True)
    return get_goal(settings, goal_id)


def reply_goal(server: Any, settings: Any, goal_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Answer a `waiting_user` goal: confirm achievement, or push it forward with guidance."""
    svc.require_enabled(settings)
    if not isinstance(body, dict):
        raise _service_error(HTTPStatus.BAD_REQUEST, "request body must be an object", code="invalid_body")
    goal = _get_goal_row(settings, goal_id)
    if goal["state"] != GOAL_WAITING_USER:
        raise _service_error(
            HTTPStatus.CONFLICT,
            "目标当前没有在等待用户回复",
            code="goal_not_waiting_user",
        )

    raw_achieved = body.get("achieved")
    achieved = bool(raw_achieved) if isinstance(raw_achieved, bool) else False

    raw_message = body.get("message")
    message = ""
    if raw_message is not None:
        if not isinstance(raw_message, str):
            raise _service_error(HTTPStatus.BAD_REQUEST, "message must be a string", code="invalid_message")
        message = redact_text(raw_message).strip()

    if achieved:
        _update_goal(
            settings,
            goal_id,
            state=GOAL_ACHIEVED,
            stop_reason="用户确认已达成",
            question="",
            updated_at=_utc_now(),
        )
        svc.ensure_server_worker(server, settings, wake=True)
        return get_goal(settings, goal_id)

    if not message:
        raise _service_error(
            HTTPStatus.BAD_REQUEST,
            "请用 message 描述这一轮还要改什么（或把 achieved 设为 true 确认达成）",
            code="invalid_message",
        )

    iterations = _iterations(settings, goal_id)
    current = iterations[-1] if iterations else None

    # Release the worktree lock held by a task that is itself waiting for the user.
    if current and current["task_id"]:
        task = ags.get_task(settings, current["task_id"])
        if task is not None and str(task.get("state") or "") == "waiting_user":
            try:
                svc.cancel_scoped_task(server, settings, current["task_id"], reason="用户回复目标，取消等待中的任务")
            except svc.SupervisorServiceError:
                log.warning(
                    "goal %s: cancelling waiting task %s failed",
                    goal_id,
                    current["task_id"],
                    exc_info=True,
                )

    next_n = len(iterations) + 1
    # Documented exception: when the budget is used up the user's explicit reply
    # buys exactly ONE extra iteration; anything beyond that is refused.
    if next_n > int(goal["max_iterations"]) + 1:
        raise _service_error(
            HTTPStatus.CONFLICT,
            "迭代预算已经用完（用户补充也只允许多跑一轮），请调高 max_iterations 后重试",
            code="goal_iterations_exhausted",
        )

    base_prompt = str(goal.get("last_next_prompt") or "").strip()
    prompt = _reply_prompt(goal, next_n, base_prompt, message)

    now = _utc_now()
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO supervisor_goal_iterations
               (goal_id, n, task_id, prompt, decision, decision_reason,
                started_at, finished_at, task_final_state, evaluating, evaluating_since)
               VALUES (?, ?, '', ?, '', '用户补充说明后继续', ?, '', '', 0, '')""",
            (goal_id, next_n, prompt, now),
        )
        conn.execute(
            "UPDATE supervisor_goals SET state = ?, question = '', updated_at = ? WHERE id = ?",
            (GOAL_RUNNING, now, goal_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    svc.ensure_server_worker(server, settings, wake=True)
    return get_goal(settings, goal_id)


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


def _claim_iteration(settings: Any, goal_id: str, n: int) -> bool:
    """Atomically claim an iteration for evaluation.

    Returns False when another tick is already evaluating it, or when it already
    carries a decision (evaluation is idempotent: never re-evaluated). A claim
    left behind by a crashed tick is taken over once it is older than
    `EVAL_STALE_SECONDS`.
    """
    now = _utc_now()
    try:
        stale_cutoff = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - EVAL_STALE_SECONDS, tz=timezone.utc
        ).isoformat(timespec="seconds")
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
) -> None:
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE supervisor_goal_iterations
               SET decision = ?, decision_reason = ?, task_final_state = ?,
                   finished_at = ?, evaluating = 0, evaluating_since = ''
               WHERE goal_id = ? AND n = ?""",
            (
                decision,
                truncate(redact_text(str(reason or "")), MAX_REASON_CHARS),
                str(task_state or ""),
                _utc_now(),
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
        updated_at=_utc_now(),
    )
    log.info("goal %s -> %s (%s)", goal["id"], state, updated.get("stop_reason") or updated.get("question") or "")
    return updated


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


# ---------------------------------------------------------------------------
# Task creation / dispatch through the existing service functions
# ---------------------------------------------------------------------------

def _first_iteration_prompt(goal: dict[str, Any]) -> str:
    return f"{goal['outcome']}\n\n{FIRST_ITERATION_NOTE}"


def _continuation_prompt(goal: dict[str, Any], n: int, next_prompt: str) -> str:
    raw = f"这是目标「{goal['title']}」的第 {n} 轮。目标：{goal['outcome']}\n\n本轮要做：{next_prompt}"
    return truncate(raw, svc.MAX_GOAL_CHARS)


def _reply_prompt(goal: dict[str, Any], n: int, base_next_prompt: str, message: str) -> str:
    """Prompt for the iteration started by an operator reply: the evaluator's last
    `next_prompt` (when there is one) plus the operator's own clarification."""
    header = f"这是目标「{goal['title']}」的第 {n} 轮。目标：{goal['outcome']}\n\n"
    if base_next_prompt:
        raw = f"{header}本轮要做：{base_next_prompt}\n\n用户补充：{message}"
    else:
        raw = f"{header}本轮要做：请按用户下面的补充继续。\n\n用户补充：{message}"
    return truncate(raw, svc.MAX_GOAL_CHARS)


def _adopt_orphan_task(settings: Any, worktree_path: str, prompt: str) -> str:
    """Crash recovery: adopt an already-active task on this worktree whose goal
    text matches the iteration prompt exactly (a task created just before a crash)."""
    try:
        tasks = ags.list_tasks(settings, limit=500)
    except Exception:
        return ""
    for task in tasks:
        if str(task.get("state") or "") not in svc._ACTIVE_WORKTREE_STATES:
            continue
        if str(task.get("goal") or "") != prompt:
            continue
        wp = str(task.get("worktree_path") or "")
        if wp and os.path.realpath(wp) != os.path.realpath(worktree_path):
            continue
        return str(task.get("id") or "")
    return ""


def _start_iteration(server: Any, settings: Any, goal: dict[str, Any], n: int, prompt: str) -> str:
    """Ensure iteration `n` has a dispatched task. Returns the task id ('' when deferred)."""
    iteration = _ensure_iteration(settings, goal["id"], n, prompt)
    if iteration["task_id"]:
        return iteration["task_id"]

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
            _set_iteration_task(settings, goal["id"], n, adopted)
            return adopted
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
        _set_iteration_task(settings, goal["id"], n, task_id)
        svc.dispatch_scoped_task(
            server,
            settings,
            task_id,
            {"roles": roles} if roles else {},
        )
    except svc.SupervisorServiceError as exc:
        log.warning("goal %s iteration %s: task dispatch failed: %s", goal["id"], n, exc.message)
        if task_id:
            # Task row exists but never ran: drop it so the next tick can retry cleanly.
            try:
                svc.cancel_scoped_task(server, settings, task_id, reason="目标派发失败，取消本轮任务")
            except svc.SupervisorServiceError:
                log.warning("goal %s: cleanup of task %s failed", goal["id"], task_id, exc_info=True)
            _clear_iteration_task(settings, goal["id"], n)
        if exc.status == HTTPStatus.CONFLICT:
            adopted = _adopt_orphan_task(settings, wt_path, prompt)
            if adopted:
                _set_iteration_task(settings, goal["id"], n, adopted)
                return adopted
            return ""
        _fail_goal(settings, goal, f"第 {n} 轮任务派发失败：{exc.message}")
        return ""
    except Exception as exc:  # defensive: never let one goal kill the loop
        log.exception("goal %s iteration %s: unexpected dispatch error", goal["id"], n)
        _fail_goal(settings, goal, f"第 {n} 轮任务派发异常：{type(exc).__name__}")
        return ""
    log.info("goal %s iteration %s dispatched task %s", goal["id"], n, task_id)
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
        if acc is None:
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
                "验收: "
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
    lines: list[str] = [
        "你是 Conveyor Agent Supervisor 的目标评估器（coordinator）。",
        "请根据「目标」和「历史轮次」判断用户期望的结果是否已经达成。",
        "只输出一个 JSON 对象：不要输出任何解释文字，不要使用 Markdown 代码块。",
        'JSON 字段：{"decision": "continue"|"achieved"|"ask_user"|"give_up", '
        '"reason": "...", "next_prompt": "...", "question": "...", "summary": "..."}',
        "- decision=achieved 仅当最新一轮任务状态为 completed 且确实实现了目标；",
        "- decision=continue 必须在 next_prompt 里给出下一轮的具体中文指令；",
        "- decision=ask_user 必须在 question 里给出需要用户决策的中文问题；",
        "- decision=give_up 表示目标无法达成，reason 说明原因；",
        "- summary 是一段中文进展摘要（当前进展、还差什么）。",
        "",
        f"目标标题：{goal['title']}",
        f"最大轮次：{goal['max_iterations']}（已用 {len(iterations)} 轮）",
        "目标（用户期望的结果）：",
        goal["outcome"],
        "",
        "历史轮次：",
    ]
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


def _normalize_decision(parsed: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(parsed, dict):
        return {
            "decision": "",
            "reason": "",
            "next_prompt": "",
            "question": "",
            "summary": "",
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

    return {
        "decision": raw_decision,
        "reason": _text("reason"),
        "next_prompt": _text("next_prompt"),
        "question": _text("question"),
        "summary": _text("summary"),
    }


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
        roles = ags.load_roles(settings)
        coordinator = roles["coordinator"]
        executor = ags.get_executor(settings, "chat")
        ok, reason = executor.available()
        if not ok:
            log.warning("goal %s: evaluator chat unavailable: %s", goal["id"], reason)
            return empty
        prompt = _build_eval_prompt(goal, iterations, settings)
        ctx = ags.ExecutionContext(
            task_id=task_id,
            repo_path=repo_path,
            worktree_path=worktree_path,
            stage="goal_evaluation",
            metadata={"goal_id": goal["id"]},
        )
        timeout = max(30, min(int(ags.task_timeout(settings)), 600))
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
        log.warning("goal %s: evaluator returned failure: %s", goal["id"], getattr(result, "error", ""))
        return empty
    parsed = _parse_decision(getattr(result, "output", ""))
    if parsed is None:
        log.warning("goal %s: evaluator output was not parsable JSON", goal["id"])
        return empty
    return _normalize_decision(parsed)


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
    state = str(task.get("state") or "")
    stage = str(task.get("stage") or "")

    if not _claim_iteration(settings, goal_id, n):
        return  # another tick owns it, or it was already evaluated

    try:
        repo_path, wt_path, _managed = _scope_paths(settings, goal)
    except svc.SupervisorServiceError as exc:
        _fail_goal(settings, goal, str(exc.message))
        return

    decision = await _ask_evaluator(
        settings,
        goal,
        iterations,
        worktree_path=wt_path,
        repo_path=repo_path,
        task_id=task_id,
    )

    # The operator may have stopped / resolved the goal while we were evaluating.
    if str(_get_goal_row(settings, goal_id).get("state") or "") != GOAL_RUNNING:
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

    summary = decision["summary"] or f"第 {n} 轮已结束，任务状态：{state}。"

    # ---- hard rules enforced in code, never trusted from the model -------
    if state == "cancelled":
        # No evaluator decision is recorded: the round never produced a result.
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
        _record_decision(
            settings, goal_id, n,
            decision=DECISION_ASK_USER, reason=question, task_state=state,
        )
        _finish_goal(settings, goal, GOAL_WAITING_USER, summary=summary, question=question)
        return

    if not decision["decision"]:
        # Parse failure / chat unavailable / unusable decision payload.
        question = EVALUATOR_UNAVAILABLE_QUESTION
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

    if decision["decision"] == DECISION_GIVE_UP:
        reason = decision["reason"] or "评估器判断目标无法达成"
        _record_decision(
            settings, goal_id, n,
            decision=DECISION_GIVE_UP, reason=reason, task_state=state,
        )
        _finish_goal(
            settings, goal, GOAL_FAILED,
            summary=summary,
            stop_reason=reason,
        )
        return

    if decision["decision"] == DECISION_ACHIEVED:
        reason = decision["reason"] or "评估器判断目标已达成"
        _record_decision(
            settings, goal_id, n,
            decision=DECISION_ACHIEVED, reason=reason, task_state=state,
        )
        _finish_goal(
            settings, goal, GOAL_ACHIEVED,
            summary=summary,
            stop_reason=reason,
        )
        return

    if decision["decision"] == DECISION_ASK_USER:
        question = decision["question"] or (
            "评估器需要你确认这一轮的结果，请检查后告诉我是否达成或还要改什么。"
        )
        _record_decision(
            settings, goal_id, n,
            decision=DECISION_ASK_USER, reason=decision["reason"] or question, task_state=state,
        )
        # Keep the evaluator's suggestion so an operator reply can build on it.
        if decision["next_prompt"].strip():
            _update_goal(settings, goal_id, last_next_prompt=decision["next_prompt"].strip())
        _finish_goal(settings, goal, GOAL_WAITING_USER, summary=summary, question=question)
        return

    # decision == continue
    used = len(iterations)
    max_iterations = int(goal["max_iterations"])
    _record_decision(
        settings, goal_id, n,
        decision=DECISION_CONTINUE, reason=decision["reason"], task_state=state,
    )
    if used >= max_iterations:
        _finish_goal(
            settings, goal, GOAL_EXHAUSTED,
            summary=summary,
            stop_reason=f"迭代预算已用完（{max_iterations} 轮），目标尚未达成",
        )
        return

    next_prompt = decision["next_prompt"].strip()
    if not next_prompt or len(next_prompt) > MAX_NEXT_PROMPT_CHARS:
        _record_decision(
            settings, goal_id, n,
            decision=DECISION_ASK_USER, reason="continue 缺少可用的 next_prompt", task_state=state,
        )
        _finish_goal(
            settings, goal, GOAL_WAITING_USER,
            summary=summary,
            question=NO_NEXT_PROMPT_QUESTION,
        )
        return

    next_n = n + 1
    prompt = _continuation_prompt(goal, next_n, next_prompt)
    _update_goal(
        settings,
        goal_id,
        last_next_prompt=next_prompt,
        summary=truncate(redact_text(summary), MAX_SUMMARY_CHARS),
        updated_at=_utc_now(),
    )
    _start_iteration(server, settings, goal, next_n, prompt)


async def _advance_one_goal(server: Any, settings: Any, goal: dict[str, Any]) -> None:
    goal_id = goal["id"]
    goal = _get_goal_row(settings, goal_id)
    if goal["state"] != GOAL_RUNNING:
        return

    iterations = _iterations(settings, goal_id)
    if not iterations:
        _start_iteration(server, settings, goal, 1, _first_iteration_prompt(goal))
        return

    current = iterations[-1]
    if not current["task_id"]:
        # Crash between iteration insert and task creation: retry now.
        _start_iteration(server, settings, goal, int(current["n"]), current["prompt"])
        return

    task = ags.get_task(settings, current["task_id"])
    if task is None:
        _fail_goal(
            settings,
            goal,
            f"第 {current['n']} 轮的任务记录缺失（task_id={current['task_id']}），目标无法继续",
        )
        return

    if str(task.get("state") or "") in svc._INFLIGHT_STATES:
        return  # still active: nothing to do this tick

    await _evaluate_iteration(server, settings, goal, current, iterations, task)


async def advance_goals(server: Any, settings: Any) -> None:
    """Drive every `running` goal forward. Called on each SupervisorWorkerManager tick."""
    if settings is None or not svc.is_enabled(settings):
        return
    try:
        goals = _running_goals(settings)
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
