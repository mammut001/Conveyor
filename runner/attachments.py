"""runner/attachments.py — private image store for agent jobs.

Images sent to the bot are saved under ``<codex_task_root>/attachments``
with random names and 0600 permissions. A job prompt references them with
leading header lines::

    [image: 3f2a…9c.jpg]

The runner turns those headers into ``codex exec --image`` arguments (and
an ``--add-dir`` for the Claude Code backend). Only header lines at the very
top of the prompt count, names must match the random-name pattern, and the
file must resolve inside the attachments directory, so text quoted into a
prompt can never attach an arbitrary file.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES_PER_JOB = 4
RETENTION_SECONDS = 7 * 24 * 3600

_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(?:jpg|png|gif|webp)$")
_HEADER_RE = re.compile(r"^\[image: ([^\]\s]+)\]$")


def attachments_root(task_root: Path) -> Path:
    return Path(task_root) / "attachments"


def sniff_image_ext(data: bytes) -> str | None:
    """File extension for supported image bytes, else None."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def save_image(task_root: Path, data: bytes) -> str | None:
    """Store image bytes; return the stored name, or None if rejected."""
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    ext = sniff_image_ext(data)
    if ext is None:
        return None
    root = attachments_root(task_root)
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    name = f"{uuid.uuid4().hex}.{ext}"
    path = root / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return name


def sweep(task_root: Path, *, now: float | None = None) -> int:
    """Delete stored images older than the retention window."""
    root = attachments_root(task_root)
    if not root.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - RETENTION_SECONDS
    removed = 0
    for path in root.iterdir():
        try:
            if _NAME_RE.match(path.name) and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def header(names: list[str] | tuple[str, ...]) -> str:
    """Prompt header lines referencing stored images."""
    return "\n".join(f"[image: {n}]" for n in names)


def prompt_images(task_root: Path, prompt: str) -> list[Path]:
    """Stored images referenced by the prompt's leading header lines."""
    root = attachments_root(task_root).resolve()
    found: list[Path] = []
    for line in (prompt or "").lstrip().splitlines():
        m = _HEADER_RE.match(line.strip())
        if not m:
            break
        name = m.group(1)
        if not _NAME_RE.match(name):
            continue
        path = (root / name).resolve()
        if path.parent == root and path.is_file() and path not in found:
            found.append(path)
        if len(found) >= MAX_IMAGES_PER_JOB:
            break
    return found
