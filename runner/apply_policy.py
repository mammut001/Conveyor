from __future__ import annotations

import os
import re
import fnmatch
from pathlib import Path
from typing import Any

# Denylist patterns
DENYLIST_PATTERNS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_ed25519",
    "**/id_rsa",
    "**/id_ed25519",
    "**/.ssh/**",
    "**/authorized_keys",
    "**/known_hosts",
    "**/*token*",
    "**/*secret*",
    "**/*password*",
    "**/*credential*",
    "desktop_nodes.json",
    ".git",
    ".git/**",
    "**/.git/**",
    "**/.venv/**",
    "**/venv/**",
    "**/node_modules/**",
    "**/__pycache__/**",
]

# High-risk project patterns
HIGH_RISK_PATTERNS = [
    ".github/workflows/**",
    "scripts/install.sh",
    "scripts/deploy.sh",
    "scripts/deploy_vps.sh",
    "systemd/**",
    "config.py",
    "desktop_agent_server.py",
    "bot.py",
    "feishu_bot.py",
    "**/deploy.sh",
    "**/deploy_vps.sh",
    "**/*deploy*",
    "**/*auth*",
    "requirements.txt",
    "**/requirements.txt",
    "**/*requirements.txt",
    "setup.py",
    "**/setup.py",
    "security/**",
    "**/security/**",
    "redaction.py",
    "**/redaction.py",
    "runner/apply_policy.py",
    "**/apply_policy.py",
]

# Allowed patterns
ALLOWLIST_PATTERNS = [
    "README.md",
    "README.zh.md",
    "docs/**",
    "runner/**",
    "handlers/**",
    "channel/**",
    "personal_tools/**",
    "nodes/**",
    "tests/**",
    "scripts/*_smoke.py",
    ".env.example",
]

def glob_to_regex(pattern: str) -> re.Pattern:
    """Build a regex pattern manually from a glob pattern for safety and compatibility."""
    regex_parts = []
    i = 0
    n = len(pattern)
    while i < n:
        if pattern[i:i+2] == "**":
            regex_parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            regex_parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            regex_parts.append("[^/]")
            i += 1
        elif pattern[i] in ".+^$()[]{}|\\":
            regex_parts.append(re.escape(pattern[i]))
            i += 1
        else:
            regex_parts.append(pattern[i])
            i += 1
    return re.compile("^" + "".join(regex_parts) + "$", re.IGNORECASE)

def is_binary_file(filepath: Path) -> bool:
    """Returns True if the file contains NUL bytes, indicating a binary file."""
    try:
        with open(filepath, "rb") as f:
            chunk = f.read(8000)
            return b"\0" in chunk
    except Exception:
        return True

class ApplyValidationResult:
    def __init__(self, allowed: bool, blocked_paths: list[str], reason: str = ""):
        self.allowed = allowed
        self.blocked_paths = blocked_paths
        self.reason = reason


class CollectResult:
    """Result of collecting changed/untracked paths from a worktree.

    ``ok=False`` means the underlying git command failed (or returned an
    unexpected payload). For apply safety the caller MUST treat a failed
    collection as "could not collect paths safely" and refuse the apply
    rather than silently behaving as if there were no paths. ``error`` is a
    short, safe, non-leaking reason string for logs/messages; it must not
    include raw repo paths or secrets.
    """
    def __init__(self, *, ok: bool, paths: list[str], error: str = ""):
        self.ok = ok
        self.paths = paths
        self.error = error

    @staticmethod
    def success(paths: list[str]) -> "CollectResult":
        return CollectResult(ok=True, paths=list(paths), error="")

    @staticmethod
    def failure(error: str) -> "CollectResult":
        return CollectResult(ok=False, paths=[], error=error)

class ApplyPolicy:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.allow_high_risk = getattr(settings, "conveyor_apply_allow_high_risk", False)
        self.max_untracked_bytes = getattr(settings, "conveyor_apply_max_untracked_bytes", 1048576)

    def validate_path(self, path: str, *, kind: str, worktree_path: Path | None = None) -> str | None:
        """Validate a single path. Returns None if allowed, or a string reason if rejected."""
        if not path or path.strip() == "":
            return "empty path"

        # Replace backslashes for cross-platform POSIX conversion
        norm_path = path.replace("\\", "/").strip()
        
        # Reject absolute paths
        if os.path.isabs(norm_path) or norm_path.startswith("/") or (len(norm_path) > 1 and norm_path[1] == ":"):
            return "absolute path"
            
        # Reject path traversal (..)
        components = norm_path.split("/")
        if ".." in components:
            return "path traversal"
            
        # Reject control characters or NUL
        if "\0" in norm_path or any(ord(c) < 32 for c in norm_path):
            return "invalid characters"
            
        # Reject extremely long paths
        if len(norm_path) > 1024:
            return "path too long"

        # Check if settings.codex_memory_root or settings.codex_task_root are under the repo
        try:
            rel_mem = os.path.relpath(self.settings.codex_memory_root, self.settings.codex_workspace_root)
            if not rel_mem.startswith("..") and not os.path.isabs(rel_mem):
                rel_mem_norm = os.path.normpath(rel_mem).replace("\\", "/")
                if norm_path == rel_mem_norm or norm_path.startswith(rel_mem_norm + "/"):
                    return "touches codex memory root"
        except Exception:
            pass

        try:
            rel_task = os.path.relpath(self.settings.codex_task_root, self.settings.codex_workspace_root)
            if not rel_task.startswith("..") and not os.path.isabs(rel_task):
                rel_task_norm = os.path.normpath(rel_task).replace("\\", "/")
                if norm_path == rel_task_norm or norm_path.startswith(rel_task_norm + "/"):
                    return "touches codex task root"
        except Exception:
            pass

        # Check Denylist (always block)
        for pattern in DENYLIST_PATTERNS:
            if glob_to_regex(pattern).match(norm_path):
                return f"denied by pattern: {pattern}"

        # Untracked safety checks
        if kind == "untracked" and worktree_path is not None:
            full_path = worktree_path / norm_path
            
            # Reject symlinks
            if full_path.is_symlink():
                return "symlink rejected"
                
            # Reject directories
            if full_path.is_dir():
                return "directories rejected"
                
            # Reject missing files
            if not full_path.exists():
                return "file does not exist"
                
            # Reject oversized files
            try:
                size = full_path.stat().st_size
                if size > self.max_untracked_bytes:
                    return f"file too large ({size} bytes)"
            except OSError:
                return "cannot read metadata"
                
            # Reject binary files
            if is_binary_file(full_path):
                return "binary untracked file"

        # Check requirements.txt setting
        if norm_path == "requirements.txt":
            if not self.allow_high_risk:
                return "requirements.txt requires CONVEYOR_APPLY_ALLOW_HIGH_RISK=true"
            return None

        # Check High-risk files
        is_high_risk = False
        for pattern in HIGH_RISK_PATTERNS:
            if glob_to_regex(pattern).match(norm_path):
                is_high_risk = True
                break

        if is_high_risk:
            if not self.allow_high_risk:
                return f"high-risk file rejected: {norm_path}"
            return None

        # Check Allowlist
        for pattern in ALLOWLIST_PATTERNS:
            if glob_to_regex(pattern).match(norm_path):
                return None

        return "not in allowlist"

def collect_tracked_changed_files(worktree_path: Path) -> CollectResult:
    """Run ``git diff --name-only HEAD`` to collect all tracked changes.

    Fail closed: on any subprocess error, non-zero exit, or a non-existing
    worktree, returns ``CollectResult(ok=False, ...)``. The caller must
    refuse the apply on a failed collection instead of treating it as "no
    paths to validate".
    """
    import subprocess
    if not worktree_path or not Path(worktree_path).exists():
        return CollectResult.failure("worktree not available")
    try:
        res = subprocess.run(
            ["git", "diff", "--name-only", "HEAD", "--", ".", ":(exclude)MEMORY.md"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Do not include str(exc) verbatim; it may carry repo paths or
        # environment details. Return a safe, fixed reason.
        return CollectResult.failure("git diff failed")
    except Exception:
        return CollectResult.failure("git diff failed")
    paths = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    return CollectResult.success(paths)

def collect_untracked_files(worktree_path: Path) -> CollectResult:
    """Run ``git ls-files`` to collect untracked files.

    Fail closed: on any subprocess error, non-zero exit, or a non-existing
    worktree, returns ``CollectResult(ok=False, ...)``. The caller must
    refuse the apply on a failed collection instead of treating it as "no
    untracked paths to validate".
    """
    import subprocess
    if not worktree_path or not Path(worktree_path).exists():
        return CollectResult.failure("worktree not available")
    try:
        res = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return CollectResult.failure("git ls-files failed")
    except Exception:
        return CollectResult.failure("git ls-files failed")
    paths = [p for p in res.stdout.split("\0") if p.strip()]
    return CollectResult.success(paths)

def validate_apply_paths(paths: list[str], *, kind: str, settings: Any, worktree_path: Path | None = None) -> ApplyValidationResult:
    """Validate a batch of paths of the same kind."""
    policy = ApplyPolicy(settings)
    
    # Check total size limit of untracked files
    if kind == "untracked" and worktree_path is not None:
        total_size = 0
        for path in paths:
            norm_path = path.replace("\\", "/").strip()
            full_path = worktree_path / norm_path
            if full_path.exists() and not full_path.is_symlink() and full_path.is_file():
                try:
                    total_size += full_path.stat().st_size
                except OSError:
                    pass
        if total_size > policy.max_untracked_bytes:
            return ApplyValidationResult(
                False,
                paths,
                f"total size of untracked files ({total_size} bytes) exceeds limit ({policy.max_untracked_bytes} bytes)"
            )

    blocked_paths = []
    reasons = []
    
    for path in paths:
        reason = policy.validate_path(path, kind=kind, worktree_path=worktree_path)
        if reason:
            blocked_paths.append(path)
            reasons.append(f"{path} ({reason})")
            
    if blocked_paths:
        return ApplyValidationResult(False, blocked_paths, ", ".join(reasons))
    return ApplyValidationResult(True, [])

def is_allowed_apply_path(path: str, settings: Any) -> bool:
    """Helper to check if a path is allowed."""
    policy = ApplyPolicy(settings)
    return policy.validate_path(path, kind="tracked") is None

def is_denied_apply_path(path: str, settings: Any) -> bool:
    """Helper to check if a path is denied."""
    policy = ApplyPolicy(settings)
    return policy.validate_path(path, kind="tracked") is not None
