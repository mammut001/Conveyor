from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from handlers.job_queue import JobQueue
from web_control import WebControl


class WebControlSecurityTests(unittest.TestCase):
    def test_expired_approval_cannot_execute(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = SimpleNamespace(codex_memory_root=Path(temp_dir))
            queue = JobQueue()
            queue.configure(settings, SimpleNamespace(current_job=None), recover=False)
            runner = SimpleNamespace(current_job=None)
            control = WebControl(settings, runner, queue)
            connection = control._connect()
            try:
                with connection:
                    connection.execute(
                        """INSERT INTO web_approvals
                           (id, job_id, action, status, created_at, expires_at)
                           VALUES ('expired', 'q1', 'apply', 'pending', 'now', ?)""",
                        (time.time() - 1,),
                    )
            finally:
                connection.close()

            result = asyncio.run(control.decide_approval("expired", True))
            self.assertEqual(result, {"id": "expired", "status": "expired"})

    def test_artifact_identifier_rejects_path_syntax(self):
        control = object.__new__(WebControl)
        self.assertIsNone(control.artifact_path("../secret.png"))
        self.assertIsNone(control.artifact_path("nested/file"))


if __name__ == "__main__":
    unittest.main()
