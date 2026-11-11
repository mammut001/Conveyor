"""supervisor_service.py — Authenticated Web/API service layer for Agent Supervisor.

Provides:
- Canonical repository registration and real git worktree enumeration/creation
  backed by parameterized SQLite state (`state/supervisor_web.sqlite3`).
- Scoped task creation (`request_id` idempotency), server-enforced role override
  validation, durable queue dispatch, and single-worker scheduling on the web
  server's existing asyncio event loop.
- Supervisor-bound read-only chat session resolution (`web:web-console:supwt-*`)
  with real repository/worktree/branch context.
- Honest executor and deployment capabilities reporting.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable

from conveyor.agents import agent_supervisor as ags
from conveyor.core.redaction import redact_text, truncate
from conveyor.core.subprocess_env import utility_env
from conveyor.core.transcript_store import get_transcript_store, session_identity

log = logging.getLogger("conveyor.supervisor_service")

WEB_CHANNEL = "web"
WEB_OPERATOR_ID = "web-console"
SUPERVISOR_CHAT_PREFIX = "supwt-"
SUPERVISOR_DURABLE_PREFIX = f"{WEB_CHANNEL}:{WEB_OPERATOR_ID}:{SUPERVISOR_CHAT_PREFIX}"

MAX_PATH_CHARS = 1024
MAX_NAME_CHARS = 120
MAX_BRANCH_CHARS = 100
MAX_REF_CHARS = 100
MAX_GOAL_CHARS = 8000
MAX_CRITERIA_CHARS = 8000
MAX_REQUEST_ID_CHARS = 128
MAX_REASON_CHARS = 500
MAX_MODEL_CHARS = 120
MAX_EXEC_OUTPUT_PREVIEW_CHARS = 4000

_ID_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,119}$")
_FORBIDDEN_REF_SUBSTRINGS = ("..", "@{", "\\", "\x00", "\r", "\n", "\t", " ")
_UI_EXPOSED_EXECUTORS: tuple[tuple[str, str], ...] = (
    # Pool first: it is the recommended choice whenever a pool worker is usable.
    ("pool", "执行者池（自动轮换）"),
    ("pi", "Pi（内置）"),
    ("claude", "Claude Code"),
    ("command-code", "Command Code"),
    ("codex", "Codex CLI"),
    ("antigravity", "Antigravity"),
)
_OVERRIDABLE_ROLES = frozenset({"analyst", "implementer", "reviewer"})
_INFLIGHT_STATES = frozenset({
    "queued",
    "dispatching",
    "analyzing",
    "implementing",
    "reviewing",
    "repair_pending",
})
_ACTIVE_WORKTREE_STATES = _INFLIGHT_STATES | frozenset({"waiting_user"})

_SCHEMA_INIT_LOCK = threading.Lock()
_INITIALIZED_DBS: set[str] = set()

_WEB_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS supervisor_repositories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    canonical_path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supervisor_worktrees (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    canonical_path TEXT NOT NULL UNIQUE,
    managed INTEGER NOT NULL DEFAULT 0,
    chat_session_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    branch TEXT,
    FOREIGN KEY(repo_id) REFERENCES supervisor_repositories(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sup_worktrees_repo ON supervisor_worktrees(repo_id);

CREATE TABLE IF NOT EXISTS supervisor_task_requests (
    request_id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    worktree_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supervisor_repo_validation (
    repo_id TEXT PRIMARY KEY,
    selected_json TEXT NOT NULL,
    specs_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(repo_id) REFERENCES supervisor_repositories(id) ON DELETE CASCADE
);
"""


class SupervisorServiceError(Exception):
    """Structured HTTP-mappable error with redacted message and optional code."""

    def __init__(
        self,
        status: int | HTTPStatus,
        message: str,
        *,
        code: str | None = None,
    ) -> None:
        clean = redact_text(str(message or "服务端出错")).strip() or "服务端出错"
        super().__init__(clean)
        self.status = int(status)
        self.message = truncate(clean, 600)
        self.code = code

    @property
    def status_code(self) -> int:
        return self.status

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.message}
        if self.code:
            payload["code"] = self.code
        return payload


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_enabled(settings: Any) -> bool:
    return bool(settings is not None and ags.enabled(settings))


def require_enabled(settings: Any) -> None:
    if not is_enabled(settings):
        raise SupervisorServiceError(
            HTTPStatus.CONFLICT,
            "项目功能还没有打开（需要在服务端设置 CONVEYOR_AGENT_SUPERVISOR_ENABLED=true）",
            code="agent_supervisor_disabled",
        )


# ---------------------------------------------------------------------------
# SQLite persistence (isolated from agent_supervisor.sqlite3 lock domain)
# ---------------------------------------------------------------------------

def _db_path(settings: Any) -> Path:
    mem_root = getattr(settings, "codex_memory_root", "") or ""
    if not mem_root:
        raise SupervisorServiceError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "还没有配置存储目录",
        )
    state_dir = Path(os.path.realpath(os.path.expanduser(str(mem_root)))) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "supervisor_web.sqlite3"


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
                conn.executescript(_WEB_SCHEMA_SQL)
                _migrate_web_schema(conn)
                _INITIALIZED_DBS.add(key)
    return conn


def _migrate_web_schema(conn: sqlite3.Connection) -> None:
    """Additive columns for databases created by older builds."""
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(supervisor_worktrees)").fetchall()}
    if "branch" not in cols:
        # The branch a managed worktree was created on; the merge refuses to
        # commit/merge when the worktree HEAD has been moved to another branch.
        try:
            conn.execute("ALTER TABLE supervisor_worktrees ADD COLUMN branch TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


# ---------------------------------------------------------------------------
# Input & Git validation helpers
# ---------------------------------------------------------------------------

# Owner-facing validation text: plain Chinese, no git jargon. The ``code`` field is
# what clients and tests match on, so it never changes with the wording.

_PATH_FIELD_LABELS = {"path": "路径", "repo_path": "仓库路径", "worktree_path": "工作目录路径"}


def _path_label(field_name: str) -> str:
    return _PATH_FIELD_LABELS.get(field_name, "路径")


def _validate_id_token(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            f"{field_name} 需要是文字",
            code="invalid_input",
        )
    cleaned = value.strip()
    if not cleaned or not _ID_TOKEN_RE.match(cleaned):
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            f"{field_name} 不正确",
            code="invalid_input",
        )
    return cleaned


def _canonicalize_user_path(raw: Any, field_name: str = "path") -> str:
    label = _path_label(field_name)
    if not isinstance(raw, str):
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            f"请填写{label}",
            code="invalid_path",
        )
    trimmed = raw.strip()
    if not trimmed or len(trimmed) > MAX_PATH_CHARS:
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            f"{label}长度需要在 1-{MAX_PATH_CHARS} 个字符之间",
            code="invalid_path",
        )
    if any(ch in trimmed for ch in ("\x00", "\r", "\n")):
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            f"{label}里有不允许的特殊字符",
            code="invalid_path",
        )
    expanded = os.path.expanduser(trimmed)
    if not os.path.isabs(expanded):
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            f"请填写完整{label}（以 / 开头）",
            code="invalid_path",
        )
    raw_parts = Path(expanded).parts
    if ".." in raw_parts:
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            f"路径里不能有 ../ 这样的上级跳转",
            code="invalid_path",
        )
    return os.path.realpath(expanded)


def _canonical_internal_path(raw: str) -> str:
    if not raw:
        return ""
    return os.path.realpath(os.path.expanduser(str(raw).strip()))


def _managed_worktree_root(settings: Any) -> Path:
    task_root = getattr(settings, "codex_task_root", "") or ""
    if not task_root:
        raise SupervisorServiceError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "还没有配置任务目录",
        )
    return Path(_canonical_internal_path(str(task_root))) / "worktrees" / "supervisor"


def _run_git(
    settings: Any,
    repo_or_wt: str,
    args: list[str],
    *,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", repo_or_wt, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=utility_env(settings),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            "Git 命令超时了，可以再试一次",
            code="git_timeout",
        ) from exc
    except OSError as exc:
        raise SupervisorServiceError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            f"Git 运行失败：{exc}",
            code="git_unavailable",
        ) from exc


def validate_git_repository_root(settings: Any, raw_path: Any) -> str:
    """Validate that `raw_path` resolves to the canonical root of a real main git repository."""
    canon = _canonicalize_user_path(raw_path, "路径")
    target = Path(canon)
    if not target.exists() or not target.is_dir():
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            "这个路径不存在，或者不是文件夹",
            code="invalid_repository",
        )
    top = _run_git(settings, canon, ["rev-parse", "--show-toplevel"])
    if top.returncode != 0 or not top.stdout.strip():
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            "这个文件夹不是 Git 仓库",
            code="invalid_repository",
        )
    top_canon = os.path.realpath(top.stdout.strip())
    if top_canon != canon:
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            "请选 Git 仓库最外层的主文件夹，不要选里面的子文件夹",
            code="invalid_repository_root",
        )
    # Reject secondary linked worktrees being registered as top-level repositories.
    dirs = _run_git(settings, canon, ["rev-parse", "--git-dir", "--git-common-dir"])
    if dirs.returncode != 0:
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            "读不到这个仓库的信息",
            code="invalid_repository",
        )
    dir_lines = [ln.strip() for ln in dirs.stdout.splitlines() if ln.strip()]
    if len(dir_lines) >= 2:
        git_dir = os.path.realpath(os.path.join(canon, dir_lines[0]))
        common_dir = os.path.realpath(os.path.join(canon, dir_lines[1]))
        if git_dir != common_dir:
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                "这是 Git 的工作目录，请注册它所属的主仓库文件夹",
                code="worktree_not_repo_root",
            )
    if not ags.repo_is_git_dir(canon):
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            "这个仓库没有通过检查",
            code="invalid_repository",
        )
    return canon


def _detect_default_branch(settings: Any, canon_repo: str) -> str | None:
    sym = _run_git(settings, canon_repo, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], timeout=5.0)
    if sym.returncode == 0 and sym.stdout.strip():
        val = sym.stdout.strip()
        return val.split("/", 1)[1] if val.startswith("origin/") else val
    cur = _run_git(settings, canon_repo, [" symbolic-ref".strip(), "--short", "HEAD"], timeout=5.0)
    if cur.returncode == 0 and cur.stdout.strip():
        return cur.stdout.strip()
    return None


def _validate_branch_name(settings: Any, canon_repo: str, raw_branch: Any) -> str:
    if not isinstance(raw_branch, str):
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            "分支名称需要是文字",
            code="invalid_branch",
        )
    branch = raw_branch.strip()
    if not branch:
        return ""
    if len(branch) > MAX_BRANCH_CHARS:
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            f"分支名称长度不能超过 {MAX_BRANCH_CHARS} 个字符",
            code="invalid_branch",
        )
    if branch.startswith("-") or branch.startswith("/") or branch.endswith("/") or branch.endswith(".lock"):
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            "分支名称不符合 Git 规则，请换一个（不要以 - 或 / 开头）",
            code="invalid_branch",
        )
    if any(tok in branch for tok in _FORBIDDEN_REF_SUBSTRINGS) or any(
        ch in branch for ch in ("~", "^", ":", "?", "*", "[")
    ):
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            "分支名称里有不允许的字符，请换一个",
            code="invalid_branch",
        )
    chk = _run_git(settings, canon_repo, ["check-ref-format", "--branch", branch], timeout=5.0)
    if chk.returncode != 0:
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            "Git 不接受这个分支名称，请换一个",
            code="invalid_branch",
        )
    return chk.stdout.strip() or branch


def _validate_base_ref(settings: Any, canon_repo: str, raw_ref: Any) -> tuple[str, str]:
    """Validate optional `base_ref` and return `(ref_or_HEAD, resolved_commit_sha)`."""
    ref = "HEAD"
    if raw_ref is not None:
        if not isinstance(raw_ref, str):
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                "起始位置需要是文字",
                code="invalid_base_ref",
            )
        cleaned = raw_ref.strip()
        if cleaned:
            if len(cleaned) > MAX_REF_CHARS or cleaned.startswith("-"):
                raise SupervisorServiceError(
                    HTTPStatus.BAD_REQUEST,
                    "起始位置不符合 Git 规则",
                    code="invalid_base_ref",
                )
            if any(tok in cleaned for tok in ("\x00", "\r", "\n", "\t", " ")):
                raise SupervisorServiceError(
                    HTTPStatus.BAD_REQUEST,
                    "起始位置里有不允许的字符",
                    code="invalid_base_ref",
                )
            ref = cleaned
    rev = _run_git(settings, canon_repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], timeout=8.0)
    if rev.returncode != 0 or not rev.stdout.strip():
        msg = (
            "这个仓库还没有任何提交，请先提交一次再新建工作目录"
            if ref == "HEAD"
            else f"找不到 {ref} 对应的提交，请检查起始位置"
        )
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            msg,
            code="invalid_base_ref",
        )
    return ref, rev.stdout.strip()


# ---------------------------------------------------------------------------
# Worktree enumeration, persistence, and session binding
# ---------------------------------------------------------------------------

def _enumerate_git_worktrees(settings: Any, canon_repo: str) -> list[dict[str, Any]]:
    if not Path(canon_repo).is_dir() or not ags.repo_is_git_dir(canon_repo):
        return []
    proc = _run_git(settings, canon_repo, ["worktree", "list", "--porcelain"], timeout=10.0)
    if proc.returncode != 0:
        return []

    entries: list[dict[str, Any]] = []
    current: dict[str, Any] = {}

    def _flush(item: dict[str, Any]) -> None:
        raw_wt = item.get("raw_path")
        if not raw_wt:
            return
        wt_canon = os.path.realpath(raw_wt)
        if not Path(wt_canon).is_dir():
            # Prunable / deleted directory on disk: do not advertise phantom worktrees.
            return
        entries.append({
            "path": wt_canon,
            "head": str(item.get("head") or ""),
            "branch": str(item.get("branch") or ""),
            "detached": bool(item.get("detached", False)),
            "git_locked": bool(item.get("locked", False)),
        })

    for line in proc.stdout.splitlines():
        if not line.strip():
            if current:
                _flush(current)
                current = {}
            continue
        if line.startswith("worktree "):
            if current:
                _flush(current)
            current = {"raw_path": line[len("worktree "):].strip()}
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            if ref.startswith("refs/heads/"):
                current["branch"] = ref[len("refs/heads/"):]
            else:
                current["branch"] = ref
        elif line.strip() == "detached":
            current["detached"] = True
        elif line.startswith("locked"):
            current["locked"] = True
    if current:
        _flush(current)
    return entries


def _ensure_worktree_transcript_session(
    settings: Any,
    *,
    wt_id: str,
    repo_name: str,
    branch_label: str,
    chat_session_id: str | None = None,
) -> str:
    source_chat_id = f"{SUPERVISOR_CHAT_PREFIX}{wt_id}"
    durable_id = chat_session_id or session_identity(WEB_CHANNEL, source_chat_id, WEB_OPERATOR_ID)
    title = f"Code · {repo_name} ({branch_label or 'detached'})"
    get_transcript_store(settings).insert_session_if_absent(
        durable_id,
        channel=WEB_CHANNEL,
        operator_id=WEB_OPERATOR_ID,
        source_chat_id=source_chat_id,
        title=title,
    )
    return durable_id


def _active_tasks_by_worktree(settings: Any) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for task in ags.list_tasks(settings, limit=500):
        if task.get("state") not in _ACTIVE_WORKTREE_STATES:
            continue
        wp = _canonical_internal_path(str(task.get("worktree_path") or ""))
        if wp and wp not in mapping:
            mapping[wp] = task
    return mapping


def _sync_repo_worktrees(
    settings: Any,
    repo_row: dict[str, Any],
    *,
    active_by_wt: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    repo_id = str(repo_row["id"])
    repo_name = str(repo_row["name"])
    canon_repo = str(repo_row["canonical_path"])
    managed_root = _managed_worktree_root(settings)
    git_wts = _enumerate_git_worktrees(settings, canon_repo)
    active_map = active_by_wt if active_by_wt is not None else _active_tasks_by_worktree(settings)

    now = _utc_now()
    conn = _connect(settings)
    db_by_path: dict[str, dict[str, Any]] = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing_rows = conn.execute(
            "SELECT * FROM supervisor_worktrees WHERE repo_id = ?",
            (repo_id,),
        ).fetchall()
        for r in existing_rows:
            db_by_path[str(r["canonical_path"])] = dict(r)

        for gw in git_wts:
            wt_path = gw["path"]
            is_main = wt_path == canon_repo
            row = db_by_path.get(wt_path)
            if row is None:
                # Check if path was registered under another repo record (should not happen)
                other = conn.execute(
                    "SELECT * FROM supervisor_worktrees WHERE canonical_path = ?",
                    (wt_path,),
                ).fetchone()
                if other is not None:
                    row = dict(other)
                    db_by_path[wt_path] = row
                else:
                    wt_id = f"wt-{os.urandom(6).hex()}"
                    source_chat_id = f"{SUPERVISOR_CHAT_PREFIX}{wt_id}"
                    chat_sid = session_identity(WEB_CHANNEL, source_chat_id, WEB_OPERATOR_ID)
                    wt_p = Path(wt_path)
                    in_managed_root = (not is_main) and (
                        wt_p == managed_root or managed_root in wt_p.parents
                    )
                    valid_wt = False
                    if in_managed_root:
                        valid_wt, _ = ags.validate_worktree(settings, canon_repo, wt_path)
                    managed_int = 1 if (in_managed_root and valid_wt) else 0
                    conn.execute(
                        """INSERT INTO supervisor_worktrees
                           (id, repo_id, canonical_path, managed, chat_session_id, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (wt_id, repo_id, wt_path, managed_int, chat_sid, now, now),
                    )
                    row = {
                        "id": wt_id,
                        "repo_id": repo_id,
                        "canonical_path": wt_path,
                        "managed": managed_int,
                        "chat_session_id": chat_sid,
                        "created_at": now,
                        "updated_at": now,
                    }
                    db_by_path[wt_path] = row
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    results: list[dict[str, Any]] = []
    for gw in git_wts:
        wt_path = gw["path"]
        row = db_by_path.get(wt_path)
        if not row:
            continue
        is_main = wt_path == canon_repo
        branch_display = gw["branch"] or ("detached" if gw["detached"] else "")
        wt_id = str(row["id"])
        chat_sid = _ensure_worktree_transcript_session(
            settings,
            wt_id=wt_id,
            repo_name=repo_name,
            branch_label=branch_display or "detached",
            chat_session_id=str(row["chat_session_id"]) if row.get("chat_session_id") else None,
        )
        is_managed = False
        if not is_main and bool(row.get("managed")):
            wt_p = Path(wt_path)
            if wt_p == managed_root or managed_root in wt_p.parents:
                valid_ok, _ = ags.validate_worktree(settings, canon_repo, wt_path)
                is_managed = bool(valid_ok)

        active_task = active_map.get(wt_path)
        active_task_id = str(active_task["id"]) if active_task else None
        has_runtime_lock = bool(active_task_id) or ags.has_active_task_for_worktree(settings, wt_path)
        is_locked = bool(gw["git_locked"] or has_runtime_lock)

        results.append({
            "id": wt_id,
            "repo_id": repo_id,
            "path": wt_path,
            "branch": branch_display or "detached",
            "head": gw["head"],
            "is_main": is_main,
            "managed": is_managed,
            "locked": is_locked,
            "active_task_id": active_task_id,
            "chat_session_id": chat_sid,
        })

    # Sort: main worktree first, then managed worktrees (newest/alphabetical), then unmanaged.
    results.sort(key=lambda w: (0 if w["is_main"] else (1 if w["managed"] else 2), w["branch"], w["id"]))
    return results


def _format_repository(
    settings: Any,
    repo_row: dict[str, Any],
    *,
    active_by_wt: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    canon_repo = str(repo_row["canonical_path"])
    worktrees = _sync_repo_worktrees(settings, repo_row, active_by_wt=active_by_wt)
    return {
        "id": str(repo_row["id"]),
        "name": str(repo_row["name"]),
        "path": canon_repo,
        "validation_source": _repo_validation_source(settings, str(repo_row["id"])),
        "default_branch": _detect_default_branch(settings, canon_repo),
        "created_at": str(repo_row["created_at"]),
        "worktrees": worktrees,
    }


def _repo_validation_source(settings: Any, repo_id: str) -> str:
    """Per-repository validation source: 'env' | 'repo' | 'none'.

    Lets the UI warn about missing checks without an extra call. Lazy import
    keeps supervisor_validation (which lazily imports this module) cycle-free.
    """
    try:
        from conveyor.agents import supervisor_validation

        return supervisor_validation.compute_validation_source(settings, repo_id)
    except Exception:
        return "none"


def register_repository(
    settings: Any,
    *,
    path: Any,
    name: Any = None,
) -> tuple[dict[str, Any], bool]:
    """Validate and register a canonical git repository root. Idempotent on canonical duplicates."""
    require_enabled(settings)
    canon_repo = validate_git_repository_root(settings, path)

    clean_name = ""
    if name is not None:
        if not isinstance(name, str):
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                "名称需要是文字",
                code="invalid_name",
            )
        clean_name = name.strip()
        if len(clean_name) > MAX_NAME_CHARS or any(ch in clean_name for ch in ("\x00", "\r", "\n")):
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                f"名称长度不能超过 {MAX_NAME_CHARS} 个字符",
                code="invalid_name",
            )
    repo_name = clean_name or Path(canon_repo).name or "repository"

    now = _utc_now()
    created = False
    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM supervisor_repositories WHERE canonical_path = ?",
            (canon_repo,),
        ).fetchone()
        if existing is not None:
            repo_row = dict(existing)
            if clean_name and clean_name != repo_row["name"]:
                conn.execute(
                    "UPDATE supervisor_repositories SET name = ?, updated_at = ? WHERE id = ?",
                    (clean_name, now, repo_row["id"]),
                )
                repo_row["name"] = clean_name
                repo_row["updated_at"] = now
            conn.commit()
        else:
            repo_id = f"repo-{os.urandom(6).hex()}"
            conn.execute(
                """INSERT INTO supervisor_repositories
                   (id, name, canonical_path, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (repo_id, repo_name, canon_repo, now, now),
            )
            repo_row = {
                "id": repo_id,
                "name": repo_name,
                "canonical_path": canon_repo,
                "created_at": now,
                "updated_at": now,
            }
            created = True
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return _format_repository(settings, repo_row), created


def list_repositories(settings: Any) -> list[dict[str, Any]]:
    require_enabled(settings)
    conn = _connect(settings)
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM supervisor_repositories ORDER BY created_at ASC, id ASC"
            ).fetchall()
        ]
    finally:
        conn.close()
    active_map = _active_tasks_by_worktree(settings)
    return [_format_repository(settings, row, active_by_wt=active_map) for row in rows]


def _get_repo_row(settings: Any, repo_id: str) -> dict[str, Any]:
    rid = _validate_id_token(repo_id, "repo_id")
    conn = _connect(settings)
    try:
        row = conn.execute(
            "SELECT * FROM supervisor_repositories WHERE id = ?",
            (rid,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SupervisorServiceError(
            HTTPStatus.NOT_FOUND,
            f"找不到这个仓库（{rid}）",
            code="repository_not_found",
        )
    return dict(row)


def create_managed_worktree(
    settings: Any,
    repo_id: str,
    *,
    branch: Any = None,
    base_ref: Any = None,
) -> dict[str, Any]:
    """Create a real git worktree under the runtime's managed supervisor root."""
    require_enabled(settings)
    repo_row = _get_repo_row(settings, repo_id)
    canon_repo = str(repo_row["canonical_path"])
    if not Path(canon_repo).is_dir() or not ags.repo_is_git_dir(canon_repo):
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            "这个仓库在磁盘上已经不见了，或者不再是 Git 仓库",
            code="invalid_repository",
        )

    req_branch = _validate_branch_name(settings, canon_repo, branch) if branch is not None else ""
    ref_arg, resolved_base_sha = _validate_base_ref(settings, canon_repo, base_ref)

    managed_root = _managed_worktree_root(settings)
    managed_root.mkdir(parents=True, exist_ok=True)
    max_wt = int(getattr(settings, "agent_supervisor_max_worktrees", 32) or 32)
    existing_dirs = [p for p in managed_root.iterdir() if p.is_dir()]
    if len(existing_dirs) >= max_wt:
        raise SupervisorServiceError(
            HTTPStatus.CONFLICT,
            f"工作目录数量到上限了（{max_wt} 个），请先删掉不用的",
            code="worktree_limit_reached",
        )
    raw_budget = os.getenv("CONVEYOR_MAX_WORKTREES_BYTES", "")
    budget_bytes = (
        int(raw_budget)
        if raw_budget.strip().isdigit()
        else int(getattr(settings, "conveyor_max_worktrees_bytes", 0) or 0)
    )
    if budget_bytes > 0:
        total_bytes = 0
        for p in managed_root.rglob("*"):
            try:
                if p.is_file():
                    total_bytes += p.stat().st_size
            except OSError:
                continue
            if total_bytes > budget_bytes:
                raise SupervisorServiceError(
                    HTTPStatus.CONFLICT,
                    "工作目录占用的空间超出上限了，请先删掉不用的",
                    code="worktree_budget_exceeded",
                )

    wt_token = os.urandom(6).hex()
    wt_id = f"wt-{wt_token}"
    eff_branch = req_branch or f"sup/{wt_token[:8]}"
    wt_dir = managed_root / f"sup-{wt_token}"

    branch_exists = (
        _run_git(
            settings,
            canon_repo,
            ["show-ref", "--verify", "--quiet", f"refs/heads/{eff_branch}"],
            timeout=5.0,
        ).returncode
        == 0
    )
    if branch_exists:
        if base_ref is not None and str(base_ref).strip():
            branch_rev = _run_git(
                settings,
                canon_repo,
                ["rev-parse", "--verify", "--quiet", f"refs/heads/{eff_branch}^{{commit}}"],
                timeout=5.0,
            )
            if branch_rev.returncode == 0 and branch_rev.stdout.strip() != resolved_base_sha:
                raise SupervisorServiceError(
                    HTTPStatus.CONFLICT,
                    f"分支 {eff_branch} 已经存在，而且指向的不是同一个提交",
                    code="branch_conflict",
                )
        add_cmd = ["worktree", "add", str(wt_dir), eff_branch]
    else:
        add_cmd = ["worktree", "add", "-b", eff_branch, str(wt_dir), ref_arg]

    proc = _run_git(settings, canon_repo, add_cmd, timeout=30.0)
    if proc.returncode != 0:
        err_detail = redact_text((proc.stderr or proc.stdout or "git worktree add failed").strip())
        status = (
            HTTPStatus.CONFLICT
            if "already checked out" in err_detail or "already exists" in err_detail
            else HTTPStatus.BAD_REQUEST
        )
        raise SupervisorServiceError(
            status,
            f"新建工作目录失败：{truncate(err_detail, 280)}",
            code="worktree_create_failed",
        )

    wt_canon = os.path.realpath(str(wt_dir))
    valid_ok, valid_reason = ags.validate_worktree(settings, canon_repo, wt_canon)
    if not valid_ok:
        _run_git(settings, canon_repo, ["worktree", "remove", "--force", wt_canon], timeout=10.0)
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            f"新建的工作目录没有通过安全检查：{valid_reason}",
            code="worktree_policy_violation",
        )

    now = _utc_now()
    source_chat_id = f"{SUPERVISOR_CHAT_PREFIX}{wt_id}"
    chat_sid = session_identity(WEB_CHANNEL, source_chat_id, WEB_OPERATOR_ID)

    conn = _connect(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO supervisor_worktrees
               (id, repo_id, canonical_path, managed, chat_session_id, created_at, updated_at, branch)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?)
               ON CONFLICT(canonical_path) DO UPDATE SET
                 managed = 1,
                 branch = excluded.branch,
                 updated_at = excluded.updated_at""",
            (wt_id, str(repo_row["id"]), wt_canon, chat_sid, now, now, eff_branch),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    synced = _sync_repo_worktrees(settings, repo_row)
    for item in synced:
        if item["path"] == wt_canon:
            return item
    raise SupervisorServiceError(
        HTTPStatus.INTERNAL_SERVER_ERROR,
        "工作目录已经建好，但读取不到它的信息",
    )


# ---------------------------------------------------------------------------
# Supervisor-bound chat session resolution & read-only context
# ---------------------------------------------------------------------------

def is_supervisor_session_id(session_id: Any) -> bool:
    if not isinstance(session_id, str):
        return False
    s = session_id.strip()
    return s.startswith(SUPERVISOR_CHAT_PREFIX) or s.startswith(SUPERVISOR_DURABLE_PREFIX)


def resolve_supervisor_chat_session(
    settings: Any,
    session_or_chat_id: str,
) -> dict[str, Any] | None:
    """Resolve a `supwt-*` or `web:web-console:supwt-*` session back to its
    verified `(repo, worktree)` binding. Returns `None` if disabled, unknown,
    or if the underlying git repository/worktree no longer exists.
    """
    if not is_enabled(settings) or not isinstance(session_or_chat_id, str):
        return None
    raw = session_or_chat_id.strip()
    if raw.startswith(f"{WEB_CHANNEL}:{WEB_OPERATOR_ID}:"):
        source_chat_id = raw[len(f"{WEB_CHANNEL}:{WEB_OPERATOR_ID}:"):]
    else:
        source_chat_id = raw
    if not source_chat_id.startswith(SUPERVISOR_CHAT_PREFIX):
        return None
    durable_id = session_identity(WEB_CHANNEL, source_chat_id, WEB_OPERATOR_ID)

    conn = _connect(settings)
    try:
        row = conn.execute(
            """SELECT w.id AS wt_id, w.repo_id, w.canonical_path AS wt_path,
                      w.managed, w.chat_session_id,
                      r.name AS repo_name, r.canonical_path AS repo_path
               FROM supervisor_worktrees w
               JOIN supervisor_repositories r ON r.id = w.repo_id
               WHERE w.chat_session_id = ?""",
            (durable_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None

    repo_path = str(row["repo_path"])
    wt_path = str(row["wt_path"])
    if not Path(repo_path).is_dir() or not Path(wt_path).is_dir():
        return None
    if not ags.repo_is_git_dir(repo_path) or not ags.is_git_worktree(wt_path):
        return None
    is_main = wt_path == repo_path
    if not is_main and not ags.registered_worktree_of(repo_path, wt_path):
        return None

    branch = ""
    head = ""
    br_proc = _run_git(settings, wt_path, ["symbolic-ref", "--short", "-q", "HEAD"], timeout=4.0)
    if br_proc.returncode == 0 and br_proc.stdout.strip():
        branch = br_proc.stdout.strip()
    hd_proc = _run_git(settings, wt_path, ["rev-parse", "--verify", "-q", "HEAD"], timeout=4.0)
    if hd_proc.returncode == 0 and hd_proc.stdout.strip():
        head = hd_proc.stdout.strip()

    managed = False
    if not is_main and bool(row["managed"]):
        ok_m, _ = ags.validate_worktree(settings, repo_path, wt_path)
        managed = bool(ok_m)

    return {
        "session_id": durable_id,
        "source_chat_id": source_chat_id,
        "repo_id": str(row["repo_id"]),
        "repo_name": str(row["repo_name"]),
        "repo_path": repo_path,
        "worktree_id": str(row["wt_id"]),
        "worktree_path": wt_path,
        "branch": branch or "detached",
        "head": head,
        "is_main": is_main,
        "managed": managed,
    }


def build_supervisor_chat_context(settings: Any, binding: dict[str, Any]) -> str:
    """Build a concise, read-only repository/worktree snapshot for coordinator chat."""
    wt_path = str(binding["worktree_path"])
    status_lines = "(clean)"
    st = _run_git(settings, wt_path, ["status", "--short", "--untracked-files=normal"], timeout=4.0)
    if st.returncode == 0 and st.stdout.strip():
        lines = st.stdout.strip().splitlines()[:25]
        status_lines = "\n".join(lines)

    log_lines = "(none)"
    lg = _run_git(settings, wt_path, ["log", "-n", "5", "--oneline"], timeout=4.0)
    if lg.returncode == 0 and lg.stdout.strip():
        log_lines = "\n".join(lg.stdout.strip().splitlines()[:5])

    mode_desc = (
        "managed worktree (eligible for explicit task dispatch)"
        if binding.get("managed")
        else "read-only worktree (main or unmanaged; operator must create/select a managed worktree before dispatching writes)"
    )
    return redact_text(
        "Supervisor Workspace Context (verified backend binding):\n"
        f"- Repository: {binding['repo_name']} ({binding['repo_path']})\n"
        f"- Worktree: {wt_path}\n"
        f"- Branch: {binding['branch']}\n"
        f"- HEAD SHA: {binding.get('head') or 'unknown'}\n"
        f"- Worktree Mode: {mode_desc}\n"
        f"- Git Status (read-only snapshot):\n{status_lines}\n"
        f"- Recent Commits:\n{log_lines}"
    )


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

def _source_hostname() -> str:
    try:
        host = socket.gethostname().strip()
        if host:
            return host
    except Exception:
        pass
    try:
        return os.uname().nodename.strip() or "unknown"
    except Exception:
        return "unknown"


def get_capabilities(settings: Any) -> dict[str, Any]:
    require_enabled(settings)
    from runner.chat_client import config_from_settings

    roles_cfg = ags.load_roles(settings)
    role_sources = ags.role_executor_sources(settings)
    chat_cfg = config_from_settings(settings)
    coord_role = roles_cfg["coordinator"]
    coord_ok, coord_reason = ags.get_executor(settings, coord_role.executor).available()
    coord_model = (
        chat_cfg.model
        if (coord_role.executor == "chat" and chat_cfg is not None)
        else (coord_role.model or None)
    )

    roles_out: dict[str, Any] = {}
    for role_name in ("analyst", "implementer", "reviewer"):
        rc = roles_cfg[role_name]
        ok, reason = ags.get_executor(settings, rc.executor).available()
        entry: dict[str, Any] = {
            "executor": rc.executor,
            "model": rc.model or None,
            "location": rc.location,
            "available": bool(ok),
            # True when no operator configuration named this executor and the
            # server picked the first usable one for the owner.
            "auto_selected": role_sources.get(role_name) == "auto",
        }
        if not ok and reason:
            entry["reason"] = redact_text(reason)
        roles_out[role_name] = entry

    executors_out: list[dict[str, Any]] = []
    for exec_name, label in _UI_EXPOSED_EXECUTORS:
        adapter = ags.get_executor(settings, exec_name)
        ok, reason = adapter.available()
        item: dict[str, Any] = {
            "name": exec_name,
            "label": label,
            "available": bool(ok),
        }
        if not ok and reason:
            item["reason"] = redact_text(reason)
        executors_out.append(item)

    host = _source_hostname()
    coordinator_info: dict[str, Any] = {
        "executor": coord_role.executor,
        "model": coord_model,
        "available": bool(coord_ok),
    }
    if not coord_ok and coord_reason:
        coordinator_info["reason"] = redact_text(coord_reason)

    return {
        "enabled": True,
        "coordinator": coordinator_info,
        "roles": roles_out,
        "executors": executors_out,
        "deployment": {
            "configured": False,
            "can_start": False,
            "reason": "这台机器上还没有配置远程执行节点。",
            "source_machine": host,
        },
        "source_machine": host,
    }


# ---------------------------------------------------------------------------
# Scoped Tasks (Create, Dispatch, List, Detail, Cancel)
# ---------------------------------------------------------------------------

def _resolve_repo_and_worktree(
    settings: Any,
    *,
    repo_id: Any = None,
    worktree_id: Any = None,
    legacy_repo_path: Any = None,
    legacy_worktree_path: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if repo_id or worktree_id:
        if not repo_id or not worktree_id:
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                "请同时选择仓库和工作目录",
                code="missing_scope",
            )
        repo_row = _get_repo_row(settings, str(repo_id))
        wid = _validate_id_token(worktree_id, "worktree_id")
        synced = _sync_repo_worktrees(settings, repo_row)
        wt = next((item for item in synced if item["id"] == wid), None)
        if wt is None:
            # Distinguish cross-repo mismatch (400) from unknown worktree (404)
            conn = _connect(settings)
            try:
                other = conn.execute(
                    "SELECT repo_id FROM supervisor_worktrees WHERE id = ?",
                    (wid,),
                ).fetchone()
            finally:
                conn.close()
            if other is not None:
                raise SupervisorServiceError(
                    HTTPStatus.BAD_REQUEST,
                    "这个工作目录不属于选择的仓库",
                    code="scope_mismatch",
                )
            raise SupervisorServiceError(
                HTTPStatus.NOT_FOUND,
                f"worktree not found: {wid}",
                code="worktree_not_found",
            )
        return _format_repository(settings, repo_row), wt

    if legacy_repo_path and legacy_worktree_path:
        canon_repo = _canonicalize_user_path(legacy_repo_path, "repo_path")
        canon_wt = _canonicalize_user_path(legacy_worktree_path, "worktree_path")
        conn = _connect(settings)
        try:
            row = conn.execute(
                "SELECT * FROM supervisor_repositories WHERE canonical_path = ?",
                (canon_repo,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                "请先注册这个仓库，再新建任务",
                code="unregistered_repository",
            )
        repo_fmt = _format_repository(settings, dict(row))
        wt = next((w for w in repo_fmt["worktrees"] if w["path"] == canon_wt), None)
        if wt is None:
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                "这个工作目录不属于已注册的仓库",
                code="invalid_worktree",
            )
        return repo_fmt, wt

    raise SupervisorServiceError(
        HTTPStatus.BAD_REQUEST,
        "请选择仓库和工作目录",
        code="missing_scope",
    )


def _lookup_scope_ids_by_paths(settings: Any) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """Return `(repo_path -> repo_id, wt_path -> (repo_id, wt_id))` for safe task view enrichment."""
    conn = _connect(settings)
    try:
        repos = {
            str(r["canonical_path"]): str(r["id"])
            for r in conn.execute("SELECT id, canonical_path FROM supervisor_repositories").fetchall()
        }
        wts = {
            str(w["canonical_path"]): (str(w["repo_id"]), str(w["id"]))
            for w in conn.execute("SELECT id, repo_id, canonical_path FROM supervisor_worktrees").fetchall()
        }
        return repos, wts
    finally:
        conn.close()


def _enrich_task_dict(
    settings: Any,
    task: dict[str, Any],
    *,
    path_maps: tuple[dict[str, str], dict[str, tuple[str, str]]] | None = None,
    include_changed_files: bool = False,
) -> dict[str, Any]:
    out = dict(task)
    meta = dict(out.get("metadata") or {})
    if out.get("error"):
        out["error"] = redact_text(str(out["error"]))

    if not meta.get("repo_id") or not meta.get("worktree_id"):
        repo_map, wt_map = path_maps or _lookup_scope_ids_by_paths(settings)
        canon_rp = _canonical_internal_path(str(out.get("repo_path") or ""))
        canon_wp = _canonical_internal_path(str(out.get("worktree_path") or ""))
        if not meta.get("repo_id") and canon_rp in repo_map:
            meta["repo_id"] = repo_map[canon_rp]
        if not meta.get("worktree_id") and canon_wp in wt_map:
            rid, wid = wt_map[canon_wp]
            meta.setdefault("repo_id", rid)
            meta["worktree_id"] = wid

    if include_changed_files and "changed_files" not in meta:
        wp = _canonical_internal_path(str(out.get("worktree_path") or ""))
        base_sha = str(out.get("base_sha") or "").strip()
        if wp and Path(wp).is_dir() and ags.is_git_worktree(wp):
            changed: list[str] = []
            if base_sha:
                diff = _run_git(settings, wp, ["diff", "--name-only", base_sha], timeout=4.0)
                if diff.returncode == 0:
                    changed.extend(ln.strip() for ln in diff.stdout.splitlines() if ln.strip())
            untracked = _run_git(settings, wp, ["ls-files", "--others", "--exclude-standard"], timeout=4.0)
            if untracked.returncode == 0:
                for ln in untracked.stdout.splitlines():
                    clean_ln = ln.strip()
                    if clean_ln and clean_ln not in changed:
                        changed.append(clean_ln)
            meta["changed_files"] = changed[:100]

    out["metadata"] = meta
    return out


MAX_SNAPSHOT_TURNS = 8
MAX_SNAPSHOT_BYTES = 16 * 1024  # ~16 KiB serialized UTF-8
DISCUSSION_KINDS: tuple[str | None, ...] = (None, "chat", "task_suggestion")


def capture_discussion_snapshot(
    settings: Any,
    session_id: str,
    *,
    max_turns: int = MAX_SNAPSHOT_TURNS,
    max_bytes: int = MAX_SNAPSHOT_BYTES,
    kinds: tuple[str | None, ...] | list[str | None] | set[str | None] | None = DISCUSSION_KINDS,
) -> dict[str, Any]:
    """Capture a frozen snapshot of recent discussion turns for a session.

    API-to-core contract:
      metadata.discussion_snapshot = {
        session_id: actual verified worktree chat session,
        captured_at: UTC ISO timestamp,
        messages: [{id: string, role: 'user'|'assistant', text: string}],
        sha256: SHA256 of json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(',',':')).encode('utf-8'),
        truncated: bool
      }
    """
    store = get_transcript_store(settings)
    allowed_roles = ("user", "assistant")

    try:
        total_count = store.count_messages(session_id, roles=allowed_roles, kinds=kinds)
        recent_rows = store.recent_messages(session_id, limit=max_turns, roles=allowed_roles, kinds=kinds)
    except Exception as exc:
        log.error("Failed to read discussion transcript for session %s: %s", session_id, exc)
        raise SupervisorServiceError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            f"读不到这个会话的记录：{exc}",
            code="transcript_read_error",
        ) from exc

    truncated = total_count > len(recent_rows)
    formatted: list[dict[str, str]] = []
    for item in recent_rows:
        raw_text = str(item.get("content") or "")
        clean_text = redact_text(raw_text).strip()
        formatted.append({
            "id": str(item.get("id") or ""),
            "role": str(item.get("role") or ""),
            "text": clean_text,
        })

    def _serialize(msgs: list[dict[str, str]]) -> bytes:
        return json.dumps(
            msgs,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    # If serialized messages exceed byte budget, drop oldest turns first
    # to retain the most meaningful recent context.
    if len(_serialize(formatted)) > max_bytes:
        truncated = True
        while len(formatted) > 1 and len(_serialize(formatted)) > max_bytes:
            formatted.pop(0)

        # If a single message still exceeds max_bytes, truncate text to fit
        if len(formatted) == 1 and len(_serialize(formatted)) > max_bytes:
            single = formatted[0]
            overhead = len(_serialize([{"id": single["id"], "role": single["role"], "text": ""}]))
            avail = max(0, max_bytes - overhead)
            single["text"] = truncate(single["text"], avail)
            while len(_serialize(formatted)) > max_bytes and len(single["text"]) > 10:
                single["text"] = single["text"][:-50]

    serialized_final = _serialize(formatted)
    digest = hashlib.sha256(serialized_final).hexdigest()

    return {
        "session_id": session_id,
        "captured_at": _utc_now(),
        "messages": formatted,
        "sha256": digest,
        "truncated": bool(truncated),
    }


def create_scoped_task(settings: Any, body: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Create a validated `pending_dispatch` task without starting execution.
    Idempotent on `request_id` when payload matches.
    """
    require_enabled(settings)
    raw_goal = body.get("goal")
    if not isinstance(raw_goal, str):
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            "请填写要做什么",
            code="invalid_goal",
        )
    goal = raw_goal.strip()
    if not goal or len(goal) > MAX_GOAL_CHARS:
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            f"目标长度需要在 1-{MAX_GOAL_CHARS} 个字符之间",
            code="invalid_goal",
        )

    raw_criteria = body.get("acceptance_criteria")
    acceptance_criteria = ""
    if raw_criteria is not None:
        if not isinstance(raw_criteria, str):
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                "完成标准需要是文字",
                code="invalid_acceptance_criteria",
            )
        acceptance_criteria = raw_criteria.strip()
        if len(acceptance_criteria) > MAX_CRITERIA_CHARS:
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                f"完成标准长度不能超过 {MAX_CRITERIA_CHARS} 个字符",
                code="invalid_acceptance_criteria",
            )

    raw_request_id = body.get("request_id")
    request_id = ""
    if raw_request_id is not None and str(raw_request_id).strip():
        request_id = _validate_id_token(raw_request_id, "request_id")
        if len(request_id) > MAX_REQUEST_ID_CHARS:
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                f"请求编号长度不能超过 {MAX_REQUEST_ID_CHARS} 个字符",
                code="invalid_request_id",
            )

    repo, wt = _resolve_repo_and_worktree(
        settings,
        repo_id=body.get("repo_id"),
        worktree_id=body.get("worktree_id"),
        legacy_repo_path=body.get("repo_path"),
        legacy_worktree_path=body.get("worktree_path"),
    )

    if wt["is_main"] or not wt["managed"]:
        raise SupervisorServiceError(
            HTTPStatus.CONFLICT,
            "任务只能在新建的工作目录里跑；主仓库和外部工作目录是只读的",
            code="unmanaged_worktree",
        )

    ok_wt, reason_wt = ags.validate_worktree(settings, repo["path"], wt["path"])
    if not ok_wt:
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            f"这个工作目录没有通过检查：{reason_wt}",
            code="invalid_worktree",
        )

    raw_session_id = body.get("session_id")
    if raw_session_id is not None and str(raw_session_id).strip():
        if not isinstance(raw_session_id, str):
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                "会话编号需要是文字",
                code="invalid_session",
            )
        supplied_sid = raw_session_id.strip()
        if supplied_sid.startswith(SUPERVISOR_CHAT_PREFIX):
            supplied_sid = session_identity(WEB_CHANNEL, supplied_sid, WEB_OPERATOR_ID)
        if supplied_sid != wt["chat_session_id"]:
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                "会话编号和选择的仓库/工作目录不一致",
                code="session_mismatch",
            )

    payload_digest = hashlib.sha256(
        json.dumps(
            [repo["id"], wt["id"], goal, acceptance_criteria],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    if request_id:
        conn = _connect(settings)
        try:
            existing_req = conn.execute(
                "SELECT * FROM supervisor_task_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        finally:
            conn.close()
        if existing_req is not None:
            if str(existing_req["payload_sha256"]) != payload_digest:
                raise SupervisorServiceError(
                    HTTPStatus.CONFLICT,
                    "这个请求编号已经用过，而且参数不一样",
                    code="request_id_conflict",
                )
            existing_task = ags.get_task(settings, str(existing_req["task_id"]))
            if existing_task is not None:
                return _enrich_task_dict(settings, existing_task), False

    if wt["locked"] or ags.has_active_task_for_worktree(settings, wt["path"]):
        raise SupervisorServiceError(
            HTTPStatus.CONFLICT,
            "这个工作目录已经有任务在跑，或者还锁着",
            code="worktree_locked",
        )

    meta: dict[str, Any] = {
        "repo_id": repo["id"],
        "repo_name": repo["name"],
        "worktree_id": wt["id"],
        "session_id": wt["chat_session_id"],
    }
    if acceptance_criteria:
        meta["acceptance_criteria"] = acceptance_criteria
    if request_id:
        meta["request_id"] = request_id

    # Server-side snapshot of verified worktree chat discussion (never taken from browser body)
    meta["discussion_snapshot"] = capture_discussion_snapshot(settings, wt["chat_session_id"])

    branch_for_task = "" if wt["branch"] == "detached" else wt["branch"]
    try:
        task = ags.create_task(
            settings,
            goal=goal,
            repo_path=repo["path"],
            worktree_path=wt["path"],
            branch=branch_for_task,
            base_sha=wt["head"],
            metadata=meta,
        )
    except (ags.SupervisorStateError, ags.SupervisorLockError) as exc:
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            str(exc),
            code="task_create_failed",
        ) from exc

    if request_id:
        now = _utc_now()
        conn = _connect(settings)
        try:
            conn.execute("BEGIN IMMEDIATE")
            race_row = conn.execute(
                "SELECT * FROM supervisor_task_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if race_row is not None:
                conn.rollback()
                if str(race_row["payload_sha256"]) != payload_digest:
                    raise SupervisorServiceError(
                        HTTPStatus.CONFLICT,
                        "这个请求编号已经用过，而且参数不一样",
                        code="request_id_conflict",
                    )
                winner = ags.get_task(settings, str(race_row["task_id"]))
                if winner is not None:
                    return _enrich_task_dict(settings, winner), False
            else:
                conn.execute(
                    """INSERT INTO supervisor_task_requests
                       (request_id, repo_id, worktree_id, task_id, payload_sha256, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (request_id, repo["id"], wt["id"], task["id"], payload_digest, now),
                )
                conn.commit()
        except SupervisorServiceError:
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return _enrich_task_dict(settings, task), True


def _sanitize_role_overrides(raw_roles: Any) -> dict[str, dict[str, Any]] | None:
    """Strictly validate client role overrides: ONLY `executor` and `model` on
    `analyst` / `implementer` / `reviewer`. Reject any attempt to override
    `tools`, `location`, or permission flags.
    """
    if raw_roles is None:
        return None
    if not isinstance(raw_roles, dict):
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            "角色设置的格式不对",
            code="invalid_role_override",
        )
    allowed_executors = {name for name, _ in _UI_EXPOSED_EXECUTORS}
    cleaned: dict[str, dict[str, Any]] = {}
    for role_name, spec in raw_roles.items():
        if role_name not in _OVERRIDABLE_ROLES:
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                f"角色 {role_name} 不能在页面上改",
                code="invalid_role_override",
            )
        if isinstance(spec, str):
            exec_name = spec.strip()
            model_val: str | None = None
        elif isinstance(spec, dict):
            forbidden_keys = set(spec.keys()) - {"executor", "model"}
            if forbidden_keys:
                raise SupervisorServiceError(
                    HTTPStatus.BAD_REQUEST,
                    f"角色 {role_name} 里有不能改的项："
                    f"{', '.join(sorted(str(k) for k in forbidden_keys))}",
                    code="invalid_role_override",
                )
            raw_exec = spec.get("executor")
            if not isinstance(raw_exec, str) or not raw_exec.strip():
                raise SupervisorServiceError(
                    HTTPStatus.BAD_REQUEST,
                    f"请为角色 {role_name} 选择执行者",
                    code="invalid_role_override",
                )
            exec_name = raw_exec.strip()
            raw_model = spec.get("model")
            if raw_model is None or raw_model == "":
                model_val = None
            elif isinstance(raw_model, str):
                m_clean = raw_model.strip()
                if not m_clean:
                    model_val = None
                else:
                    if len(m_clean) > MAX_MODEL_CHARS or m_clean.startswith("-") or not _MODEL_NAME_RE.match(m_clean):
                        raise SupervisorServiceError(
                            HTTPStatus.BAD_REQUEST,
                            f"角色 {role_name} 的模型名称不正确",
                            code="invalid_model",
                        )
                    model_val = m_clean
            else:
                raise SupervisorServiceError(
                    HTTPStatus.BAD_REQUEST,
                    f"角色 {role_name} 的模型名称需要是文字，或者留空",
                    code="invalid_model",
                )
        else:
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                f"角色 {role_name} 的设置格式不对",
                code="invalid_role_override",
            )

        if exec_name not in allowed_executors:
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                f"执行者 {exec_name} 不能用于角色 {role_name}",
                code="invalid_executor",
            )
        role_out: dict[str, Any] = {"executor": exec_name}
        if model_val is not None:
            role_out["model"] = model_val
        cleaned[role_name] = role_out
    return cleaned or None


def dispatch_scoped_task(
    server: Any,
    settings: Any,
    task_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Validate, durably queue (`queue_task`), and wake the server-owned worker."""
    require_enabled(settings)
    tid = _validate_id_token(task_id, "task_id")
    task = ags.get_task(settings, tid)
    if task is None:
        raise SupervisorServiceError(
            HTTPStatus.NOT_FOUND,
            f"找不到这个任务（{tid}）",
            code="task_not_found",
        )

    # Reject browser attempts to mutate worktree_path or repo_path at dispatch time.
    for immutable_field in ("repo_path", "worktree_path"):
        if immutable_field in body and body[immutable_field] is not None:
            supplied = _canonicalize_user_path(body[immutable_field], immutable_field)
            if supplied != _canonical_internal_path(str(task.get(immutable_field) or "")):
                raise SupervisorServiceError(
                    HTTPStatus.BAD_REQUEST,
                    "开始执行时不能改仓库或工作目录",
                    code="immutable_scope",
                )

    repo_path = _canonical_internal_path(str(task.get("repo_path") or ""))
    wt_path = _canonical_internal_path(str(task.get("worktree_path") or ""))
    if not repo_path or not wt_path:
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            "这个任务没有绑定仓库或工作目录",
            code="invalid_worktree",
        )
    if wt_path == repo_path:
        raise SupervisorServiceError(
            HTTPStatus.CONFLICT,
            "不能直接在主仓库上跑任务，请先新建一个工作目录",
            code="unmanaged_worktree",
        )
    ok_wt, reason_wt = ags.validate_worktree(settings, repo_path, wt_path)
    if not ok_wt:
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            f"这个工作目录没有通过检查：{reason_wt}",
            code="invalid_worktree",
        )

    # Verify worktree is marked managed in our registry when registered.
    conn = _connect(settings)
    try:
        wt_row = conn.execute(
            "SELECT managed FROM supervisor_worktrees WHERE canonical_path = ?",
            (wt_path,),
        ).fetchone()
    finally:
        conn.close()
    if wt_row is not None and not bool(wt_row["managed"]):
        raise SupervisorServiceError(
            HTTPStatus.CONFLICT,
            "不能在别人的工作目录上派发任务，请选自己新建的工作目录",
            code="unmanaged_worktree",
        )

    raw_criteria = body.get("acceptance_criteria")
    acceptance_criteria = ""
    if raw_criteria is not None:
        if not isinstance(raw_criteria, str) or len(raw_criteria.strip()) > MAX_CRITERIA_CHARS:
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                f"完成标准需要是文字，且不超过 {MAX_CRITERIA_CHARS} 个字符",
                code="invalid_acceptance_criteria",
            )
        acceptance_criteria = raw_criteria.strip()

    sanitized_roles = _sanitize_role_overrides(body.get("roles"))

    state = str(task.get("state") or "")
    if state in _INFLIGHT_STATES:
        # Idempotent retry: already queued or running; ensure worker is awake and return 202.
        ensure_server_worker(server, settings, wake=True)
        return _enrich_task_dict(settings, task)

    if state in ags.TERMINAL_STATES:
        raise SupervisorServiceError(
            HTTPStatus.CONFLICT,
            f"这个任务已经结束了（{state}）",
            code="task_terminal",
        )

    try:
        queued = ags.queue_task(
            settings,
            tid,
            worktree_path=wt_path,
            branch=str(task.get("branch") or "") or None,
            acceptance_criteria=acceptance_criteria,
            roles=sanitized_roles,
        )
    except ags.SupervisorLockError as exc:
        raise SupervisorServiceError(
            HTTPStatus.CONFLICT,
            str(exc),
            code="worktree_locked",
        ) from exc
    except ags.SupervisorStateError as exc:
        raise SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            str(exc),
            code="dispatch_rejected",
        ) from exc

    ensure_server_worker(server, settings, wake=True)
    return _enrich_task_dict(settings, queued)


def list_scoped_tasks(
    settings: Any,
    *,
    repo_id: str | None = None,
    worktree_id: str | None = None,
    state: str | None = None,
) -> list[dict[str, Any]]:
    require_enabled(settings)
    clean_state: str | None = None
    if state:
        clean_state = state.strip()
        if clean_state not in ags.STATES:
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                f"不知道的筛选状态：{clean_state}",
                code="invalid_state",
            )

    repo_filter: dict[str, Any] | None = None
    wt_filter: dict[str, Any] | None = None
    if repo_id:
        repo_filter = _get_repo_row(settings, repo_id)
    if worktree_id:
        wid = _validate_id_token(worktree_id, "worktree_id")
        conn = _connect(settings)
        try:
            row = conn.execute(
                "SELECT * FROM supervisor_worktrees WHERE id = ?",
                (wid,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise SupervisorServiceError(
                HTTPStatus.NOT_FOUND,
                f"找不到这个工作目录（{wid}）",
                code="worktree_not_found",
            )
        wt_filter = dict(row)
        if repo_filter is not None and str(wt_filter["repo_id"]) != str(repo_filter["id"]):
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                "这个工作目录不属于选择的仓库",
                code="scope_mismatch",
            )

    raw_tasks = ags.list_tasks(settings, state=clean_state, limit=200)
    path_maps = _lookup_scope_ids_by_paths(settings)
    filtered: list[dict[str, Any]] = []
    for t in raw_tasks:
        enriched = _enrich_task_dict(settings, t, path_maps=path_maps)
        meta = enriched.get("metadata") or {}
        if repo_filter is not None:
            t_repo_id = meta.get("repo_id")
            t_repo_path = _canonical_internal_path(str(enriched.get("repo_path") or ""))
            if t_repo_id:
                if t_repo_id != str(repo_filter["id"]):
                    continue
            elif t_repo_path != str(repo_filter["canonical_path"]):
                continue
        if wt_filter is not None:
            t_wt_id = meta.get("worktree_id")
            t_wt_path = _canonical_internal_path(str(enriched.get("worktree_path") or ""))
            if t_wt_id:
                if t_wt_id != str(wt_filter["id"]):
                    continue
            elif t_wt_path != str(wt_filter["canonical_path"]):
                continue
        filtered.append(enriched)
    return filtered


def _safe_read_execution_output(settings: Any, result_path: str) -> str | None:
    if not result_path:
        return None
    mem_root = getattr(settings, "codex_memory_root", "") or ""
    if not mem_root:
        return None
    state_root = Path(_canonical_internal_path(str(mem_root))) / "state"
    allowed_roots = (
        state_root / "agent_supervisor_evidence",
        state_root / "supervisor_evidence",
    )
    try:
        candidate = Path(_canonical_internal_path(result_path))
        if not any(root in candidate.parents for root in allowed_roots) or not candidate.is_file():
            return None
        raw = candidate.read_text(encoding="utf-8", errors="replace")
        return truncate(redact_text(raw), MAX_EXEC_OUTPUT_PREVIEW_CHARS)
    except OSError:
        return None


def _collect_task_executions(
    settings: Any,
    task: dict[str, Any],
    handoffs: list[dict[str, Any]],
    acceptance: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    tid = str(task["id"])
    direct = ags.list_executions(settings, task_id=tid, limit=100)
    by_id: dict[str, dict[str, Any]] = {str(e["execution_id"]): dict(e) for e in direct}

    # Backwards-compatible only when executions explicitly match this task's
    # handoff or acceptance evidence paths. Unbound rows (task_id="") without
    # matching evidence are strictly excluded; never guess or reassign by timestamp.
    evidence_paths: set[str] = set()
    for h in handoffs:
        ep = str(h.get("evidence_path") or "").strip()
        if ep and ep != "coordinator":
            evidence_paths.add(_canonical_internal_path(ep))
    latest_acc = ags.latest_acceptance(settings, tid)
    for acc_item in (acceptance, latest_acc):
        if isinstance(acc_item, dict):
            ep = str(acc_item.get("evidence_path") or "").strip()
            if ep:
                evidence_paths.add(_canonical_internal_path(ep))

    if evidence_paths:
        for ex in ags.list_executions(settings, limit=200):
            eid = str(ex["execution_id"])
            if eid in by_id:
                continue
            ex_tid = str(ex.get("task_id") or "").strip()
            if ex_tid and ex_tid != tid:
                continue
            ex_ep = _canonical_internal_path(str(ex.get("evidence_path") or ""))
            if ex_ep and ex_ep in evidence_paths:
                by_id[eid] = dict(ex)

    items = sorted(by_id.values(), key=lambda x: str(x.get("started_at") or ""))
    formatted: list[dict[str, Any]] = []
    for ex in items:
        item = dict(ex)
        item["id"] = str(ex["execution_id"])
        if item.get("error"):
            item["error"] = redact_text(str(item["error"]))
        output_preview = _safe_read_execution_output(settings, str(ex.get("result_path") or ""))
        if output_preview is not None:
            item["output"] = output_preview
        formatted.append(item)
    return formatted


def get_scoped_task(settings: Any, task_id: str) -> dict[str, Any]:
    require_enabled(settings)
    tid = _validate_id_token(task_id, "task_id")
    task = ags.get_task(settings, tid)
    if task is None:
        raise SupervisorServiceError(
            HTTPStatus.NOT_FOUND,
            f"task not found: {tid}",
            code="task_not_found",
        )
    enriched = _enrich_task_dict(settings, task, include_changed_files=True)
    handoffs = ags.list_handoffs(settings, tid)
    for h in handoffs:
        if h.get("summary"):
            h["summary"] = redact_text(str(h["summary"]))
    acceptance = ags.effective_acceptance(settings, tid)
    if isinstance(acceptance, dict) and acceptance.get("evidence"):
        acceptance = dict(acceptance)
        acceptance["evidence"] = redact_text(str(acceptance["evidence"]))
    executions = _collect_task_executions(settings, enriched, handoffs, acceptance)
    enriched["handoffs"] = handoffs
    enriched["acceptance"] = acceptance
    enriched["executions"] = executions
    enriched["summary"] = redact_text(ags.summary(settings, tid))
    return enriched


def get_task_diff(settings: Any, task_id: str) -> dict[str, Any]:
    """Read-only diff of one task: `base_sha` -> current managed-worktree content.

    Covers committed + staged + unstaged changes plus untracked text files.
    Imported lazily because `supervisor_diff` builds on this module's canonical
    path / error helpers.
    """
    require_enabled(settings)
    from conveyor.agents import supervisor_diff as diff_mod

    tid = _validate_id_token(task_id, "task_id")
    task = ags.get_task(settings, tid)
    if task is None:
        raise SupervisorServiceError(
            HTTPStatus.NOT_FOUND,
            f"task not found: {tid}",
            code="task_not_found",
        )
    meta = task.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
    payload = diff_mod.build_worktree_diff(
        settings,
        worktree_path=task.get("worktree_path") or "",
        base_sha=str(task.get("base_sha") or ""),
        fallback_base_sha=str(meta.get("queued_target_sha") or ""),
    )
    return {"task_id": tid, **payload}


def cancel_scoped_task(
    server: Any,
    settings: Any,
    task_id: str,
    *,
    reason: Any = None,
) -> dict[str, Any]:
    require_enabled(settings)
    tid = _validate_id_token(task_id, "task_id")
    task = ags.get_task(settings, tid)
    if task is None:
        raise SupervisorServiceError(
            HTTPStatus.NOT_FOUND,
            f"task not found: {tid}",
            code="task_not_found",
        )
    clean_reason = "手动取消"
    if reason is not None:
        if not isinstance(reason, str):
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                "原因需要是文字",
                code="invalid_reason",
            )
        stripped = reason.strip()[:MAX_REASON_CHARS]
        if stripped:
            clean_reason = redact_text(stripped)

    mgr = _worker_manager_for_server(server)
    if mgr is not None:
        mgr.cancel_flow(settings, tid, reason=clean_reason)
    else:
        try:
            ags.cancel_task(settings, tid, reason=clean_reason)
        except ags.SupervisorStateError as exc:
            raise SupervisorServiceError(HTTPStatus.BAD_REQUEST, str(exc)) from exc

    return get_scoped_task(settings, tid)


# ---------------------------------------------------------------------------
# Deployments (honest capability & backwards-compatible read-only listing)
# ---------------------------------------------------------------------------

def list_scoped_deployments(settings: Any, *, task_id: str | None = None) -> list[dict[str, Any]]:
    require_enabled(settings)
    tid: str | None = None
    if task_id:
        tid = _validate_id_token(task_id, "task_id")
    raw_items = ags.list_deployments(settings, task_id=tid, limit=50)
    sanitized: list[dict[str, Any]] = []
    for dep in raw_items:
        d = dict(dep)
        raw_stages = d.get("stages")
        if isinstance(raw_stages, list):
            clean_stages = []
            for st in raw_stages:
                if not isinstance(st, dict):
                    continue
                st_copy = dict(st)
                if st_copy.get("detail"):
                    st_copy["detail"] = redact_text(str(st_copy["detail"]))
                clean_stages.append(st_copy)
            d["stages"] = clean_stages
        sanitized.append(d)
    return sanitized


def start_scoped_deployment(settings: Any, body: dict[str, Any]) -> dict[str, Any]:
    require_enabled(settings)
    _ = body
    raise SupervisorServiceError(
        HTTPStatus.CONFLICT,
        "这台机器上还没有配置远程执行节点，暂时不能启动。",
        code="deployment_unconfigured",
    )


# ---------------------------------------------------------------------------
# Owned Asyncio Worker Manager on WebConsoleServer's loop
# ---------------------------------------------------------------------------

class SupervisorWorkerManager:
    """Owns at most one durable queue consumer on a `WebConsoleServer`'s event loop.

    - Runs `ags.recover_on_startup(settings)` once when starting on the server's
      loop so stale orphaned processes/tasks from prior runs are reconciled
      without falsely interrupting in-flight flows on subsequent poll ticks.
    - Claims queued tasks via `ags.claim_task(settings)` and runs
      `ags.run_supervisor_flow(settings, task_id)` up to `ags.MAX_CONCURRENT_FLOWS`.
    - Advances the goal-driven loop (`supervisor_goals.advance_goals`) once per tick,
      after claiming available tasks, as a single-flight background task so a slow
      evaluator chat call never blocks task claiming.
    - Supports immediate wakeup on `POST /api/supervisor/tasks/:id/dispatch` and
      clean cancellation/shutdown when `WebConsoleServer.server_close()` runs.
    - Registers a goal-loop wake hook on start, so a goal answered from a phone
      (`/goal_reply` in the bot process, which owns no server) wakes this loop too.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._lock = threading.Lock()
        self._settings: Any = None
        self._server: Any = None
        self._worker_task: asyncio.Task[None] | None = None
        self._wake_event: asyncio.Event | None = None
        self._active_flows: dict[str, asyncio.Task[Any]] = {}
        self._goals_task: asyncio.Task[None] | None = None
        self._recovered_once = False
        self._closed = False
        self._wake_unregister: Callable[[], None] | None = None

    def wake(self, settings: Any = None) -> None:
        """Wake the worker loop from ANY thread (e.g. a Telegram goal reply)."""
        self.ensure_started(self._settings if settings is None else settings, wake=True)

    def _register_goal_wake_hook(self) -> None:
        """Let `supervisor_goals.reply_goal` / `stop_goal` wake this loop without a
        server handle: a `/goal_reply` answered on a phone runs in the bot process,
        which never sees the `WebConsoleServer` that owns this manager."""
        with self._lock:
            if self._closed or self._wake_unregister is not None:
                return
        from conveyor.agents.supervisor_goals import set_goal_wake_hook

        unregister = set_goal_wake_hook(self.wake)
        with self._lock:
            if self._closed:
                if unregister is not None:
                    unregister()
                return
            self._wake_unregister = unregister

    def _unregister_goal_wake_hook(self) -> None:
        with self._lock:
            unregister = self._wake_unregister
            self._wake_unregister = None
        if unregister is not None:
            unregister()

    def ensure_started(self, settings: Any, *, wake: bool = False, server: Any = None) -> None:
        if not is_enabled(settings):
            return
        self._register_goal_wake_hook()
        with self._lock:
            if self._closed:
                return
            self._settings = settings
            if server is not None:
                self._server = server
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is self.loop:
            self._start_on_loop(wake=wake)
        elif self.loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._async_ensure(wake=wake), self.loop)
            try:
                fut.result(timeout=5.0)
            except Exception:
                log.warning("supervisor worker wakeup timed out", exc_info=True)

    async def _async_ensure(self, *, wake: bool) -> None:
        self._start_on_loop(wake=wake)

    def _start_on_loop(self, *, wake: bool) -> None:
        with self._lock:
            if self._closed or self._settings is None or not is_enabled(self._settings):
                return
            if self._wake_event is None:
                self._wake_event = asyncio.Event()
            if self._worker_task is None or self._worker_task.done():
                self._worker_task = self.loop.create_task(
                    self._run_loop(),
                    name="conveyor-web-supervisor-worker",
                )
            if wake and self._wake_event is not None:
                self._wake_event.set()

    async def _run_loop(self) -> None:
        if not self._recovered_once:
            self._recovered_once = True
            try:
                if self._settings is not None and is_enabled(self._settings):
                    ags.recover_on_startup(self._settings)
            except Exception:
                log.exception("supervisor startup recovery failed")

        while not self._closed:
            settings = self._settings
            if settings is not None and is_enabled(settings):
                try:
                    await self._claim_available(settings)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("supervisor worker claim cycle failed")

                # Goal-driven loop: one single-flight background pass per tick. Never
                # awaited inline — an evaluator chat call can take minutes, and blocking
                # here would stall task claiming for the whole server.
                self._start_goals_pass(settings)

            if self._wake_event is None:
                self._wake_event = asyncio.Event()
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=0.5)
                self._wake_event.clear()
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

    def _start_goals_pass(self, settings: Any) -> None:
        """Start `advance_goals` in the background unless the previous pass is still
        running (single-flight). Called from the worker loop only."""
        if self._closed:
            return
        with self._lock:
            goals_task = self._goals_task
            if goals_task is not None and not goals_task.done():
                return
        try:
            from conveyor.agents.supervisor_goals import advance_goals

            task = self.loop.create_task(
                advance_goals(self._server, settings),
                name="conveyor-supervisor-goals",
            )
        except Exception:
            log.exception("supervisor goal pass could not be started")
            return
        with self._lock:
            self._goals_task = task
        task.add_done_callback(self._on_goals_pass_done)

    def _on_goals_pass_done(self, task: "asyncio.Future[Any]") -> None:
        with self._lock:
            if self._goals_task is task:
                self._goals_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("supervisor goal advance cycle failed: %s", redact_text(f"{type(exc).__name__}: {exc}"))

    def _on_flow_done(self, settings: Any, task_id: str, flow_task: asyncio.Future[Any]) -> None:
        # Remove entry ONLY if map still points to this done future
        # (avoid old callback popping new retry flow with same id).
        if self._active_flows.get(task_id) is flow_task:
            self._active_flows.pop(task_id, None)

        is_cancelled = False
        flow_exc: BaseException | None = None
        if not flow_task.done():
            pass
        elif flow_task.cancelled():
            is_cancelled = True
        else:
            try:
                flow_exc = flow_task.exception()
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                is_cancelled = flow_task.cancelled()
            except Exception as e:
                flow_exc = e

        if flow_task.done() and not is_cancelled and flow_exc is None:
            try:
                flow_task.result()
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                is_cancelled = flow_task.cancelled()
            except Exception as e:
                flow_exc = e

        terminal_states = getattr(ags, "TERMINAL_STATES", ("completed", "failed", "cancelled", "interrupted"))

        if flow_exc is not None:
            clean_err = redact_text(f"{type(flow_exc).__name__}: {flow_exc}").strip() or "supervisor flow error"
            log.error("supervisor flow %s raised unexpected exception: %s", task_id, clean_err)
            try:
                task = ags.get_task(settings, task_id)
            except Exception as db_err:
                log.error(
                    "Failed to query task %s after flow exception; DB error: %s",
                    task_id,
                    redact_text(str(db_err)),
                )
                return

            if task is not None:
                cur_state = str(task.get("state") or "")
                # Terminalizes unexpected errors if task still nonterminal and DB available;
                # preserves cancelled/completed states.
                if cur_state not in terminal_states:
                    try:
                        ags.transition(
                            settings,
                            task_id,
                            "failed",
                            stage="unexpected_error",
                            error=clean_err,
                        )
                    except Exception as trans_err:
                        log.error(
                            "Failed to transition task %s to failed: %s",
                            task_id,
                            redact_text(str(trans_err)),
                        )
                        return

        # Releases worktree lock ONLY when ags.lock_release_safe proves writer dead.
        # Never clear uncertain startup/process fences.
        # If DB error, log honest failure/preserve fence rather than phantom completed.
        try:
            cur_task = ags.get_task(settings, task_id)
            if cur_task is not None and str(cur_task.get("state") or "") in terminal_states:
                wp = _canonical_internal_path(str(cur_task.get("worktree_path") or ""))
                if wp and ags.lock_release_safe(settings, wp, task_id):
                    ags.release_worktree_lock(settings, wp, task_id)
        except Exception as lock_err:
            log.error(
                "Failed to check/release worktree lock for task %s: %s",
                task_id,
                redact_text(str(lock_err)),
            )

    async def _claim_available(self, settings: Any) -> None:
        for tid, flow in list(self._active_flows.items()):
            if flow.done():
                self._on_flow_done(settings, tid, flow)

        max_flows = max(1, int(getattr(ags, "MAX_CONCURRENT_FLOWS", 2)))
        while len(self._active_flows) < max_flows:
            claimed = ags.claim_task(settings)
            if claimed is None:
                break
            tid = str(claimed["id"])
            flow_task = self.loop.create_task(
                ags.run_supervisor_flow(settings, tid),
                name=f"conveyor-supervisor-flow-{tid[:16]}",
            )
            self._active_flows[tid] = flow_task
            flow_task.add_done_callback(
                lambda _f, task_key=tid, s=settings: self._on_flow_done(s, task_key, _f)
            )

    def cancel_flow(self, settings: Any, task_id: str, *, reason: str) -> None:
        # Cancel ONLY verified exact task-owned executions via ags.cancel_task/cancel_execution.
        # Legacy unknown rows (task_id='') must NOT be killed or reassigned by timestamp.
        for ex in ags.list_executions(settings, task_id=task_id, limit=50):
            if ex.get("status") in ("spawning", "running") and str(ex.get("task_id") or "").strip() == task_id:
                try:
                    ags.cancel_execution(settings, str(ex["execution_id"]), reason=reason)
                except Exception:
                    pass

        ags.cancel_task(settings, task_id, reason=reason)

        flow_task = self._active_flows.get(task_id)
        if flow_task is not None and not flow_task.done() and self.loop.is_running():
            async def _cancel_and_wait() -> None:
                if not flow_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(flow_task), timeout=0.8)
                        return
                    except asyncio.TimeoutError:
                        flow_task.cancel()
                    except Exception:
                        return
                try:
                    await asyncio.wait_for(flow_task, timeout=3.0)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass

            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop is not self.loop:
                fut = asyncio.run_coroutine_threadsafe(_cancel_and_wait(), self.loop)
                try:
                    fut.result(timeout=4.0)
                except Exception:
                    pass
            else:
                if not flow_task.done():
                    flow_task.cancel()

        task_after = ags.get_task(settings, task_id) or {}
        wp = _canonical_internal_path(str(task_after.get("worktree_path") or ""))
        if wp and ags.lock_release_safe(settings, wp, task_id):
            ags.release_worktree_lock(settings, wp, task_id)

    def stop(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            worker = self._worker_task
            flows = list(self._active_flows.values())
        self._unregister_goal_wake_hook()

        async def _shutdown() -> None:
            if worker is not None and not worker.done():
                worker.cancel()
            for f in flows:
                if not f.done():
                    f.cancel()
            goals_task = self._goals_task
            if goals_task is not None and not goals_task.done():
                goals_task.cancel()
            pending = [
                t
                for t in ([worker] if worker else []) + flows + ([goals_task] if goals_task else [])
                if t is not None and not t.done()
            ]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        if self.loop.is_closed():
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is self.loop:
            self.loop.create_task(_shutdown())
        elif self.loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(_shutdown(), self.loop)
            try:
                fut.result(timeout=3.0)
            except Exception:
                pass


SupervisorWorker = SupervisorWorkerManager


_SERVER_MANAGER_LOCK = threading.Lock()


def _worker_manager_for_server(server: Any) -> SupervisorWorkerManager | None:
    if server is None or getattr(server, "loop", None) is None:
        return None
    with _SERVER_MANAGER_LOCK:
        mgr = getattr(server, "_supervisor_worker_manager", None)
        if mgr is None:
            mgr = SupervisorWorkerManager(server.loop)
            setattr(server, "_supervisor_worker_manager", mgr)
        return mgr


def ensure_server_worker(server: Any, settings: Any, *, wake: bool = False) -> SupervisorWorkerManager | None:
    if not is_enabled(settings):
        return None
    mgr = _worker_manager_for_server(server)
    if mgr is not None:
        mgr.ensure_started(settings, wake=wake, server=server)
    return mgr


def stop_server_worker(server: Any) -> None:
    if server is None:
        return
    with _SERVER_MANAGER_LOCK:
        mgr = getattr(server, "_supervisor_worker_manager", None)
    if mgr is not None:
        mgr.stop()
