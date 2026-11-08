"""supervisor_merge.py — owner-confirmed 「合并到主分支」 for Supervisor worktrees.

README step 5 (目标驱动的闭环): when a goal or task finishes, its changes sit in
a *managed Supervisor worktree* (branch e.g. `goal-calc`) under the runtime's
managed worktree root. The owner has no way to take them — this module is that
explicit, owner-triggered merge back into the repository's default branch in the
main checkout.

    GET  /api/supervisor/worktrees/<worktree_id>/merge-preview   (read-only)
    POST /api/supervisor/worktrees/<worktree_id>/merge           (owner-confirmed)

Design contract (see docs/agent-supervisor.md 「合并到主分支」):
- Never trust a caller-supplied path: the worktree id is resolved through
  `supervisor_service`'s own rows + live `git worktree list` enumeration, and the
  merge target is always the *main checkout* of the registered repository.
- Every git call runs with a timeout (60 s), `--no-pager`, forced
  `core.hooksPath=/dev/null`, `core.fsmonitor=false`, `commit.gpgsign=false`,
  `core.editor=true`, a scrubbed child env (copied from `supervisor_diff._git_env`)
  and never `shell=True`. Nothing is ever pushed.
- Every rule is a *blocker* with a Chinese message: preview reports them all,
  POST re-checks them and answers `409` with the first one.
- Merges are serialized per repository (module-level lock registry) and hold the
  worktree's runtime lock for the whole operation, so no task can start mid-merge.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import subprocess
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import quote

import agent_supervisor as ags
import supervisor_diff as diff_mod
import supervisor_service as svc
from redaction import redact_text

log = logging.getLogger("conveyor.supervisor_merge")

# --------------------------------------------------------------------------
# limits & fixed strings
# --------------------------------------------------------------------------
GIT_TIMEOUT_SECONDS = 60.0
MAX_MESSAGE_CHARS = 200
MERGE_AUTHOR_NAME = "Conveyor"
MERGE_AUTHOR_EMAIL = "conveyor@localhost"
# Synthetic lock holder id: the merge is not a task, but it must keep the
# worktree's runtime lock so `dispatch` cannot start a task mid-merge.
MERGE_LOCK_TASK_PREFIX = "merge-"
MERGE_LOCK_TTL_SECONDS = 3600
DEFAULT_COMMIT_MESSAGE = "Conveyor：任务改动"
GOAL_COMMIT_MESSAGE_TEMPLATE = "Conveyor：{title}"
MERGE_MESSAGE_TEMPLATE = "合并 {branch}（Conveyor）"

# --------------------------------------------------------------------------
# error / blocker codes
# --------------------------------------------------------------------------
CODE_INVALID_BODY = "invalid_body"
CODE_CONFIRM_REQUIRED = "confirm_required"
CODE_INVALID_MESSAGE = "invalid_message"
CODE_WORKTREE_NOT_FOUND = "worktree_not_found"
CODE_WORKTREE_MISSING = "worktree_missing"
CODE_UNMANAGED_WORKTREE = "unmanaged_worktree"
CODE_WORKTREE_BUSY = "worktree_busy"
CODE_TARGET_BRANCH_UNKNOWN = "target_branch_unknown"
CODE_DETACHED_SOURCE = "detached_source"
CODE_TARGET_NOT_CHECKED_OUT = "target_not_checked_out"
CODE_TARGET_DIRTY = "target_dirty"
CODE_UNSAFE_REPO_CONFIG = "unsafe_repo_config"
CODE_UNSAFE_GITATTRIBUTES = "unsafe_gitattributes"
CODE_NOTHING_TO_MERGE = "nothing_to_merge"
CODE_MERGE_CONFLICT = "merge_conflict"
CODE_COMMIT_FAILED = "commit_failed"
CODE_MERGE_ABORT_FAILED = "merge_abort_failed"

# --------------------------------------------------------------------------
# per-repository merge serialization
# --------------------------------------------------------------------------
_REPO_LOCKS_GUARD = threading.Lock()
_REPO_LOCKS: dict[str, threading.Lock] = {}


def _repo_merge_lock(repo_id: str) -> threading.Lock:
    """One lock per repository id, created on demand (module-level registry)."""
    with _REPO_LOCKS_GUARD:
        lock = _REPO_LOCKS.get(repo_id)
        if lock is None:
            lock = threading.Lock()
            _REPO_LOCKS[repo_id] = lock
        return lock


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

class SupervisorMergeError(svc.SupervisorServiceError):
    """`SupervisorServiceError` that can carry extra payload keys.

    Used for `merge_conflict`, where the owner also needs the conflicting paths
    without the message turning into an unreadable wall of text.
    """

    def __init__(
        self,
        status: int | HTTPStatus,
        message: str,
        *,
        code: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(status, message, code=code)
        self.extra = dict(extra or {})

    def to_payload(self) -> dict[str, Any]:
        payload = super().to_payload()
        for key, value in self.extra.items():
            payload.setdefault(key, value)
        return payload


def _error(
    status: int | HTTPStatus,
    message: str,
    *,
    code: str | None = None,
    extra: dict[str, Any] | None = None,
) -> SupervisorMergeError:
    return SupervisorMergeError(status, message, code=code, extra=extra)


def _blocker(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": redact_text(message)}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# git plumbing — every invocation is timeout-bound, hook-free and env-scrubbed
# ---------------------------------------------------------------------------

def _git_env(settings: Any) -> dict[str, str]:
    """Child env copied from `supervisor_diff._git_env`, plus `GIT_EDITOR=true`.

    Strips `GIT_DIR` / `GIT_WORK_TREE` / `GIT_INDEX_FILE` / … so no inherited
    variable can redirect git at another repository, and pins
    `GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0` and `GIT_EDITOR=true` (no
    editor may block a merge).
    """
    env = diff_mod._git_env(settings)
    env["GIT_EDITOR"] = "true"
    return env


def _git_argv(path: str, args: list[str], *, config: tuple[str, ...] = ()) -> list[str]:
    """`git --no-pager -C <path>` with the command-running surfaces forced off.

    `core.hooksPath=/dev/null` and `commit.gpgsign=false` matter because this is
    the one module that *writes*: an agent that can edit the repository config
    must not be able to run code when the owner merges, and a `gpg.program` /
    `core.sshCommand` in that config must never fire either.

    `config` adds extra `-c key=value` overrides. They must lead the subcommand:
    `git commit -c user.name=…` would be read as `commit --reedit-message=…`.
    """
    forced = (
        "core.hooksPath=/dev/null",
        "core.fsmonitor=false",
        "commit.gpgsign=false",
        "tag.gpgsign=false",
        "core.editor=true",
        "core.quotePath=false",
    )
    argv = ["git", "--no-pager", "-C", path]
    for item in (*forced, *config):
        argv.extend(["-c", item])
    argv.extend(args)
    return argv


def _git(
    settings: Any,
    path: str,
    args: list[str],
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
    config: tuple[str, ...] = (),
) -> tuple[int, str, str]:
    """Run one git command inside an already-validated worktree / main checkout."""
    try:
        proc = subprocess.run(
            _git_argv(path, args, config=config),
            capture_output=True,
            env=_git_env(settings),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise _error(
            HTTPStatus.GATEWAY_TIMEOUT,
            "git 操作超时",
            code="git_timeout",
        ) from exc
    except OSError as exc:
        raise _error(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            f"git 执行失败: {exc}",
            code="git_unavailable",
        ) from exc

    def _decode(raw: bytes | None) -> str:
        return (raw or b"").decode("utf-8", "replace")

    return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)


def _git_ok(settings: Any, path: str, args: list[str]) -> str:
    rc, out, err = _git(settings, path, args)
    if rc != 0:
        raise _error(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            f"git {' '.join(args[:2])} 失败: {redact_text((err or out or '').strip())[:200]}",
            code="git_command_failed",
        )
    return out


def _head_sha(settings: Any, path: str) -> str:
    rc, out, _ = _git(settings, path, ["rev-parse", "--verify", "--quiet", "HEAD"])
    return out.strip() if rc == 0 else ""


def _current_branch(settings: Any, path: str) -> str:
    rc, out, _ = _git(settings, path, ["symbolic-ref", "--short", "-q", "HEAD"])
    return out.strip() if rc == 0 else ""


def _branch_exists(settings: Any, path: str, branch: str) -> bool:
    rc, _out, _err = _git(
        settings, path, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
    )
    return rc == 0


def _is_clean(settings: Any, path: str) -> bool:
    """No *tracked* modification in the main checkout (untracked files ignored)."""
    rc, out, _ = _git(settings, path, ["status", "--porcelain", "--untracked-files=no"])
    return rc == 0 and not out.strip()


def _has_uncommitted(settings: Any, path: str) -> bool:
    """Anything the worktree still holds outside a commit (untracked included)."""
    rc, out, _ = _git(settings, path, ["status", "--porcelain", "--untracked-files=normal"])
    return rc == 0 and bool(out.strip())


# ---------------------------------------------------------------------------
# scope resolution: worktree id -> (row, path, repo row, live worktree entry)
# ---------------------------------------------------------------------------

def _resolve_scope(settings: Any, worktree_id: Any) -> dict[str, Any]:
    """Resolve a worktree id through the service layer's rows + live git state.

    Never trusts a caller-supplied path: the id must exist in
    `supervisor_worktrees`, its repository must still be registered, and the
    worktree must still be enumerated by `git worktree list` for that repository.
    """
    svc.require_enabled(settings)
    wid = svc._validate_id_token(worktree_id, "worktree_id")
    conn = svc._connect(settings)
    try:
        row = conn.execute(
            "SELECT * FROM supervisor_worktrees WHERE id = ?",
            (wid,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise _error(
            HTTPStatus.NOT_FOUND,
            f"worktree 不存在: {wid}",
            code=CODE_WORKTREE_NOT_FOUND,
        )
    repo_row = svc._get_repo_row(settings, str(row["repo_id"]))
    synced = svc._sync_repo_worktrees(settings, repo_row)
    wt = next((item for item in synced if str(item["id"]) == wid), None)
    if wt is None:
        raise _error(
            HTTPStatus.NOT_FOUND,
            "worktree 已经不在仓库的 git 工作树列表里，无法合并",
            code=CODE_WORKTREE_MISSING,
        )
    return {
        "worktree_id": wid,
        "worktree": wt,
        "repo_row": repo_row,
        "repo_id": str(repo_row["id"]),
        "worktree_path": str(wt["path"]),
        "main_path": str(repo_row["canonical_path"]),
    }


# ---------------------------------------------------------------------------
# target branch (the repository's default branch, resolved stably)
# ---------------------------------------------------------------------------

def _remote_default_branch(settings: Any, main_path: str) -> str:
    rc, out, _ = _git(
        settings, main_path, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"]
    )
    if rc != 0 or not out.strip():
        return ""
    branch = out.strip().split("/", 1)[1] if out.strip().startswith("origin/") else out.strip()
    return branch if _branch_exists(settings, main_path, branch) else ""


def _target_branch(settings: Any, scope: dict[str, Any]) -> str:
    """The repository's default branch, resolved without trusting the checkout.

    `origin/HEAD` first, then the conventional `main` / `master` (only when they
    really exist), and only as a last resort whatever the main checkout happens
    to have checked out right now. Deliberately *not* the current branch first:
    the whole point of rule 3 is to refuse when the main checkout drifted away
    from the default branch.
    """
    main_path = str(scope["main_path"])
    remote = _remote_default_branch(settings, main_path)
    if remote:
        return remote
    for candidate in ("main", "master"):
        if _branch_exists(settings, main_path, candidate):
            return candidate
    return _current_branch(settings, main_path)


# ---------------------------------------------------------------------------
# rule 5: repository configuration that can run commands
# ---------------------------------------------------------------------------
# `git config` keys whose value is handed to a shell / external program.
_UNSAFE_CONFIG_EXACT = frozenset({
    "core.fsmonitor",
    "core.hookspath",
    "core.sshcommand",
    "core.pager",
    "core.editor",
    "sequence.editor",
    "diff.external",
    "credential.helper",
    "gpg.program",
})
# Values that disable a feature instead of naming a program to run.
_DISABLING_VALUES = frozenset({"", "false", "0", "no", "off", "none"})
_ATTRIBUTE_DRIVER_RE = re.compile(r"(?:^|\s)(filter|merge|diff)\s*=\s*([^\s#]+)")
_MAX_CONFIG_SCAN_BYTES = 256 * 1024


def _value_runs_commands(value: str) -> bool:
    return value.strip().lower() not in _DISABLING_VALUES


def _config_key_runs_commands(key: str) -> bool:
    """True for `core.fsmonitor`, `core.hooksPath`, `filter.<x>.clean|smudge|process`,
    `merge.<x>.driver`, `diff.<x>.textconv|command`, `gpg.<x>.program`, …"""
    cleaned = str(key or "").strip().lower()
    if not cleaned:
        return False
    if cleaned in _UNSAFE_CONFIG_EXACT:
        return True
    parts = cleaned.split(".")
    if len(parts) != 3:
        return False
    section, _name, leaf = parts
    if section == "filter" and leaf in ("clean", "smudge", "process"):
        return True
    if section == "merge" and leaf == "driver":
        return True
    if section == "diff" and leaf in ("textconv", "command"):
        return True
    if section == "gpg" and leaf == "program":
        return True
    return False


def _parse_config_list(raw: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key:
            entries.append((key, value))
    return entries


def _unsafe_config_keys(settings: Any, paths: list[str]) -> list[str]:
    """Keys in `--local` (+ `--worktree` when available) that can execute commands.

    `paths` is scanned in order and deduplicated: a linked worktree shares the
    repository's common config, so the same key must not be reported twice.
    """
    keys: list[str] = []
    seen: set[str] = set()
    for path in paths:
        for args in (["config", "--local", "--list"], ["config", "--worktree", "--list"]):
            rc, out, _err = _git(settings, path, args)
            if rc != 0:
                # `--worktree` needs extensions.worktreeConfig; older git rejects it.
                continue
            for key, value in _parse_config_list(out):
                if not _config_key_runs_commands(key) or not _value_runs_commands(value):
                    continue
                if key in seen:
                    continue
                seen.add(key)
                keys.append(key)
    return keys


def _gitattributes_has_driver(path: str) -> bool:
    """`.gitattributes` at the root of `path` naming a filter/merge/diff driver."""
    full = os.path.join(path, ".gitattributes")
    try:
        with open(full, "rb") as handle:
            raw = handle.read(_MAX_CONFIG_SCAN_BYTES)
    except OSError:
        return False
    text = raw.decode("utf-8", "replace")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for match in _ATTRIBUTE_DRIVER_RE.finditer(stripped):
            value = match.group(2).strip()
            # `-diff` / `!merge` unset the attribute; they name no driver.
            if value and not value.startswith(("-", "!")):
                return True
    return False


# ---------------------------------------------------------------------------
# rule 2: no active task / running goal on the worktree
# ---------------------------------------------------------------------------

def _goal_running_state() -> str:
    try:
        import supervisor_goals as goals_mod

        return str(goals_mod.GOAL_RUNNING)
    except Exception:
        return "running"


def _goal_achieved_state() -> str:
    try:
        import supervisor_goals as goals_mod

        return str(goals_mod.GOAL_ACHIEVED)
    except Exception:
        return "achieved"


def _goals_db_rows(settings: Any, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
    """Read-only rows from the goals DB; empty when it does not exist yet."""
    try:
        import supervisor_goals as goals_mod

        db_file = goals_mod._db_path(settings)
    except Exception:
        return []
    if not Path(db_file).exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{quote(str(db_file))}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _running_goal_ids(settings: Any, worktree_id: str) -> list[str]:
    """Goal ids still `running` on this worktree (the goals DB may not exist yet)."""
    rows = _goals_db_rows(
        settings,
        "SELECT id, state FROM supervisor_goals WHERE worktree_id = ?",
        (worktree_id,),
    )
    return [str(row["id"]) for row in rows if str(row["state"] or "") == _goal_running_state()]


def _busy_message(settings: Any, scope: dict[str, Any]) -> str:
    worktree_path = str(scope["worktree_path"])
    if ags.has_active_task_for_worktree(settings, worktree_path):
        return "这个 worktree 上还有正在跑的任务，等它结束后再合并"
    running = _running_goal_ids(settings, str(scope["worktree_id"]))
    if running:
        joined = "、".join(running[:3])
        return f"这个 worktree 上还有正在跑的目标（{joined}），等它结束后再合并"
    return ""


# ---------------------------------------------------------------------------
# blockers (every hard rule, each with a Chinese message)
# ---------------------------------------------------------------------------

def _is_safe_branch_name(branch: str) -> bool:
    """A branch name git cannot mistake for an option or a revision range."""
    cleaned = str(branch or "").strip()
    if not cleaned or cleaned.startswith("-"):
        return False
    return not any(tok in cleaned for tok in ("..", "\n", "\x00", " ", "~", "^", ":", "?", "*", "["))


def _collect_blockers(
    settings: Any,
    scope: dict[str, Any],
    *,
    target_branch: str,
    source_branch: str,
) -> list[dict[str, str]]:
    """Evaluate rules 1-5. Preview reports all of them; POST answers the first."""
    blockers: list[dict[str, str]] = []
    wt = scope["worktree"]
    main_path = str(scope["main_path"])
    worktree_path = str(scope["worktree_path"])

    # 1. Only managed Supervisor worktrees, never the main checkout itself.
    if bool(wt.get("is_main")) or not bool(wt.get("managed")):
        blockers.append(_blocker(
            CODE_UNMANAGED_WORKTREE,
            "只能合并受 Supervisor 托管的 worktree，主工作目录不能作为合并来源",
        ))
        return blockers

    # 2. Nothing may be running on the worktree.
    busy = _busy_message(settings, scope)
    if busy:
        blockers.append(_blocker(CODE_WORKTREE_BUSY, busy))
        return blockers

    if not _is_safe_branch_name(target_branch):
        blockers.append(_blocker(
            CODE_TARGET_BRANCH_UNKNOWN,
            "没能确定这个仓库的默认分支，无法合并",
        ))
        return blockers

    if not _is_safe_branch_name(source_branch):
        blockers.append(_blocker(
            CODE_DETACHED_SOURCE,
            "工作目录现在不在任何分支上（detached HEAD），先切回分支再合并",
        ))
        return blockers

    # 3. The main checkout must have the target branch checked out.
    head_branch = _current_branch(settings, main_path)
    if head_branch != target_branch:
        blockers.append(_blocker(
            CODE_TARGET_NOT_CHECKED_OUT,
            f"主工作目录当前不在 {target_branch} 分支上，请先切回去再合并",
        ))

    # 4. The main checkout must be clean.
    if not _is_clean(settings, main_path):
        blockers.append(_blocker(
            CODE_TARGET_DIRTY,
            "主工作目录里有还没提交的改动，为了不弄乱它，先不合并",
        ))

    # 5. No repository config that can run commands.
    for key in _unsafe_config_keys(settings, [main_path, worktree_path]):
        blockers.append(_blocker(
            CODE_UNSAFE_REPO_CONFIG,
            f"仓库配置里有会执行命令的设置（{key}），为安全起见不自动合并",
        ))

    # 5b. No .gitattributes driver in either checkout.
    for path in (main_path, worktree_path):
        if _gitattributes_has_driver(path):
            blockers.append(_blocker(
                CODE_UNSAFE_GITATTRIBUTES,
                f"仓库里的 .gitattributes 配置了会执行命令的 filter/merge/diff 驱动（{path}），"
                "为安全起见不自动合并",
            ))

    return blockers


# ---------------------------------------------------------------------------
# changed files / commit distance (reuses supervisor_diff for the file list)
# ---------------------------------------------------------------------------

def _merge_base(settings: Any, scope: dict[str, Any], target_branch: str) -> str:
    """Best common ancestor of the target branch and the worktree HEAD."""
    rc, out, _ = _git(
        settings,
        str(scope["worktree_path"]),
        ["merge-base", target_branch, "HEAD"],
    )
    return out.strip() if rc == 0 else ""


def _commits_ahead(settings: Any, scope: dict[str, Any], target_branch: str) -> int:
    rc, out, _ = _git(
        settings,
        str(scope["worktree_path"]),
        ["rev-list", "--count", f"{target_branch}..HEAD"],
    )
    if rc != 0:
        return 0
    try:
        return int(out.strip() or "0")
    except ValueError:
        return 0


def _changed_files(settings: Any, scope: dict[str, Any], target_branch: str) -> list[dict[str, Any]]:
    """Same `files` shape as the task/goal diff endpoints.

    A preview must never explode because of the file list: the main checkout and
    unmanaged worktrees are exactly the cases the owner needs a blocker for, and
    `supervisor_diff` refuses to diff anything outside the managed root.
    """
    if not target_branch:
        return []
    base = _merge_base(settings, scope, target_branch)
    if not base:
        try:
            base = _git_ok(
                settings,
                str(scope["main_path"]),
                ["rev-parse", "--verify", "--quiet", f"refs/heads/{target_branch}"],
            ).strip()
        except svc.SupervisorServiceError:
            return []
    if not base:
        return []
    try:
        payload = diff_mod.build_worktree_diff(
            settings,
            worktree_path=str(scope["worktree_path"]),
            base_sha=base,
        )
    except svc.SupervisorServiceError as exc:
        log.info("merge preview: no file list for worktree %s (%s)", scope["worktree_id"], exc.code)
        return []
    files = payload.get("files")
    return list(files) if isinstance(files, list) else []


# ---------------------------------------------------------------------------
# default commit message
# ---------------------------------------------------------------------------

def _goal_title_for_commit(settings: Any, worktree_id: str) -> str:
    """Title of the goal that owns this worktree (achieved first, else latest)."""
    rows = _goals_db_rows(
        settings,
        "SELECT title, state, created_at FROM supervisor_goals WHERE worktree_id = ?",
        (worktree_id,),
    )
    if not rows:
        return ""
    # Newest first, then (stable) the achieved goal of this worktree wins.
    ranked = sorted(rows, key=lambda r: str(r["created_at"] or ""), reverse=True)
    ranked.sort(key=lambda r: 0 if str(r["state"] or "") == _goal_achieved_state() else 1)
    return str(ranked[0]["title"] or "").strip()


def _default_commit_message(settings: Any, scope: dict[str, Any]) -> str:
    title = _goal_title_for_commit(settings, str(scope["worktree_id"]))
    if title:
        return GOAL_COMMIT_MESSAGE_TEMPLATE.format(title=title[:MAX_MESSAGE_CHARS])
    return DEFAULT_COMMIT_MESSAGE


# ---------------------------------------------------------------------------
# preview payload
# ---------------------------------------------------------------------------

def build_merge_preview(settings: Any, worktree_id: Any) -> dict[str, Any]:
    """Read-only 「可以合并吗」 report for one managed worktree."""
    scope = _resolve_scope(settings, worktree_id)
    target_branch = _target_branch(settings, scope)
    source_branch = _current_branch(settings, str(scope["worktree_path"]))
    blockers = _collect_blockers(
        settings, scope, target_branch=target_branch, source_branch=source_branch,
    )
    return {
        "worktree_id": str(scope["worktree_id"]),
        "repo_id": str(scope["repo_id"]),
        "source_branch": source_branch,
        "target_branch": target_branch,
        "can_merge": not blockers,
        "blockers": blockers,
        "files": _changed_files(settings, scope, target_branch),
        "commits_ahead": _commits_ahead(settings, scope, target_branch),
        "has_uncommitted": _has_uncommitted(settings, str(scope["worktree_path"])),
    }


# ---------------------------------------------------------------------------
# audit rows (`supervisor_merges`, in the same state DB as the service layer)
# ---------------------------------------------------------------------------
# Plain statements, not one script: each one auto-commits under
# `isolation_level=None`, so the table provably exists on disk before the insert.
_MERGE_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS supervisor_merges (
    id TEXT PRIMARY KEY,
    worktree_id TEXT NOT NULL,
    repo_id TEXT NOT NULL,
    source_branch TEXT NOT NULL,
    target_branch TEXT NOT NULL,
    merge_commit TEXT NOT NULL,
    created_at TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_sup_merges_repo ON supervisor_merges(repo_id)",
    "CREATE INDEX IF NOT EXISTS idx_sup_merges_worktree ON supervisor_merges(worktree_id)",
)

_AUDIT_INSERT_SQL = (
    "INSERT INTO supervisor_merges "
    "(id, worktree_id, repo_id, source_branch, target_branch, merge_commit, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)


def _audit_db(settings: Any) -> sqlite3.Connection:
    """Connection to the supervisor state DB (`svc._db_path`) plus the audit table.

    Deliberately independent of `svc._connect`: same file, same path resolution,
    no shared schema-init bookkeeping — so a reader and a writer can never end up
    looking at two different databases.
    """
    conn = sqlite3.connect(str(svc._db_path(settings)), timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        for statement in _MERGE_SCHEMA_STATEMENTS:
            conn.execute(statement)
    except sqlite3.Error:
        log.warning("supervisor_merges schema init failed", exc_info=True)
    return conn


def _record_audit(
    settings: Any,
    scope: dict[str, Any],
    *,
    source_branch: str,
    target_branch: str,
    merge_commit: str,
) -> bool:
    """Write the audit row; True only when it is durably readable afterwards.

    A DB failure must not fail a merge that already happened, but it must never
    pass silently either: the failure is logged with its traceback and the row
    is read back on the same connection.
    """
    audit_id = f"mrg-{os.urandom(8).hex()}"
    conn: sqlite3.Connection | None = None
    try:
        conn = _audit_db(settings)
        conn.execute(
            _AUDIT_INSERT_SQL,
            (
                audit_id,
                str(scope["worktree_id"]),
                str(scope["repo_id"]),
                source_branch,
                target_branch,
                merge_commit,
                _utc_now(),
            ),
        )
        # isolation_level=None -> autocommit; commit() is a no-op safety net.
        conn.commit()
        stored = conn.execute(
            "SELECT COUNT(*) FROM supervisor_merges WHERE id = ?", (audit_id,)
        ).fetchone()[0]
        if int(stored) != 1:
            log.error("merge audit row %s is not readable after insert", audit_id)
            return False
        return True
    except Exception:
        log.error(
            "merge audit insert failed for worktree %s",
            scope["worktree_id"],
            exc_info=True,
        )
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def list_merge_audit(settings: Any, *, worktree_id: Any = None) -> list[dict[str, Any]]:
    """Audit trail, newest first (read-only helper for operators/tests)."""
    svc.require_enabled(settings)
    conn = _audit_db(settings)
    try:
        if worktree_id:
            wid = svc._validate_id_token(worktree_id, "worktree_id")
            rows = conn.execute(
                "SELECT * FROM supervisor_merges WHERE worktree_id = ? ORDER BY created_at DESC",
                (wid,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM supervisor_merges ORDER BY created_at DESC"
            ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# body validation
# ---------------------------------------------------------------------------

def _validate_body(body: Any) -> str:
    """`{"confirm": true, "message": optional str <=200}` -> cleaned message."""
    if not isinstance(body, dict):
        raise _error(HTTPStatus.BAD_REQUEST, "请求体必须是 JSON 对象", code=CODE_INVALID_BODY)
    if body.get("confirm") is not True:
        raise _error(
            HTTPStatus.BAD_REQUEST,
            "请确认后再合并（confirm: true）",
            code=CODE_CONFIRM_REQUIRED,
        )
    raw = body.get("message")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise _error(
            HTTPStatus.BAD_REQUEST,
            f"message 必须是不超过 {MAX_MESSAGE_CHARS} 个字的字符串",
            code=CODE_INVALID_MESSAGE,
        )
    cleaned = raw.strip()
    if len(cleaned) > MAX_MESSAGE_CHARS:
        raise _error(
            HTTPStatus.BAD_REQUEST,
            f"message 最长 {MAX_MESSAGE_CHARS} 个字",
            code=CODE_INVALID_MESSAGE,
        )
    if any(ch in cleaned for ch in ("\x00", "\r", "\n")):
        raise _error(
            HTTPStatus.BAD_REQUEST,
            "message 里不能有换行或控制字符",
            code=CODE_INVALID_MESSAGE,
        )
    return cleaned


# ---------------------------------------------------------------------------
# the merge itself
# ---------------------------------------------------------------------------

def _commit_worktree_changes(settings: Any, scope: dict[str, Any], message: str) -> None:
    """Rule 7a: commit the worktree's uncommitted changes as `Conveyor`."""
    worktree_path = str(scope["worktree_path"])
    commit_message = message or _default_commit_message(settings, scope)
    rc, _out, err = _git(settings, worktree_path, ["add", "-A"])
    if rc != 0:
        raise _error(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            f"暂存工作目录改动失败: {redact_text((err or '').strip())[:200]}",
            code=CODE_COMMIT_FAILED,
        )
    rc, _out, err = _git(
        settings,
        worktree_path,
        ["commit", "--no-verify", "-m", commit_message],
        config=(
            f"user.name={MERGE_AUTHOR_NAME}",
            f"user.email={MERGE_AUTHOR_EMAIL}",
        ),
    )
    if rc != 0:
        if not _has_uncommitted(settings, worktree_path):
            # e.g. only ignored files were pending: nothing was actually added.
            raise _error(
                HTTPStatus.CONFLICT,
                "这个 worktree 里没有可以合并到主分支的改动",
                code=CODE_NOTHING_TO_MERGE,
            )
        raise _error(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            f"提交工作目录改动失败: {redact_text((err or '').strip())[:200]}",
            code=CODE_COMMIT_FAILED,
        )


def _conflicting_paths(settings: Any, main_path: str) -> list[str]:
    rc, out, _ = _git(
        settings,
        main_path,
        ["diff", "--name-only", "--diff-filter=U", "--no-ext-diff", "--no-textconv", "-z"],
    )
    if rc != 0:
        return []
    return [p for p in out.split("\0") if p]


def _merge_file_count(settings: Any, main_path: str, merge_commit: str) -> int:
    rc, out, _ = _git(
        settings,
        main_path,
        [
            "diff", "--name-only", "--no-ext-diff", "--no-textconv", "-z",
            f"{merge_commit}^1",
            merge_commit,
        ],
    )
    if rc != 0 or not merge_commit:
        return 0
    return len([p for p in out.split("\0") if p])


def _run_merge(settings: Any, scope: dict[str, Any], message: str) -> dict[str, Any]:
    """Rules 6-8 for one already-locked merge attempt.

    Every blocker is re-evaluated *before* the first write: when any rule fails
    the merge is refused with a `409` and neither the worktree nor the main
    checkout is touched at all (no commit, no merge, no audit row).
    """
    main_path = str(scope["main_path"])
    target_branch = _target_branch(settings, scope)
    source_branch = _current_branch(settings, str(scope["worktree_path"]))

    # Re-check every rule: nothing may have changed since the preview.
    blockers = _collect_blockers(
        settings, scope, target_branch=target_branch, source_branch=source_branch,
    )
    if blockers:
        first = blockers[0]
        raise _error(HTTPStatus.CONFLICT, first["message"], code=first["code"])

    # 7a. Uncommitted work becomes a Conveyor-authored commit on the worktree.
    if _has_uncommitted(settings, str(scope["worktree_path"])):
        _commit_worktree_changes(settings, scope, message)

    # 7b. Nothing to merge?
    if _commits_ahead(settings, scope, target_branch) <= 0:
        raise _error(
            HTTPStatus.CONFLICT,
            "这个 worktree 里没有可以合并到主分支的改动",
            code=CODE_NOTHING_TO_MERGE,
        )

    # 7c. Merge into the main checkout, never pushing anywhere.
    main_head_before = _head_sha(settings, main_path)
    merge_message = MERGE_MESSAGE_TEMPLATE.format(branch=source_branch)
    rc, _out, err = _git(
        settings,
        main_path,
        [
            "merge",
            "--no-ff",
            "--no-verify",
            "--no-edit",
            "-m",
            merge_message,
            source_branch,
        ],
    )
    if rc != 0:
        conflicts = _conflicting_paths(settings, main_path)
        _git(settings, main_path, ["merge", "--abort"])
        restored = (
            _head_sha(settings, main_path) == main_head_before
            and _is_clean(settings, main_path)
        )
        if not restored:
            raise _error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "合并失败后没能把主工作目录恢复原状，请手动检查主工作目录的状态",
                code=CODE_MERGE_ABORT_FAILED,
            )
        raise _error(
            HTTPStatus.CONFLICT,
            "合并有冲突，已经撤销，主工作目录没有变化。可以让执行者在工作目录里先合并主分支再试。",
            code=CODE_MERGE_CONFLICT,
            extra={"conflicts": conflicts},
        )

    merge_commit = _head_sha(settings, main_path)
    if not merge_commit:
        raise _error(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "合并之后没能读到主工作目录的 HEAD",
            code=CODE_MERGE_ABORT_FAILED,
        )

    # 7d. Audit row + honest payload.
    if not _record_audit(
        settings,
        scope,
        source_branch=source_branch,
        target_branch=target_branch,
        merge_commit=merge_commit,
    ):
        # The merge itself is done and reported honestly; the missing audit row
        # is an operator-visible error, not a reason to lie about the merge.
        log.error(
            "merge of %s into %s succeeded but no audit row was written (worktree %s)",
            source_branch,
            target_branch,
            scope["worktree_id"],
        )
    return {
        "merged": True,
        "target_branch": target_branch,
        "merge_commit": merge_commit,
        "files_changed": _merge_file_count(settings, main_path, merge_commit),
    }


def merge_worktree(settings: Any, worktree_id: Any, body: Any) -> dict[str, Any]:
    """Owner-confirmed 「合并到主分支」.

    Serialized per repository and holding the worktree's runtime lock, so no task
    can be dispatched on the worktree while the merge runs.
    """
    message = _validate_body(body)
    scope = _resolve_scope(settings, worktree_id)
    repo_id = str(scope["repo_id"])
    with _repo_merge_lock(repo_id):
        worktree_path = str(scope["worktree_path"])
        lock_task_id = f"{MERGE_LOCK_TASK_PREFIX}{scope['worktree_id']}"
        try:
            ags.acquire_worktree_lock(
                settings, worktree_path, lock_task_id, ttl_seconds=MERGE_LOCK_TTL_SECONDS
            )
        except Exception as exc:  # SupervisorLockError / sqlite errors
            log.warning(
                "merge lock refused for worktree %s: %s", scope["worktree_id"], exc
            )
            raise _error(
                HTTPStatus.CONFLICT,
                "这个 worktree 上还有正在跑的任务或目标，等它结束后再合并",
                code=CODE_WORKTREE_BUSY,
            ) from exc
        try:
            return _run_merge(settings, scope, message)
        finally:
            try:
                ags.release_worktree_lock(settings, worktree_path, lock_task_id)
            except Exception:
                log.warning(
                    "merge lock release failed for worktree %s",
                    scope["worktree_id"],
                    exc_info=True,
                )
