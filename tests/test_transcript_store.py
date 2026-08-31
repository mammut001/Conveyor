from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from transcript_store import TranscriptStore, session_identity


class TranscriptStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.sqlite3"
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "CREATE TABLE queued_jobs (id TEXT PRIMARY KEY, chat_id TEXT, channel TEXT, operator_id TEXT)"
            )
            conn.execute(
                "INSERT INTO queued_jobs (id, chat_id, channel, operator_id) VALUES ('q1', 's1', 'web', 'web-console')"
            )
            conn.commit()
        finally:
            conn.close()
        self.store = TranscriptStore(self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_append_turn_creates_session_and_ordered_messages(self) -> None:
        self.store.append_turn(
            "web:web-console:s1", "hello", "world", job_id="q1", channel="web",
            operator_id="web-console", source_chat_id="s1",
        )
        session = self.store.get_session("web:web-console:s1")
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual([item["role"] for item in session["messages"]], ["user", "assistant"])
        self.assertEqual([item["content"] for item in session["messages"]], ["hello", "world"])
        self.assertEqual(session["source_chat_id"], "s1")

    def test_content_is_bounded(self) -> None:
        message = self.store.append("s1", "user", "x" * 20000)
        self.assertLessEqual(len(message.content), 16000)

    def test_list_sessions_exposes_message_and_job_counts(self) -> None:
        self.store.append(
            "web:web-console:s1", "user", "hello", channel="web",
            operator_id="web-console", source_chat_id="s1",
        )
        sessions = self.store.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["message_count"], 1)
        self.assertEqual(sessions[0]["job_count"], 1)

    def test_session_identity_is_channel_and_operator_namespaced(self) -> None:
        telegram = session_identity("telegram", "same", "operator")
        feishu = session_identity("feishu", "same", "operator")
        other_operator = session_identity("telegram", "same", "other")
        self.assertEqual(len({telegram, feishu, other_operator}), 3)

    def test_metadata_is_redacted_bounded_and_excludes_reasoning(self) -> None:
        message = self.store.append(
            "s1", "assistant", "safe", metadata={
                "api_key": "secret-value",
                "reasoning": "hidden chain",
                "label": "x" * 3000,
            },
        )
        self.assertEqual(message.metadata["api_key"], "[REDACTED]")
        self.assertEqual(message.metadata["reasoning"], "[REDACTED]")
        self.assertLessEqual(len(message.metadata["label"]), 2000)


if __name__ == "__main__":
    unittest.main()
