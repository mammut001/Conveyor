#!/usr/bin/env python3
"""deploy_db.py — queue database helper for deploy_vps.sh.

The control-plane database lives under the *service* user's CODEX_MEMORY_ROOT
(default ~/.codex, mode 700). deploy_vps.sh runs as a separate deploy user, so
it invokes this helper as the service user:

    deploy_db.py path             print the resolved job_queue.sqlite3 path
    deploy_db.py idle             print "<queued> <running>"
    deploy_db.py backup           stream an integrity-checked online backup to stdout

Stdlib only, so it also works before the virtualenv is synced.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env_value(key: str) -> str | None:
    if os.getenv(key):
        return os.getenv(key)
    env_file = ROOT / ".env"
    try:
        lines = env_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for raw in lines:
        line = raw.strip()
        if line.startswith(f"{key}="):
            return line[len(key) + 1:].strip().strip("'\"") or None
    return None


def db_path() -> Path:
    root = Path(_env_value("CODEX_MEMORY_ROOT") or "~/.codex").expanduser().resolve()
    return root / "state" / "job_queue.sqlite3"


def queue_counts(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='queued_jobs'"
        ).fetchone()
        if not exists:
            return 0, 0
        queued = conn.execute("SELECT COUNT(*) FROM queued_jobs WHERE state='queued'").fetchone()[0]
        running = conn.execute("SELECT COUNT(*) FROM queued_jobs WHERE state='running'").fetchone()[0]
        return int(queued), int(running)
    finally:
        conn.close()


def stream_backup(path: Path) -> None:
    if not path.exists():
        return
    with tempfile.TemporaryDirectory(prefix="conveyor-db-backup-") as tmp:
        target = Path(tmp) / "job_queue.sqlite3"
        src = sqlite3.connect(str(path), timeout=10)
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise SystemExit(f"backup integrity_check failed: {result}")
        finally:
            dst.close()
            src.close()
        sys.stdout.buffer.write(target.read_bytes())
        sys.stdout.buffer.flush()


def deploy_fence(path: Path, token: str, *, release: bool = False) -> None:
    """Atomically stop dequeue at the database boundary while the repo updates.

    The fence is owned by a random deploy token, not by a mutable paused flag.
    A second deploy cannot steal it or unfreeze someone else\'s deployment.
    """
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        raise SystemExit("invalid deploy fence token")
    if not path.exists():
        raise SystemExit("queue DB is missing; cannot guarantee deployment drain")
    conn = sqlite3.connect(str(path), timeout=15)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT value FROM queue_metadata WHERE key = \'deploy_fence\'"
            ).fetchone()
            if release:
                if existing is None or existing[0] != token:
                    raise RuntimeError("deploy fence token mismatch or already released")
                conn.execute(
                    "DELETE FROM queue_metadata WHERE key = \'deploy_fence\' AND value = ?", (token,)
                )
            else:
                if existing is not None:
                    raise RuntimeError("another deployment holds the queue fence")
                queued, running = (
                    int(conn.execute("SELECT COUNT(*) FROM queued_jobs WHERE state=?", (state,)).fetchone()[0])
                    for state in ("queued", "running")
                )
                if queued or running:
                    raise RuntimeError(f"queue is not idle (queued={queued}, running={running})")
                conn.execute(
                    "INSERT INTO queue_metadata (key, value) VALUES (\'deploy_fence\', ?)", (token,)
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    print("thawed" if release else "fenced")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    path = db_path()
    if command == "path":
        print(path)
    elif command == "idle":
        queued, running = queue_counts(path)
        print(f"{queued} {running}")
    elif command == "backup":
        stream_backup(path)
    elif command in ("freeze", "thaw"):
        if len(sys.argv) != 3:
            raise SystemExit("freeze/thaw requires a token")
        deploy_fence(path, sys.argv[2], release=(command == "thaw"))
    else:
        raise SystemExit("usage: deploy_db.py path|idle|backup|freeze TOKEN|thaw TOKEN")


if __name__ == "__main__":
    main()
