"""setup_wizard/envfile.py — read and update `.env` safely.

Updates keep unrelated lines and comments, replace managed keys in place,
append new keys at the end, back up the previous file and write atomically
with mode 600.
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path

MAX_BACKUPS = 5
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_./:@,+\-=]*$")


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def format_value(value: str) -> str:
    """Quote values that dotenv would otherwise misread (spaces, #, quotes)."""
    if _SAFE_VALUE.match(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class EnvFile:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        return parse(self.path.read_text(encoding="utf-8"))

    def render(self, updates: dict[str, str], removals: set[str] | None = None) -> str:
        removals = removals or set()
        lines = self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []
        out: list[str] = []
        done: set[str] = set()
        for raw in lines:
            stripped = raw.strip()
            body = stripped[len("export "):] if stripped.startswith("export ") else stripped
            if body and not body.startswith("#") and "=" in body:
                key = body.split("=", 1)[0].strip()
                if key in removals:
                    continue
                if key in updates:
                    if key not in done:
                        out.append(f"{key}={format_value(updates[key])}")
                        done.add(key)
                    continue
            out.append(raw)
        pending = [k for k in updates if k not in done]
        if pending:
            if out and out[-1].strip():
                out.append("")
            out.append(f"# --- conveyor setup {time.strftime('%Y-%m-%d')} ---")
            out.extend(f"{k}={format_value(updates[k])}" for k in pending)
        return "\n".join(out).rstrip("\n") + "\n"

    def backup(self) -> Path | None:
        if not self.path.exists():
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.name}.bak-{stamp}")
        n = 1
        while target.exists():
            n += 1
            target = self.path.with_name(f"{self.path.name}.bak-{stamp}-{n}")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(self.path.read_bytes())
        backups = sorted(self.path.parent.glob(f"{self.path.name}.bak-*"))
        for old in backups[:-MAX_BACKUPS]:
            try:
                old.unlink()
            except OSError:
                pass
        return target

    def write(self, updates: dict[str, str], removals: set[str] | None = None) -> Path | None:
        """Apply updates; returns the backup path (None for a new file)."""
        content = self.render(updates, removals)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        backup = self.backup()
        fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(self.path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.chmod(self.path, 0o600)
        return backup
