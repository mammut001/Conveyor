"""Switch a shared SQLite database to WAL mode.

bot, feishu_bot and web share ``state/job_queue.sqlite3``. In the default
rollback-journal mode a writer in one process blocks readers in the others,
for up to the 10 s busy timeout, and those reads often run on an event loop.
WAL lets readers proceed while one process writes. The mode is stored in the
database file, so setting it once is enough; repeating it is cheap.
"""
from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)


def enable_wal(conn: sqlite3.Connection, *, wait_seconds: float = 10.0) -> bool:
    """Set ``journal_mode=WAL``; True when the database is now in WAL mode.

    Changing the mode does not wait on the busy handler, so a concurrent
    writer makes it fail at once with "database is locked": retry briefly.
    Failure is logged and not raised; the database keeps working as before.
    """
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            return bool(row) and str(row[0]).lower() == "wal"
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                logger.warning("Could not enable WAL mode: %s", exc)
                return False
            time.sleep(0.05)
