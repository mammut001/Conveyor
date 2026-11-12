from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from conveyor.core.transcript_store import TranscriptStore, session_identity


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

    def test_list_sessions_reports_the_last_message_kind(self) -> None:
        # A provider failure is stored with kind="error"; the 任务 / 对话 sidebar marks the
        # session 「（没有完成）」 from this field (QA r8 N30).
        self.store.append(
            "web:web-console:s1", "user", "question", channel="web",
            operator_id="web-console", source_chat_id="s1",
        )
        self.assertEqual(self.store.list_sessions()[0]["last_message_kind"], None)
        self.store.append(
            "web:web-console:s1", "assistant", "连不上模型服务", kind="error", channel="web",
            operator_id="web-console", source_chat_id="s1",
        )
        self.assertEqual(self.store.list_sessions()[0]["last_message_kind"], "error")
        # A later answer in the same session clears the mark.
        self.store.append(
            "web:web-console:s1", "assistant", "正常", kind="chat", channel="web",
            operator_id="web-console", source_chat_id="s1",
        )
        self.assertEqual(self.store.list_sessions()[0]["last_message_kind"], "chat")

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

    def test_archive_and_delete_session(self) -> None:
        self.store.append("s1", "user", "hi")
        self.assertEqual(len(self.store.list_sessions()), 1)
        self.assertTrue(self.store.archive_session("s1"))
        self.assertEqual(len(self.store.list_sessions()), 0)
        self.assertEqual(len(self.store.list_sessions(include_archived=True)), 1)
        self.assertTrue(self.store.delete_session("s1"))
        self.assertEqual(len(self.store.list_sessions(include_archived=True)), 0)

    def test_insert_session_if_absent_preserves_archived_and_activity(self) -> None:
        # Create initial session
        sess = self.store.insert_session_if_absent("s-poll", title="Original Title")
        self.assertEqual(sess["title"], "Original Title")
        self.assertEqual(sess["archived"], 0)
        initial_updated_at = sess["updated_at"]

        # Archive it
        self.assertTrue(self.store.archive_session("s-poll"))
        archived_sess = self.store.get_session("s-poll")
        assert archived_sess is not None
        self.assertEqual(archived_sess["archived"], 1)

        # Repeated insert_session_if_absent must NOT touch archived, title, or updated_at
        repeat_sess = self.store.insert_session_if_absent("s-poll", title="Should Not Overwrite")
        self.assertEqual(repeat_sess["archived"], 1)
        self.assertEqual(repeat_sess["title"], "Original Title")
        self.assertEqual(repeat_sess["updated_at"], initial_updated_at)

        # ensure_session with touch=False also preserves
        no_touch = self.store.ensure_session("s-poll", title="Another Title", touch=False)
        self.assertEqual(no_touch["archived"], 1)
        self.assertEqual(no_touch["title"], "Original Title")
        self.assertEqual(no_touch["updated_at"], initial_updated_at)

        # Actual append MUST reactivate and update
        msg = self.store.append("s-poll", "user", "Waking up session")
        reactivated = self.store.get_session("s-poll")
        assert reactivated is not None
        self.assertEqual(reactivated["archived"], 0)
        self.assertGreaterEqual(reactivated["updated_at"], msg.created_at)

    def test_recent_and_count_messages_kind_filtering(self) -> None:
        sid = "s-kinds"
        self.store.ensure_session(sid)

        # 2 discussion turns (kind=None and kind="chat")
        self.store.append(sid, "user", "turn 1", kind="chat")
        self.store.append(sid, "assistant", "turn 2")  # kind=None

        # 3 internal turns
        self.store.append(sid, "assistant", "tool req", kind="tool_approval")
        self.store.append(sid, "assistant", "tool res", kind="tool_result")
        self.store.append(sid, "assistant", "status ping", kind="status")

        # 1 more discussion turn
        self.store.append(sid, "assistant", "turn 3", kind="task_suggestion")

        # Unfiltered count
        self.assertEqual(self.store.count_messages(sid), 6)
        all_recent = self.store.recent_messages(sid, limit=10)
        self.assertEqual(len(all_recent), 6)

        # Filtered by kinds=(None, "chat", "task_suggestion")
        disc_kinds = (None, "chat", "task_suggestion")
        disc_count = self.store.count_messages(sid, kinds=disc_kinds)
        self.assertEqual(disc_count, 3)

        disc_recent = self.store.recent_messages(sid, limit=10, kinds=disc_kinds)
        self.assertEqual(len(disc_recent), 3)
        self.assertEqual([m["content"] for m in disc_recent], ["turn 1", "turn 2", "turn 3"])

        # Latest-N applied AFTER kind filtering (limit=2 returns the last 2 discussion turns)
        disc_last2 = self.store.recent_messages(sid, limit=2, kinds=disc_kinds)
        self.assertEqual(len(disc_last2), 2)
        self.assertEqual([m["content"] for m in disc_last2], ["turn 2", "turn 3"])

        # exclude_kinds test
        ex_count = self.store.count_messages(sid, exclude_kinds=("tool_approval", "tool_result", "status"))
        self.assertEqual(ex_count, 3)

    def test_task_suggestion_requirements_survive_history_refresh(self) -> None:
        from conveyor.web.web_chat import build_history

        goal = ("实现项目旅程修复。" * 280) + "LAST-GOAL-CONSTRAINT"
        criteria = ("验收必须覆盖刷新后的历史。" * 180) + "LAST-CRITERIA-CONSTRAINT"
        prompt = ("legacy prompt " * 200) + "LAST-PROMPT-CONSTRAINT"
        # Between the 1800 context bound and the generic 2000 cap, so a
        # forgotten context bound would keep the tail.
        context = ("讨论上下文。" * 310) + "CONTEXT-TAIL-PAST-BOUND"
        secret = "sk-" + ("a" * 24)
        goal_with_secret = (
            f"目标包含 {secret} 然后继续。" + ("补充约束。" * 500) + "LAST-SECRET-GOAL"
        )
        over_goal = ("Z" * 8200) + "OVER-GOAL-TAIL"
        extra = "E" * 2500 + "EXTRA-FIELD-TAIL"
        nested_goal = ("N" * 2500) + "NESTED-TAIL"
        self.assertGreater(len(goal), 2000)
        self.assertLess(len(goal), 8000)
        self.assertGreater(len(criteria), 2000)
        self.assertGreater(len(prompt), 2000)
        self.assertGreater(len(context), 1800)
        self.assertLess(len(context), 2000)
        self.assertGreater(len(goal_with_secret), 2000)

        sid = "sup-draft-persist"
        wide = {f"field{i}": "v" for i in range(33)}
        self.store.append(
            sid,
            "assistant",
            "draft reply",
            kind="task_suggestion",
            metadata={
                "prompt": prompt,
                "goal": goal,
                "acceptance_criteria": criteria,
                "source": "discussion",
                "context": context,
                "execution_authorized": False,
                "note": extra,
                "api_key": "should-not-leak-" + secret,
                "nested": {"goal": nested_goal},
                "goals": [goal],
                **wide,
            },
        )
        rows = self.store.recent_messages(sid)
        self.assertEqual(len(rows), 1)
        stored = rows[0]["metadata"]
        history = build_history({"id": sid, "messages": rows})
        suggestion = history[0]["task_suggestion"]
        self.assertEqual(suggestion["goal"], goal)
        self.assertEqual(suggestion["acceptance_criteria"], criteria)
        self.assertEqual(suggestion["prompt"], prompt)
        self.assertIn("LAST-GOAL-CONSTRAINT", suggestion["goal"])
        self.assertIn("LAST-CRITERIA-CONSTRAINT", suggestion["acceptance_criteria"])
        self.assertIn("LAST-PROMPT-CONSTRAINT", suggestion["prompt"])
        self.assertEqual(suggestion["execution_authorized"], False)
        self.assertLessEqual(len(suggestion["context"]), 1800)
        self.assertNotIn("CONTEXT-TAIL-PAST-BOUND", suggestion["context"])
        self.assertLessEqual(len(stored["note"]), 2000)
        self.assertNotIn("EXTRA-FIELD-TAIL", stored["note"])
        self.assertEqual(stored["api_key"], "[REDACTED]")
        self.assertNotIn(secret, str(stored["api_key"]))
        self.assertNotIn("NESTED-TAIL", stored["nested"]["goal"])
        self.assertLessEqual(len(stored["nested"]["goal"]), 2000)
        self.assertNotIn("LAST-GOAL-CONSTRAINT", stored["goals"][0])
        self.assertLessEqual(len(stored["goals"][0]), 2000)
        self.assertEqual(len(stored), 32)
        self.assertNotIn("field32", stored)
        self.assertIn("goal", stored)

        self.store.append(
            sid,
            "assistant",
            "secret draft",
            kind="task_suggestion",
            metadata={
                "prompt": "p",
                "goal": goal_with_secret,
                "acceptance_criteria": criteria,
            },
        )
        secret_row = next(
            m for m in self.store.recent_messages(sid) if m["content"] == "secret draft"
        )
        secret_history = build_history({"id": sid, "messages": [secret_row]})
        stored_goal = secret_history[0]["task_suggestion"]["goal"]
        stored_criteria = secret_history[0]["task_suggestion"]["acceptance_criteria"]
        self.assertNotIn(secret, stored_goal)
        self.assertNotIn(secret, str(secret_row["metadata"]))
        self.assertIn("[REDACTED]", stored_goal)
        self.assertIn("LAST-SECRET-GOAL", stored_goal)
        self.assertEqual(stored_criteria, criteria)

        self.store.append(
            sid,
            "assistant",
            "overlong draft",
            kind="task_suggestion",
            metadata={"prompt": "p", "goal": over_goal, "acceptance_criteria": "done"},
        )
        over_row = next(
            m for m in self.store.recent_messages(sid) if m["content"] == "overlong draft"
        )
        over_goal_stored = build_history({"id": sid, "messages": [over_row]})[0]["task_suggestion"]["goal"]
        self.assertLessEqual(len(over_goal_stored), 8000)
        self.assertNotIn("OVER-GOAL-TAIL", over_goal_stored)

        generic = "G" * 2500 + "GENERIC-TAIL"
        self.store.append(sid, "user", "note", kind="chat", metadata={"note": generic, "goal": goal})
        generic_meta = next(
            m for m in self.store.recent_messages(sid) if m["content"] == "note"
        )["metadata"]
        self.assertLessEqual(len(generic_meta["note"]), 2000)
        self.assertNotIn("GENERIC-TAIL", generic_meta["note"])
        self.assertLessEqual(len(generic_meta["goal"]), 2000)
        self.assertNotIn("LAST-GOAL-CONSTRAINT", generic_meta["goal"])
        self.store.append(sid, "user", "plain", metadata={"note": generic})
        plain_meta = next(
            m for m in self.store.recent_messages(sid) if m["content"] == "plain"
        )["metadata"]
        self.assertLessEqual(len(plain_meta["note"]), 2000)
        self.assertNotIn("GENERIC-TAIL", plain_meta["note"])


if __name__ == "__main__":
    unittest.main()
