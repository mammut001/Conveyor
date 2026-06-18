"""personal_tools/kb.py — Knowledge Base for Conveyor (P4.2).

SQLite-based knowledge base with FTS5 for fast full-text search.
Indexes files from allowed roots, supports hybrid synthesis.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from config import Settings
from personal_tools.base import ToolResult
from redaction import redact_text, truncate

logger = logging.getLogger(__name__)


def _get_kb_root(settings: Settings) -> Path:
    """Get KB root directory."""
    return Path(settings.kb_root or str(settings.codex_memory_root / "kb")).expanduser().resolve()


def _get_index_path(settings: Settings) -> Path:
    """Get KB index database path."""
    return Path(settings.kb_index_path or str(settings.codex_memory_root / "kb_index.sqlite")).expanduser().resolve()


def _get_allowed_roots(settings: Settings) -> list[Path]:
    """Get list of allowed search roots."""
    from personal_tools.file_search import _get_allowed_roots
    return _get_allowed_roots(settings)


def _init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize SQLite database with FTS5 if available."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    
    # Create indexed_files table first
    conn.execute("""
        CREATE TABLE IF NOT EXISTS indexed_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            root TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            sha256 TEXT NOT NULL,
            ext TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    
    # Try to create FTS5 table for file_chunks
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS file_chunks USING fts5(
                file_path,
                chunk_index,
                text
            )
        """)
        use_fts5 = True
    except sqlite3.OperationalError:
        # FTS5 not available, fall back to regular table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL
            )
        """)
        use_fts5 = False
    
    conn.commit()
    return conn


def _compute_sha256(path: Path) -> str:
    """Compute SHA256 hash of file."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _chunk_text(text: str, chunk_size: int = 1000) -> list[str]:
    """Split text into chunks for indexing."""
    lines = text.splitlines()
    chunks = []
    current = []
    current_len = 0
    
    for line in lines:
        if current_len + len(line) > chunk_size and current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    
    if current:
        chunks.append("\n".join(current))
    
    return chunks


def index_files(
    settings: Settings,
    *,
    operator_id: str = "",
) -> ToolResult:
    """Index files from allowed roots."""
    if not settings.file_search_enabled:
        return ToolResult(ok=False, text="⚠️ 文件搜索已禁用")
    
    from personal_tools.file_search import (
        _is_blocked_pattern,
        _is_binary_file,
        _BINARY_EXTENSIONS,
    )
    
    db_path = _get_index_path(settings)
    allowed_roots = _get_allowed_roots(settings)
    extensions = set(settings.file_search_extensions.split(","))
    max_bytes = settings.file_search_max_file_bytes
    
    conn = _init_db(db_path)
    indexed = 0
    skipped = 0
    errors = 0
    
    for root in allowed_roots:
        if not root.exists():
            continue
        
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            
            # Check extension
            if path.suffix not in extensions:
                continue
            
            # Check blocked patterns
            rel_path = str(path.relative_to(root))
            if _is_blocked_pattern(rel_path):
                continue
            
            # Check binary
            if _is_binary_file(path):
                continue
            
            # Check size
            try:
                size = path.stat().st_size
                mtime = path.stat().st_mtime
            except OSError:
                continue
            
            if size > max_bytes:
                continue
            
            # Compute hash
            sha256 = _compute_sha256(path)
            if not sha256:
                errors += 1
                continue
            
            # Check if already indexed with same hash
            row = conn.execute(
                "SELECT sha256 FROM indexed_files WHERE path = ?",
                (str(path),)
            ).fetchone()
            
            if row and row[0] == sha256:
                skipped += 1
                continue
            
            # Read and index file
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                errors += 1
                continue
            
            # Update indexed_files
            conn.execute("""
                INSERT OR REPLACE INTO indexed_files (path, root, size, mtime, sha256, ext, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (str(path), str(root), size, mtime, sha256, path.suffix, mtime))
            
            file_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            
            # Delete old chunks
            conn.execute("DELETE FROM file_chunks WHERE file_path = ?", (str(path),))
            
            # Insert new chunks
            chunks = _chunk_text(content)
            for i, chunk in enumerate(chunks):
                conn.execute(
                    "INSERT INTO file_chunks (file_path, chunk_index, text) VALUES (?, ?, ?)",
                    (str(path), i, chunk)
                )
            
            indexed += 1
    
    conn.commit()
    conn.close()
    
    return ToolResult(
        ok=True,
        text=f"索引完成: 新增/更新 {indexed} 个文件, 跳过 {skipped} 个, 错误 {errors} 个"
    )


def kb_status(
    settings: Settings,
    *,
    operator_id: str = "",
) -> ToolResult:
    """Show KB index status."""
    db_path = _get_index_path(settings)
    
    if not db_path.exists():
        return ToolResult(ok=True, text="知识库索引不存在。使用 /kb_index 创建索引。")
    
    conn = sqlite3.connect(str(db_path))
    
    file_count = conn.execute("SELECT COUNT(*) FROM indexed_files").fetchone()[0]
    chunk_count = conn.execute("SELECT COUNT(*) FROM file_chunks").fetchone()[0]
    total_size = conn.execute("SELECT SUM(size) FROM indexed_files").fetchone()[0] or 0
    
    # Get extension distribution
    ext_rows = conn.execute(
        "SELECT ext, COUNT(*) FROM indexed_files GROUP BY ext ORDER BY COUNT(*) DESC LIMIT 10"
    ).fetchall()
    
    conn.close()
    
    lines = [
        "📚 知识库状态",
        f"  索引文件数: {file_count}",
        f"  文本块数: {chunk_count}",
        f"  总大小: {total_size / 1024:.1f} KB",
        "",
        "文件类型分布:",
    ]
    for ext, count in ext_rows:
        lines.append(f"  {ext}: {count}")
    
    return ToolResult(ok=True, text="\n".join(lines))


def search_kb(
    settings: Settings,
    query: str,
    *,
    operator_id: str = "",
) -> ToolResult:
    """Search knowledge base."""
    if not settings.file_search_enabled:
        return ToolResult(ok=False, text="⚠️ 文件搜索已禁用")
    
    query = query.strip()
    if not query:
        return ToolResult(ok=False, text="⚠️ 用法: /kb_search <查询词>")
    
    db_path = _get_index_path(settings)
    if not db_path.exists():
        return ToolResult(ok=False, text="⚠️ 知识库索引不存在。使用 /kb_index 创建索引。")
    
    conn = sqlite3.connect(str(db_path))
    max_results = settings.file_search_max_results
    
    # Try FTS5 search first
    try:
        rows = conn.execute("""
            SELECT file_path, chunk_index, text, rank
            FROM file_chunks
            WHERE file_chunks MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, max_results)).fetchall()
    except sqlite3.OperationalError:
        # FTS5 not available, fall back to LIKE
        rows = conn.execute("""
            SELECT file_path, chunk_index, text, 0
            FROM file_chunks
            WHERE text LIKE ?
            LIMIT ?
        """, (f"%{query}%", max_results)).fetchall()
    
    conn.close()
    
    if not rows:
        return ToolResult(ok=True, text=f"知识库中未找到匹配 '{query}' 的内容")
    
    lines = [f"知识库搜索结果 ({len(rows)} 条匹配):", ""]
    for file_path, chunk_index, text, rank in rows:
        rel_path = Path(file_path).name
        lines.append(f"📄 {rel_path} (chunk {chunk_index})")
        # Show first few lines of matching chunk
        preview = "\n".join(text.splitlines()[:5])
        lines.append(f"  {preview[:200]}")
        lines.append("")
    
    return ToolResult(ok=True, text=truncate(redact_text("\n".join(lines))))


def collect_kb_facts(
    settings: Settings,
    operator_id: str,
    query: str,
) -> str:
    """Collect KB facts for hybrid synthesis."""
    if not settings.file_search_enabled:
        return ""
    
    query = query.strip()
    if not query:
        return ""
    
    db_path = _get_index_path(settings)
    if not db_path.exists():
        return ""
    
    conn = sqlite3.connect(str(db_path))
    
    # Try FTS5 search first
    try:
        rows = conn.execute("""
            SELECT file_path, chunk_index, text
            FROM file_chunks
            WHERE file_chunks MATCH ?
            ORDER BY rank
            LIMIT 5
        """, (query,)).fetchall()
    except sqlite3.OperationalError:
        # FTS5 not available, fall back to LIKE
        rows = conn.execute("""
            SELECT file_path, chunk_index, text
            FROM file_chunks
            WHERE text LIKE ?
            LIMIT 5
        """, (f"%{query}%",)).fetchall()
    
    conn.close()
    
    if not rows:
        return ""
    
    evidence = []
    for file_path, chunk_index, text in rows:
        rel_path = Path(file_path).name
        evidence.append(f"## {rel_path} (chunk {chunk_index})\n{text[:2000]}")
    
    return "\n\n".join(evidence)


# --- Adapters for personal_tools/registry.py ---

async def kb_index_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return index_files(settings, operator_id=operator_id)


async def kb_status_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return kb_status(settings, operator_id=operator_id)


async def kb_search_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    operator_id = kw.get("operator_id", "")
    return search_kb(settings, arg, operator_id=operator_id)
