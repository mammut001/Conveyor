from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from transcript_store import TranscriptStore


class TranscriptStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.sqlite3"
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("CREATE TABLE queued_jobs (id TEXT PRIMARY KEY, chat_id TEXT)")
            conn.execute("INSERT INTO queued_jobs (id, chat_id) VALUES ('q1', 's1')")
            conn.commit()
        finally:
            conn.close()
        self.store = TranscriptStore(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_append_turn_creates_session_and_ordered_messages(self) -> None:
        self.store.append_turn(
            "s1", "hello", "world", job_id="q1", channel="web", operator_id="web-console"
        )
        session = self.store.get_session("s1")
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual([item["role"] for item in session["messages"]], ["user", "assistant"])
        self.assertEqual([item["content"] for item in session["messages"]], ["hello", "world"])

    def test_content_is_bounded(self) -> None:
        message = self.store.append("s1", "user", "x" * 20000)
        self.assertLessEqual(len(message.content), 16000)

    def test_list_sessions_exposes_message_and_job_counts(self) -> None:
        self.store.append("s1", "user", "hello")
        sessions = self.store.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["message_count"], 1)
        self.assertEqual(sessions[0]["job_count"], 1)


if __name__ == "__main__":
    unittest.main()
