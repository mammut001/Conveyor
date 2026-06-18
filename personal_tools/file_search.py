"""personal_tools/file_search.py — File Search for Conveyor (P4.2 / P4.2.1).

Natural-language-first file search with strict safety boundaries.
Only allows searching under configured roots, rejects secrets/sensitive files.
All output passes redact_text + truncate.

Internal functions return structured results with absolute paths for
safe reading; display paths are for user-facing output only.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from config import Settings
from personal_tools.base import ToolResult
from redaction import redact_text, truncate

logger = logging.getLogger(__name__)

# Blocked patterns (relative to root)
_BLOCKED_PATTERNS = (
    ".env",
    "secrets/",
    ".ssh/",
    "private",
    "token",
    "google_token.json",
    "client_secret.json",
    "credentials.json",
    ".pem",
    ".key",
    "id_rsa",
    "id_ed25519",
)

# Binary file extensions to skip
_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".bin",
    ".mp3", ".mp4", ".avi", ".mov", ".wav",
    ".sqlite", ".db",
})


def _get_allowed_roots(settings: Settings) -> list[Path]:
    """Get list of allowed search roots."""
    roots = []
    
    # Always include workspace root
    roots.append(settings.codex_workspace_root)
    
    # Include memory root (notes)
    roots.append(settings.codex_memory_root / "notes")
    
    # Include KB root if configured
    kb_root = settings.kb_root or str(settings.codex_memory_root / "kb")
    roots.append(Path(kb_root))
    
    # Include additional allowed roots if configured
    if settings.file_search_allowed_roots:
        for root_str in settings.file_search_allowed_roots.split(","):
            root_str = root_str.strip()
            if root_str:
                roots.append(Path(root_str).expanduser().resolve())
    
    return roots


def _is_path_allowed(path: Path, allowed_roots: list[Path]) -> bool:
    """Check if path is under any allowed root."""
    try:
        resolved = path.resolve()
        for root in allowed_roots:
            try:
                resolved.relative_to(root.resolve())
                return True
            except ValueError:
                continue
        return False
    except (OSError, ValueError):
        return False


def _is_blocked_pattern(path_str: str) -> bool:
    """Check if path matches any blocked pattern."""
    lower = path_str.lower()
    for pattern in _BLOCKED_PATTERNS:
        if pattern in lower:
            return True
    return False


def _is_binary_file(path: Path) -> bool:
    """Check if file has a binary extension."""
    return path.suffix.lower() in _BINARY_EXTENSIONS


def _is_too_large(path: Path, max_bytes: int) -> bool:
    """Check if file exceeds max size."""
    try:
        return path.stat().st_size > max_bytes
    except OSError:
        return True


def list_roots(settings: Settings) -> ToolResult:
    """List configured search roots."""
    roots = _get_allowed_roots(settings)
    lines = ["允许的搜索根目录:"]
    for root in roots:
        exists = root.exists()
        status = "✓" if exists else "✗"
        lines.append(f"  {status} {root}")
    return ToolResult(ok=True, text="\n".join(lines))


def _search_files_internal(
    settings: Settings,
    query: str,
) -> list[dict[str, object]]:
    """Internal search returning structured results with absolute paths.

    Each result dict has:
      - absolute_path: resolved path safe for internal reading
      - display_path: relative path for user-facing output
      - size: file size in bytes
      - matches: list of matching line previews
    """
    allowed_roots = _get_allowed_roots(settings)
    extensions = set(settings.file_search_extensions.split(","))
    max_results = settings.file_search_max_results
    max_bytes = settings.file_search_max_file_bytes

    results: list[dict[str, object]] = []
    seen_paths: set[str] = set()

    for root in allowed_roots:
        if not root.exists():
            continue

        for path in root.rglob("*"):
            if len(results) >= max_results:
                break

            if not path.is_file():
                continue

            # Check extension
            if path.suffix not in extensions:
                continue

            # Check blocked patterns against relative path within root
            try:
                rel_path = str(path.relative_to(root))
            except ValueError:
                continue
            if _is_blocked_pattern(rel_path):
                continue

            # Check binary
            if _is_binary_file(path):
                continue

            # Check size
            if _is_too_large(path, max_bytes):
                continue

            # Check if already seen (dedup across roots)
            resolved = str(path.resolve())
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)

            # Search file content
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            # Simple case-insensitive search
            if query.lower() in content.lower() or query.lower() in path.name.lower():
                # Find matching lines
                matches = []
                for i, line in enumerate(content.splitlines(), 1):
                    if query.lower() in line.lower():
                        matches.append(f"  L{i}: {line.strip()[:100]}")
                        if len(matches) >= 3:
                            break

                results.append({
                    "absolute_path": resolved,
                    "display_path": rel_path,
                    "size": path.stat().st_size,
                    "matches": matches,
                })

    return results


def search_files(
    settings: Settings,
    query: str,
    *,
    operator_id: str = "",
) -> ToolResult:
    """Search for files matching query in allowed roots."""
    if not settings.file_search_enabled:
        return ToolResult(ok=False, text="⚠️ 文件搜索已禁用")

    query = query.strip()
    if not query:
        return ToolResult(ok=False, text="⚠️ 用法: /files_search <查询词>")

    results = _search_files_internal(settings, query)

    if not results:
        return ToolResult(ok=True, text=f"未找到匹配 '{query}' 的文件")

    lines = [f"搜索结果 ({len(results)} 个文件匹配 '{query}'):", ""]
    for r in results:
        lines.append(f"📄 {r['display_path']} ({r['size']} bytes)")
        lines.extend(r["matches"])  # type: ignore[arg-type]
        lines.append("")

    return ToolResult(ok=True, text=truncate(redact_text("\n".join(lines))))


def read_file(
    settings: Settings,
    path_str: str,
    *,
    operator_id: str = "",
) -> ToolResult:
    """Read a file from allowed roots."""
    if not settings.file_search_enabled:
        return ToolResult(ok=False, text="⚠️ 文件搜索已禁用")
    
    path_str = path_str.strip()
    if not path_str:
        return ToolResult(ok=False, text="⚠️ 用法: /files_read <文件路径>")
    
    allowed_roots = _get_allowed_roots(settings)
    path = Path(path_str).expanduser()
    
    # Check if path is allowed
    if not _is_path_allowed(path, allowed_roots):
        return ToolResult(ok=False, text=f"⚠️ 路径不在允许的搜索范围内: {path_str}")
    
    # Check blocked patterns
    if _is_blocked_pattern(path_str):
        return ToolResult(ok=False, text="⚠️ 拒绝访问敏感文件")
    
    # Check if file exists
    if not path.exists():
        return ToolResult(ok=False, text=f"⚠️ 文件不存在: {path_str}")
    
    if not path.is_file():
        return ToolResult(ok=False, text=f"⚠️ 不是文件: {path_str}")
    
    # Check binary
    if _is_binary_file(path):
        return ToolResult(ok=False, text="⚠️ 跳过二进制文件")
    
    # Check size
    max_bytes = settings.file_search_max_file_bytes
    if _is_too_large(path, max_bytes):
        return ToolResult(ok=False, text=f"⚠️ 文件过大 (超过 {max_bytes} bytes)")
    
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ToolResult(ok=False, text=f"⚠️ 读取失败: {exc}")
    
    # Truncate content
    if len(content) > max_bytes:
        content = content[:max_bytes] + "\n... [truncated]"
    
    return ToolResult(ok=True, text=truncate(redact_text(content)))


def _read_snippet_by_absolute_path(
    settings: Settings,
    absolute_path: str,
    display_path: str,
    query: str,
) -> str:
    """Read a safe snippet from an absolute path.

    Returns a formatted evidence string with display_path, or empty string
    on any safety/validation failure.
    """
    allowed_roots = _get_allowed_roots(settings)
    path = Path(absolute_path)

    # Verify path is still allowed (defense in depth)
    if not _is_path_allowed(path, allowed_roots):
        return ""

    # Verify not blocked
    if _is_blocked_pattern(display_path):
        return ""

    if not path.exists() or not path.is_file():
        return ""

    if _is_binary_file(path):
        return ""

    max_bytes = settings.file_search_max_file_bytes
    if _is_too_large(path, max_bytes):
        return ""

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    # Extract relevant excerpt around first match
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if query.lower() in line.lower():
            start = max(0, i - 2)
            end = min(len(lines), i + 8)
            excerpt = "\n".join(lines[start:end])
            return f"## {display_path}\n{excerpt}"

    return ""


def collect_file_facts(
    settings: Settings,
    operator_id: str,
    query: str,
) -> str:
    """Collect file facts for hybrid synthesis.

    Searches files using absolute paths internally, reads top snippets
    safely, and builds an evidence pack with display paths.
    Returns an evidence string suitable for Codex synthesis.
    """
    if not settings.file_search_enabled:
        return ""

    query = query.strip()
    if not query:
        return ""

    # Use internal search that returns absolute paths
    results = _search_files_internal(settings, query)
    if not results:
        return ""

    # Read top snippets using absolute paths
    evidence = []
    for r in results[:3]:
        snippet = _read_snippet_by_absolute_path(
            settings,
            str(r["absolute_path"]),
            str(r["display_path"]),
            query,
        )
        if snippet:
            evidence.append(snippet)

    if not evidence:
        return ""

    return "\n\n".join(evidence)


# --- Adapters for personal_tools/registry.py ---

async def files_list_roots_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    return list_roots(settings)


async def files_search_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return search_files(settings, arg, operator_id=operator_id)


async def files_read_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return read_file(settings, arg, operator_id=operator_id)
