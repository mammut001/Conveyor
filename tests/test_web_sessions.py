from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from handlers.job_queue import JobQueue
from transcript_store import get_transcript_store, session_identity
from web_control import WebControl


class WebSessionTests(unittest.TestCase):
    def test_namespaced_transcript_maps_to_source_queue_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = SimpleNamespace(
                codex_memory_root=root,
                codex_task_root=root / "tasks",
                conveyor_max_pending_jobs=10,
                conveyor_event_retention_per_job=100,
            )
            runner = SimpleNamespace(current_job=None)
            queue = JobQueue()
            queue.configure(settings, runner, recover=False)
            self.assertEqual(
                queue._db_path(),
                root / "state" / "job_queue.sqlite3",
            )
            now = datetime.now(timezone.utc).isoformat()
            connection = queue._get_conn()
            try:
                with connection:
                    connection.execute(
                        """INSERT INTO queued_jobs
                           (id, operator_id, channel, chat_id, mode, prompt, state,
                            created_at, updated_at, position, metadata_json)
                           VALUES ('q1', 'operator', 'telegram', 'same', 'run', 'hello',
                                   'completed', ?, ?, 0, '{}')""",
                        (now, now),
                    )
            finally:
                connection.close()

            durable_id = session_identity("telegram", "same", "operator")
            get_transcript_store(settings).append_turn(
                durable_id, "hello", "world", channel="telegram",
                operator_id="operator", source_chat_id="same",
            )
            control = WebControl(settings, runner, queue)

            sessions = control.list_sessions()
            self.assertEqual(sessions[0]["id"], durable_id)
            self.assertEqual(sessions[0]["latest_job"]["id"], "q1")
            detail = control.get_session(durable_id)
            self.assertEqual([job["id"] for job in detail["jobs"]], ["q1"])
            self.assertEqual(
                control.resolve_session_identity(durable_id),
                ("telegram", "operator", "same"),
            )

    def test_orphan_queued_job_is_projected_without_transcript(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            settings = SimpleNamespace(
                codex_memory_root=root,
                codex_task_root=root / "tasks",
                conveyor_max_pending_jobs=10,
                conveyor_event_retention_per_job=100,
            )
            runner = SimpleNamespace(current_job=None)
            queue = JobQueue()
            queue.configure(settings, runner, recover=False)
            store = get_transcript_store(settings)
            existing_id = session_identity("web", "chat-existing", "operator")
            store.ensure_session(
                existing_id, channel="web", operator_id="operator",
                source_chat_id="chat-existing", title="Existing chat",
            )
            now = datetime.now(timezone.utc).isoformat()
            connection = queue._get_conn()
            try:
                with connection:
                    connection.execute(
                        """INSERT INTO queued_jobs
                           (id, operator_id, channel, chat_id, mode, prompt, state,
                            created_at, updated_at, position, metadata_json)
                           VALUES ('queued-web', 'operator', 'web', 'chat-new', 'run',
                                   'queued prompt', 'queued', ?, ?, 1, '{}')""",
                        (now, now),
                    )
            finally:
                connection.close()

            control = WebControl(settings, runner, queue)
            sessions = control.list_sessions()
            orphan_id = session_identity("web", "chat-new", "operator")
            orphan = next(item for item in sessions if item["id"] == orphan_id)
            self.assertEqual(orphan["latest_job"]["id"], "queued-web")
            self.assertEqual(orphan["latest_job"]["state"], "queued")
            detail = control.get_session(orphan_id)
            self.assertIsNotNone(detail)
            self.assertEqual([job["id"] for job in detail["jobs"]], ["queued-web"])


if __name__ == "__main__":
    unittest.main()
