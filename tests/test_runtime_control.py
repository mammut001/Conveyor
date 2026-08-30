from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
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

    def test_duplicate_pending_cancel_is_collapsed(self) -> None:
        first = self.control.submit("q1", "owner", COMMAND_CANCEL)
        second = self.control.submit("q1", "owner", COMMAND_CANCEL)
        self.assertEqual(first.id, second.id)

    def test_claim_is_owner_and_job_scoped(self) -> None:
        self.control.submit("q1", "owner-a", COMMAND_CANCEL)
        self.assertIsNone(self.control.claim_next("owner-b", "q1"))
        command = self.control.claim_next("owner-a", "q1")
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command.status, "claimed")
        self.assertIsNone(self.control.claim_next("owner-a", "q1"))

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


if __name__ == "__main__":
    unittest.main()
