from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from human_takeover import HumanTakeoverStore, takeover_blocks_automation


class HumanTakeoverStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(codex_memory_root=Path(self.temp.name))
        self.store = HumanTakeoverStore(self.settings)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_full_state_machine_and_public_payload_are_secret_free(self) -> None:
        created = self.store.start(
            reason="payment",
            task_id="task-123",
            requested_by="web-console",
            ttl_seconds=300,
        )
        self.assertEqual(created["state"], "waiting_for_human")
        self.assertTrue(takeover_blocks_automation(self.settings))

        public = HumanTakeoverStore.public(created)
        self.assertEqual(public["reason"], "payment")
        self.assertEqual(public["task_id"], "task-123")
        self.assertNotIn("password", public)
        self.assertNotIn("text", public)
        self.assertNotIn("screenshot", public)

        active = self.store.activate(created["id"])
        self.assertIsNotNone(active)
        self.assertEqual(active["state"], "human_active")

        completed = self.store.complete(created["id"])
        self.assertIsNotNone(completed)
        self.assertEqual(completed["state"], "completed")
        self.assertFalse(takeover_blocks_automation(self.settings))

    def test_only_one_open_takeover_is_allowed(self) -> None:
        self.store.start(reason="captcha", ttl_seconds=300)
        with self.assertRaisesRegex(RuntimeError, "already open"):
            self.store.start(reason="login", ttl_seconds=300)

    def test_cancel_releases_exclusive_lease(self) -> None:
        first = self.store.start(reason="login", ttl_seconds=300)
        cancelled = self.store.cancel(first["id"])
        self.assertEqual(cancelled["state"], "cancelled")
        second = self.store.start(reason="payment", ttl_seconds=300)
        self.assertEqual(second["state"], "waiting_for_human")

    def test_expired_takeover_is_closed_automatically(self) -> None:
        created = self.store.start(reason="operator_requested", ttl_seconds=30)
        conn = sqlite3.connect(str(self.store.path))
        try:
            with conn:
                conn.execute(
                    "UPDATE human_takeovers SET expires_at = 0 WHERE id = ?",
                    (created["id"],),
                )
        finally:
            conn.close()
        self.assertIsNone(self.store.current())
        expired = self.store.get(created["id"])
        self.assertEqual(expired["state"], "expired")
        self.assertEqual(expired["close_reason"], "ttl_expired")

    def test_invalid_reason_and_ttl_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            self.store.start(reason="credit_card_number", ttl_seconds=300)
        with self.assertRaises(ValueError):
            self.store.start(reason="payment", ttl_seconds=5)
        with self.assertRaises(ValueError):
            self.store.start(reason="payment", ttl_seconds=3600)

    def test_invalid_transitions_do_not_reopen_terminal_session(self) -> None:
        created = self.store.start(reason="payment", ttl_seconds=300)
        self.store.complete(created["id"])
        self.assertIsNone(self.store.activate(created["id"]))
        self.assertIsNone(self.store.cancel(created["id"]))


class HumanTakeoverScriptTests(unittest.TestCase):
    def test_novnc_helper_has_valid_bash_syntax(self) -> None:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash unavailable")
        root = Path(__file__).resolve().parents[1]
        subprocess.run(
            [bash, "-n", str(root / "scripts" / "novnc_handoff.sh")],
            check=True,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
