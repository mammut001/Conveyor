"""GET /api/sessions/<id> returns only the newest messages of a long session."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from conveyor.core.transcript_store import TranscriptStore


class SessionMessageLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = TranscriptStore(Path(tmp.name) / "t.sqlite3")
        for i in range(12):
            self.store.append(session_id="s1", role="user" if i % 2 == 0 else "assistant",
                              content=f"m{i}", channel="web", operator_id="web-console", source_chat_id="c1")

    def test_without_limit_every_message_is_returned(self) -> None:
        session = self.store.get_session("s1")
        self.assertEqual([m["content"] for m in session["messages"]], [f"m{i}" for i in range(12)])
        self.assertNotIn("messages_truncated", session)

    def test_limit_keeps_the_newest_in_order_and_reports_the_total(self) -> None:
        session = self.store.get_session("s1", message_limit=5)
        self.assertEqual([m["content"] for m in session["messages"]], ["m7", "m8", "m9", "m10", "m11"])
        self.assertEqual(session["messages_total"], 12)
        self.assertTrue(session["messages_truncated"])

    def test_limit_larger_than_the_session_is_not_truncated(self) -> None:
        session = self.store.get_session("s1", message_limit=50)
        self.assertEqual(len(session["messages"]), 12)
        self.assertFalse(session["messages_truncated"])


if __name__ == "__main__":
    unittest.main()
