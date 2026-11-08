"""supervisor_diff.py — read-only git diff payloads for Supervisor tasks and goals.

Owners need to see WHAT changed, so this module renders the same candidate
surface the reviewer prompt uses (`base_sha` -> current worktree content:
committed + staged + unstaged, plus untracked text files) as a structured
payload for the web API:

    {"task_id"|"goal_id", "base_sha", "head_sha",
     "files": [{"path", "status", "additions", "deletions"}],
     "diff": "<unified diff text>", "truncated": bool, "untracked": [paths]}

Safety rules enforced here (never trust a caller-supplied worktree path):
- The worktree must canonicalize to a real directory *inside* the runtime's
  managed supervisor worktree root (`<codex_task_root>/worktrees/supervisor`),
  and git must accept it as a worktree root.
- Every git invocation runs with a timeout, `GIT_OPTIONAL_LOCKS=0`, no pager,
  no external diff driver and no textconv filter, in a scrubbed child
  environment. Only read-only porcelain is used, so no git hook can fire.
- Diff text is capped (`MAX_DIFF_CHARS`) and flagged with `truncated`.

Redaction policy (deliberate): `redaction.redact_text` is NOT applied to the
diff body. Its value patterns are tuned for logs/messages and rewrite ordinary
source code — e.g. `\\b(cli_)[A-Za-z0-9_-]{8,}\\b` turns `_cli_load_runner` into
`[REDACTED]`, and `\\b(token|secret|password|api[_-]?key)\\b\\s*[:=]\\s*...`
rewrites `self.token = token`. Returning a mangled diff would defeat the whole
purpose of the endpoint, so the diff is returned verbatim; the route is
bearer-authenticated, read-only, and never logged.
"""
from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
from http import HTTPStatus
from typing import Any

import agent_supervisor as ags
import supervisor_service as svc
from subprocess_env import utility_env

log = logging.getLogger("conveyor.supervisor_diff")

# --------------------------------------------------------------------------
# limits
# --------------------------------------------------------------------------
MAX_DIFF_CHARS = 200_000
MAX_UNTRACKED_INLINE_BYTES = 64 * 1024
MAX_FILES = 1000
MAX_UNTRACKED = 1000
GIT_TIMEOUT_SECONDS = 20.0
_BINARY_SNIFF_BYTES = 8192

_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
# git name-status letters -> the API's stable status vocabulary.
_STATUS_MAP = {
    "A": "A",
    "M": "M",
    "D": "D",
    "R": "R",
    "C": "R",
    "T": "M",
    "U": "M",
    "X": "M",
    "B": "M",
}
# Env vars that would silently redirect git at another repository/index.
_STRIPPED_GIT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_LITERAL_PATHSPECS",
)


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------

def _git_env(settings: Any) -> dict[str, str]:
    """Child env for read-only git: inherited/allowlisted base plus safe overrides."""
    base = utility_env(settings)
    env = dict(base) if base else dict(os.environ)
    for key in _STRIPPED_GIT_ENV:
        env.pop(key, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_PAGER"] = "cat"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git_argv(worktree: str, args: list[str]) -> list[str]:
    # `--no-pager` is a global option, so it leads the argv. `core.fsmonitor=false`
    # is mandatory: an agent working in the worktree can write
    # `core.fsmonitor=<command>` into the shared repository config, and git would
    # then run that command on every diff/ls-files the owner triggers just by
    # viewing changes. `core.quotePath=false` keeps non-ASCII paths readable.
    return [
        "git",
        "--no-pager",
        "-C",
        worktree,
        "-c",
        "core.quotePath=false",
        "-c",
        "core.fsmonitor=false",
        *args,
    ]


def _git(settings: Any, worktree: str, args: list[str],
         *, timeout: float = GIT_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """Run one read-only git command inside an already-validated worktree.

    Bytes mode (no universal-newline translation) so CRLF content and `-z`
    NUL-terminated records survive exactly as git emitted them.
    """
    try:
        proc = subprocess.run(
            _git_argv(worktree, args),
            capture_output=True,
            env=_git_env(settings),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise svc.SupervisorServiceError(
            HTTPStatus.GATEWAY_TIMEOUT,
            "git diff 操作超时",
            code="git_timeout",
        ) from exc
    except OSError as exc:
        raise svc.SupervisorServiceError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            f"git 执行失败: {exc}",
            code="git_unavailable",
        ) from exc

    def _decode(raw: bytes | None) -> str:
        return (raw or b"").decode("utf-8", "replace")

    return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_GIT_SHA_RE.match(value.strip()))


def _commit_exists(settings: Any, worktree: str, sha: str) -> bool:
    rc, out, _ = _git(settings, worktree,
                      ["rev-parse", "--verify", "--quiet", f"{sha.strip()}^{{commit}}"])
    return rc == 0 and bool(out.strip())


def _head_sha(settings: Any, worktree: str) -> str:
    rc, out, _ = _git(settings, worktree, ["rev-parse", "--verify", "HEAD"])
    head = out.strip()
    return head if (rc == 0 and _is_sha(head)) else ""


# --------------------------------------------------------------------------
# worktree containment (canonical-path helpers from the service layer)
# --------------------------------------------------------------------------

def _resolve_worktree(settings: Any, raw_worktree_path: Any) -> str:
    """Canonicalize + contain a worktree path before any git command touches it."""
    canon = svc._canonical_internal_path(str(raw_worktree_path or ""))
    if not canon:
        raise svc.SupervisorServiceError(
            HTTPStatus.NOT_FOUND,
            "任务尚未关联 worktree，无法读取 diff",
            code="worktree_missing",
        )
    managed_root = svc._canonical_internal_path(str(svc._managed_worktree_root(settings)))
    if not (canon == managed_root or canon.startswith(managed_root + os.sep)):
        raise svc.SupervisorServiceError(
            HTTPStatus.FORBIDDEN,
            "worktree 不在受管目录内，拒绝读取 diff",
            code="worktree_outside_managed_root",
        )
    if not os.path.isdir(canon):
        raise svc.SupervisorServiceError(
            HTTPStatus.NOT_FOUND,
            "worktree 已不存在，无法读取 diff",
            code="worktree_missing",
        )
    if not ags.is_git_worktree(canon):
        raise svc.SupervisorServiceError(
            HTTPStatus.BAD_REQUEST,
            "worktree 不是有效的 git 工作树",
            code="invalid_worktree",
        )
    return canon


# --------------------------------------------------------------------------
# base sha resolution
# --------------------------------------------------------------------------

def _resolve_base(settings: Any, worktree: str, base_sha: Any, fallback_base_sha: Any) -> str:
    """Prefer the captured base; fall back to the queued target sha, then HEAD."""
    for candidate in (base_sha, fallback_base_sha):
        sha = str(candidate or "").strip()
        if _is_sha(sha) and _commit_exists(settings, worktree, sha):
            return sha
    head = _head_sha(settings, worktree)
    if head:
        return head
    raise svc.SupervisorServiceError(
        HTTPStatus.BAD_REQUEST,
        "无法确定 diff 基准提交（base_sha 无效且 HEAD 不可用）",
        code="diff_base_unresolved",
    )


# --------------------------------------------------------------------------
# changed files (statuses + counts)
# --------------------------------------------------------------------------

def _parse_name_status(raw: str) -> list[tuple[str, list[str]]]:
    """Parse `git diff --name-status -z` into `(status, [paths])` records."""
    parts = raw.split("\0")
    records: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(parts):
        status = parts[i]
        i += 1
        if not status:
            continue
        wanted = 2 if status[:1].upper() in ("R", "C") else 1
        paths: list[str] = []
        for _ in range(wanted):
            if i >= len(parts):
                break
            paths.append(parts[i])
            i += 1
        paths = [p for p in paths if p]
        if paths:
            records.append((status, paths))
    return records


def _split_rename_display(text: str) -> list[str]:
    """Candidates for a non `-z` rename display path (`old => new`, `pre{old => new}post`)."""
    idx = text.find(" => ")
    if idx < 0:
        return [text]
    left, right = text[:idx], text[idx + 4:]
    open_idx = left.find("{")
    close_idx = right.find("}")
    if open_idx >= 0 and close_idx >= 0:
        prefix, suffix = left[:open_idx], right[close_idx + 1:]
        old, new = left[open_idx + 1:], right[:close_idx]
        return [f"{prefix}{old}{suffix}", f"{prefix}{new}{suffix}", old, new]
    return [left, right]


def _parse_numstat(raw: str) -> dict[str, tuple[int, int]]:
    """Parse `git diff --numstat -z` into `{path: (additions, deletions)}`.

    Handles plain records, the `-z` rename form (`<add>\\t<del>\\t\\0<old>\\0<new>\\0`)
    and the display form (`old => new`) so a git format difference degrades to
    missing counts instead of a wrong file entry.
    """
    out: dict[str, tuple[int, int]] = {}
    parts = raw.split("\0")
    i = 0

    def _count(field: str) -> int:
        try:
            return int(field.strip())
        except (TypeError, ValueError):
            return 0

    while i < len(parts):
        record = parts[i]
        i += 1
        if not record:
            continue
        fields = record.split("\t")
        if len(fields) < 3:
            continue
        additions, deletions = _count(fields[0]), _count(fields[1])
        if fields[2] == "":
            # `-z` rename/copy: the two paths follow as separate NUL-terminated fields.
            src = parts[i] if i < len(parts) else ""
            dst = parts[i + 1] if i + 1 < len(parts) else ""
            i += 2
            key = dst or src
            if key:
                out[key] = (additions, deletions)
            continue
        out[fields[2]] = (additions, deletions)
    return out


def _collect_tracked_files(settings: Any, worktree: str, base: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    rc, out, _ = _git(settings, worktree, ["diff", "--name-status", "-z", "--no-ext-diff", base])
    if rc == 0:
        for status, paths in _parse_name_status(out):
            letter = _STATUS_MAP.get(status[:1].upper(), "M")
            path = paths[-1]
            if not path or path in index:
                continue
            entry = {"path": path, "status": letter, "additions": 0, "deletions": 0}
            files.append(entry)
            index[path] = entry

    rc, out, _ = _git(settings, worktree, ["diff", "--numstat", "-z", "--no-ext-diff", "--no-textconv", base])
    if rc == 0:
        for key, counts in _parse_numstat(out).items():
            entry = index.get(key)
            if entry is None:
                for candidate in _split_rename_display(key):
                    entry = index.get(candidate)
                    if entry is not None:
                        break
            if entry is None:
                continue
            entry["additions"], entry["deletions"] = counts
    return files[:MAX_FILES]


# --------------------------------------------------------------------------
# untracked files
# --------------------------------------------------------------------------

def _list_untracked(settings: Any, worktree: str) -> list[str]:
    rc, out, _ = _git(settings, worktree,
                      ["ls-files", "--others", "--exclude-standard", "-z"])
    if rc != 0:
        return []
    return [p for p in out.split("\0") if p][:MAX_UNTRACKED]


def _inside(root: str, relative: str) -> bool:
    if not relative or relative.startswith("/") or ".." in relative.split("/"):
        return False
    full = os.path.realpath(os.path.join(root, relative))
    return full == root or full.startswith(root + os.sep)


def _inline_text(worktree: str, relative: str) -> str | None:
    """Text content of a small untracked file, or None when it must be listed only."""
    if not _inside(worktree, relative):
        return None
    full = os.path.join(worktree, relative)
    try:
        st = os.lstat(full)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_UNTRACKED_INLINE_BYTES:
        return None
    try:
        with open(full, "rb") as handle:
            data = handle.read(MAX_UNTRACKED_INLINE_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_UNTRACKED_INLINE_BYTES:
        return None
    if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _synthetic_section(relative: str, text: str) -> tuple[str, int]:
    """A `new file` unified diff for untracked content, plus its added-line count."""
    header = (
        f"diff --git a/{relative} b/{relative}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{relative}\n"
    )
    lines = text.splitlines()
    if not lines:
        return header, 0
    body = "".join(f"+{line}\n" for line in lines)
    if not text.endswith("\n"):
        body += "\\ No newline at end of file\n"
    return header + f"@@ -0,0 +1,{len(lines)} @@\n" + body, len(lines)


def _untracked_sections(
    worktree: str,
    untracked: list[str],
    budget: int,
) -> tuple[list[str], dict[str, int], bool]:
    """Synthetic new-file diffs for untracked text files within `budget` chars."""
    sections: list[str] = []
    counts: dict[str, int] = {}
    truncated = False
    if budget <= 0:
        # The tracked diff already filled the cap: only flag truncation when an
        # inlinable untracked file really was left out.
        for relative in untracked:
            if _inline_text(worktree, relative) is not None:
                return sections, counts, True
        return sections, counts, False
    for relative in untracked:
        text = _inline_text(worktree, relative)
        if text is None:
            continue
        section, count = _synthetic_section(relative, text)
        counts[relative] = count
        if len(section) + 1 > budget:
            keep = section[:budget]
            cut = keep.rfind("\n")
            if cut > 0:
                keep = keep[: cut + 1]
            if keep:
                sections.append(keep)
            truncated = True
            break
        sections.append(section)
        budget -= len(section) + 1
    return sections, counts, truncated


# --------------------------------------------------------------------------
# public payload builder
# --------------------------------------------------------------------------

def build_worktree_diff(
    settings: Any,
    *,
    worktree_path: Any,
    base_sha: Any = "",
    fallback_base_sha: Any = "",
) -> dict[str, Any]:
    """Structured diff of `base_sha` -> current worktree content for one managed worktree."""
    worktree = _resolve_worktree(settings, worktree_path)
    base = _resolve_base(settings, worktree, base_sha, fallback_base_sha)
    head = _head_sha(settings, worktree)

    files = _collect_tracked_files(settings, worktree, base)

    rc, out, _ = _git(settings, worktree,
                      ["diff", "--no-color", "--no-ext-diff", "--no-textconv", base])
    diff_text = out if rc == 0 else ""
    truncated = False
    if len(diff_text) > MAX_DIFF_CHARS:
        keep = diff_text[:MAX_DIFF_CHARS]
        cut = keep.rfind("\n")
        if cut > 0:
            keep = keep[: cut + 1]
        diff_text = keep
        truncated = True

    untracked = _list_untracked(settings, worktree)
    sections, inline_counts, untracked_truncated = _untracked_sections(
        worktree, untracked, budget=MAX_DIFF_CHARS - len(diff_text),
    )
    if sections:
        diff_text = diff_text + ("\n" if diff_text and not diff_text.endswith("\n") else "") \
            + "\n".join(sections)
    truncated = truncated or untracked_truncated

    known = {entry["path"] for entry in files}
    for relative in untracked:
        if relative in known or len(files) >= MAX_FILES:
            continue
        files.append({
            "path": relative,
            "status": "?",
            "additions": inline_counts.get(relative, 0),
            "deletions": 0,
        })

    return {
        "base_sha": base,
        "head_sha": head,
        "files": files,
        "diff": diff_text,
        "truncated": truncated,
        "untracked": untracked,
    }
