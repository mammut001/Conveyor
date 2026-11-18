"""Guard rail against accidental deletions in agent child processes.

This is deliberately NOT a security boundary. A chat job that only had to
*read* something ("qa-todo-cli 进度怎么样了") must not be able to delete it,
yet the model occasionally runs ``rm -f /tmp/...`` on its own and then
apologises. The system prompt alone does not stop that, so the child of a
non-approved job gets a directory of POSIX sh shims first on its ``PATH``:

* ``rm`` / ``rmdir`` / ``unlink`` / ``shred`` refuse every call, print the
  message below on stderr and exit 126 without touching the file system.
* ``git`` blocks only the destructive subcommands (``clean``,
  ``reset --hard``, ``push --force`` / ``-f`` / ``--force-with-lease``,
  ``branch -D``, ``worktree remove``) the same way and execs the real git
  binary for everything else.

An operator who approved the deletion (``Job.destructive_confirmed``) never
gets the guard, and neither do the Supervisor implementer rounds, which
legitimately delete files inside their worktree.
"""
from __future__ import annotations

import os
from pathlib import Path

# Deletion commands whose shims refuse every invocation.
DELETE_COMMANDS = ("rm", "rmdir", "unlink", "shred")

# Same text for every blocked command, so the agent (and the operator reading
# the log) sees one consistent, actionable sentence.
GUARD_MESSAGE = (
    "这个删除操作要先经过主人确认，已拦下（没有删除任何东西）。"
    "请把要删除的内容告诉主人，让他确认后再执行。"
)

# Exit status of a refused command (126: found but not executable).
BLOCK_EXIT_CODE = 126

# git global options that take a separate value; the subcommand that follows
# them is still the one that has to be checked.
_GIT_VALUE_OPTIONS = ("-C", "-c", "--git-dir", "--work-tree", "--namespace",
                      "--exec-path", "--super-prefix")
_GIT_VALUE_OPTION_PREFIXES = tuple(f"{opt}=" for opt in _GIT_VALUE_OPTIONS)

_DELETE_SHIM_TEMPLATE = """#!/bin/sh
# Conveyor delete guard (runner/delete_guard.py). Guard rail against an
# accidental deletion, not a security boundary: nothing is deleted here.
printf '%s\\n' {message} >&2
exit {exit_code}
"""

_GIT_SHIM_TEMPLATE = """#!/bin/sh
# Conveyor delete guard (runner/delete_guard.py). Blocks the destructive git
# subcommands and execs the real git for everything else. Guard rail against
# an accidental deletion, not a security boundary: nothing is deleted here.
#
# POSIX sh only, and deliberately without any command substitution: a case
# pattern's ')' would close a $( ) early, which dash rejects as a syntax
# error. Everything below runs at the top level and only sets variables.
real_git={real_git}
blocked={message}

# Find the subcommand, skipping git's global options and the values they take,
# so "git -C <dir> clean" is caught too. Nothing is shifted, so "$@" stays
# intact for the final exec. "sub_arg" is the next non-option argument, which
# is what distinguishes "git worktree remove" from "git worktree list".
sub=""
sub_arg=""
skip_next=0
for arg in "$@"; do
    if [ "$skip_next" = "1" ]; then
        skip_next=0
        continue
    fi
    case "$arg" in
        {value_options})
            skip_next=1
            ;;
        {value_option_prefixes})
            ;;
        -*)
            ;;
        *)
            if [ -z "$sub" ]; then
                sub="$arg"
            else
                sub_arg="$arg"
                break
            fi
            ;;
    esac
done

is_blocked=0
case "$sub" in
    clean)
        is_blocked=1
        ;;
    reset)
        for arg in "$@"; do
            case "$arg" in
                --hard) is_blocked=1 ;;
            esac
        done
        ;;
    push)
        for arg in "$@"; do
            case "$arg" in
                -f|--force|--force-with-lease|--force-with-lease=*) is_blocked=1 ;;
            esac
        done
        ;;
    branch)
        for arg in "$@"; do
            case "$arg" in
                -D) is_blocked=1 ;;
            esac
        done
        ;;
    worktree)
        if [ "$sub_arg" = "remove" ]; then is_blocked=1; fi
        ;;
esac

if [ "$is_blocked" = "1" ]; then
    printf '%s\\n' "$blocked" >&2
    exit {exit_code}
fi
exec "$real_git" "$@"
"""


def _sh_quote(value: str) -> str:
    """``value`` as a single-quoted POSIX sh word (safe for any path)."""
    return "'" + value.replace("'", "'\\''") + "'"


def _delete_shim() -> str:
    return _DELETE_SHIM_TEMPLATE.format(message=_sh_quote(GUARD_MESSAGE), exit_code=BLOCK_EXIT_CODE)


def _git_shim(real_git: str) -> str:
    return _GIT_SHIM_TEMPLATE.format(
        real_git=_sh_quote(real_git),
        message=_sh_quote(GUARD_MESSAGE),
        exit_code=BLOCK_EXIT_CODE,
        value_options="|".join(_GIT_VALUE_OPTIONS),
        value_option_prefixes="|".join(_GIT_VALUE_OPTION_PREFIXES),
    )


def _write_shim(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _real_git_path(guard_dir: Path) -> str | None:
    """Absolute path of the real ``git``, ignoring anything inside ``guard_dir``.

    The guard directory is prepended to the child PATH only after the shims
    exist, so a shim that execs a bare ``git`` could resolve to itself and
    fork-loop; the absolute path avoids that. ``None`` when git is not
    installed at all, in which case no git command can run either and no
    shim is written.
    """
    guard = Path(guard_dir).resolve()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / "git"
        try:
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                continue
            resolved = candidate.resolve()
        except OSError:
            continue
        try:
            resolved.relative_to(guard)
            continue
        except ValueError:
            pass
        return str(resolved)
    return None


def make_guard_dir(base: Path) -> Path:
    """Write the deletion guard shims into ``base`` and return ``base``.

    ``base`` should be a fresh empty directory (``tempfile.mkdtemp``); the
    returned path is the guard directory to prepend to the child's PATH.
    """
    directory = Path(base)
    directory.mkdir(parents=True, exist_ok=True)
    for name in DELETE_COMMANDS:
        _write_shim(directory / name, _delete_shim())
    real_git = _real_git_path(directory)
    if real_git:
        _write_shim(directory / "git", _git_shim(real_git))
    return directory


def prepend_guard_to_path(env: dict[str, str], guard_dir: Path) -> str:
    """Put ``guard_dir`` first in ``env['PATH']`` and return the new value.

    Empty PATH entries are dropped: an empty entry means "current directory",
    which would silently widen the child's command search path.
    """
    entries = [str(guard_dir)]
    base = env.get("PATH") or os.defpath
    entries += [entry for entry in base.split(os.pathsep) if entry]
    env["PATH"] = os.pathsep.join(entries)
    return env["PATH"]
