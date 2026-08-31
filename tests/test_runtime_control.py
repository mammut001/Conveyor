from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from runtime_control import COMMAND_CANCEL, RuntimeControl, watch_runtime_commands


class FakeRunner:
    def __init__(self, job_id: str) -> None:
        self.current_job = SimpleNamespace(external_id=job_id)
        self.cancelled = False

    async def cancel(self) -> str:
        self.cancelled = True
        self.current_job = None
        return "cancelled"


class FailingRunner(FakeRunner):
    async def cancel(self) -> str:
        raise RuntimeError("cancel failed")


class RuntimeControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "state" / "job_queue.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """CREATE TABLE queued_jobs (
                    id TEXT PRIMARY KEY,
                    metadata_json TEXT,
                    updated_at TEXT
                )"""
            )
            conn.execute(
                "INSERT INTO queued_jobs (id, metadata_json, updated_at) VALUES ('q1', '{}', '')"
            )
            conn.commit()
        finally:
            conn.close()
        self.control = RuntimeControl(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_bind_owner_uses_existing_job_metadata(self) -> None:
        self.assertTrue(self.control.bind_job_owner("q1", "telegram:host:1"))
        self.assertEqual(self.control.owner_for_job("q1"), "telegram:host:1")
        conn = sqlite3.connect(self.db_path)
        try:
            raw = conn.execute("SELECT metadata_json FROM queued_jobs WHERE id = 'q1'").fetchone()[0]
        finally:
            conn.close()
        metadata = json.loads(raw)
        self.assertEqual(metadata["execution_owner_id"], "telegram:host:1")
        self.assertIn("execution_owner_bound_at", metadata)

    def test_missing_job_has_no_owner_and_cannot_receive_command(self) -> None:
        self.assertIsNone(self.control.owner_for_job("missing"))
        with self.assertRaises(ValueError):
            self.control.submit("missing", "", COMMAND_CANCEL)

    def test_duplicate_pending_cancel_is_collapsed(self) -> None:
        first = self.control.submit("q1", "owner", COMMAND_CANCEL)
        second = self.control.submit("q1", "owner", COMMAND_CANCEL)
        self.assertEqual(first.id, second.id)

    def test_concurrent_duplicate_cancel_is_collapsed_atomically(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            commands = list(pool.map(
                lambda _index: self.control.submit("q1", "owner", COMMAND_CANCEL),
                range(24),
            ))
        self.assertEqual(len({command.id for command in commands}), 1)

    def test_claim_is_owner_and_job_scoped(self) -> None:
        self.control.submit("q1", "owner-a", COMMAND_CANCEL)
        self.assertIsNone(self.control.claim_next("owner-b", "q1"))
        command = self.control.claim_next("owner-a", "q1")
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command.status, "claimed")
        self.assertIsNone(self.control.claim_next("owner-a", "q1"))

    def test_stale_owner_cannot_claim_old_command(self) -> None:
        self.control.submit("q1", "old-owner", COMMAND_CANCEL)
        self.assertIsNone(self.control.claim_next("new-owner", "q1"))

    def test_claim_is_atomic_across_consumers(self) -> None:
        self.control.submit("q1", "owner", COMMAND_CANCEL)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            claimed = list(pool.map(
                lambda _index: self.control.claim_next("owner", "q1"),
                range(16),
            ))
        self.assertEqual(sum(command is not None for command in claimed), 1)

    def test_watcher_delivers_cancel_to_live_owner(self) -> None:
        settings = SimpleNamespace(codex_memory_root=self.root)
        runner = FakeRunner("q1")
        self.control.submit("q1", "owner", COMMAND_CANCEL)
        asyncio.run(
            watch_runtime_commands(
                settings,
                runner,
                job_id="q1",
                owner_id="owner",
                poll_seconds=0.01,
            )
        )
        self.assertTrue(runner.cancelled)
        conn = sqlite3.connect(self.db_path)
        try:
            status = conn.execute("SELECT status FROM runtime_commands LIMIT 1").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(status, "completed")

    def test_watcher_records_failed_cancel(self) -> None:
        settings = SimpleNamespace(codex_memory_root=self.root)
        runner = FailingRunner("q1")
        command = self.control.submit("q1", "owner", COMMAND_CANCEL)
        asyncio.run(watch_runtime_commands(
            settings, runner, job_id="q1", owner_id="owner", poll_seconds=0.01,
        ))
        result = self.control.get(command.id)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "failed")
        self.assertIn("cancel failed", result.result)

    def test_prune_removes_stale_commands_in_every_status(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                """INSERT INTO runtime_commands
                   (id, job_id, owner_id, kind, status, created_at, claimed_at, completed_at)
                   VALUES (?, 'q1', 'owner', 'cancel', ?, ?, ?, ?)""",
                [
                    ("pending-old", "pending", old, None, None),
                    ("claimed-old", "claimed", old, old, None),
                    ("completed-old", "completed", old, old, old),
                    ("failed-old", "failed", old, old, old),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self.control.prune(86_400), 4)


if __name__ == "__main__":
    unittest.main()
