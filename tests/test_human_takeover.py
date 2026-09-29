from __future__ import annotations

import asyncio
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from human_takeover import HumanTakeoverStore, takeover_blocks_automation


def _computer_settings(root: Path):
    from config import Settings

    return Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_id=1,
        codex_workspace_root=root,
        codex_bin="codex",
        codex_task_root=root / "tasks",
        codex_model=None,
        codex_timeout_seconds=30,
        codex_retry_429_delays_seconds=(),
        telegram_progress_seconds=1,
        codex_memory_root=root,
        user_timezone="UTC",
        conveyor_computer_use_enabled=True,
        conveyor_computer_direct_enabled=True,
        conveyor_computer_backend="fake",
        conveyor_computer_allowed_actions=("observe", "click", "type", "hotkey", "scroll", "wait"),
        conveyor_computer_blocked_keywords=("password", "payment"),
    )


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

    def test_takeover_blocks_claims_and_discards_queued_actions(self) -> None:
        from desktop_computer_requests import (
            cancel_pending_computer_steps,
            claim_computer_step,
            create_computer_step,
            create_computer_task,
            get_computer_task,
        )
        from desktop_observe_requests import (
            cancel_pending_observe_requests,
            claim_observe_request,
            save_observe_requests,
        )

        settings = _computer_settings(Path(self.temp.name))
        created = create_computer_task(
            settings, "safe test action", direct_mode=True, max_steps=3, max_seconds=30,
        )
        step = create_computer_step(settings, created["task_id"], {"action": "observe"})
        save_observe_requests(settings, {
            "observe-test": {
                "request_id": "observe-test",
                "node_id": "test-node",
                "status": "pending",
            },
        })
        takeover = self.store.start(reason="operator_requested", ttl_seconds=300)

        blocked_step = create_computer_step(
            settings, created["task_id"], {"action": "click", "x": 10, "y": 10},
        )
        self.assertEqual(blocked_step["error"], "human_takeover_active")
        self.assertEqual(cancel_pending_computer_steps(settings), 1)
        self.assertEqual(cancel_pending_observe_requests(settings), 1)
        claim = claim_computer_step(settings, step["step_id"], "test-node")
        self.assertEqual(claim["error"], "human_takeover_active")
        observe_claim = claim_observe_request(settings, "observe-test", "test-node")
        self.assertEqual(observe_claim["error"], "human_takeover_active")
        stored = get_computer_task(settings, created["task_id"])
        self.assertEqual(stored["steps"][step["step_id"]]["status"], "cancelled")
        self.store.complete(takeover["id"])

    def test_resume_forces_fresh_observe_before_planner(self) -> None:
        from desktop_computer_loop import FakeComputerBackend, run_computer_loop
        from desktop_computer_planner import ScriptedPlanner

        settings = _computer_settings(Path(self.temp.name))
        store = HumanTakeoverStore(settings)
        takeover = store.start(reason="operator_requested", ttl_seconds=300)

        class RecordingBackend:
            def __init__(self) -> None:
                self.inner = FakeComputerBackend(settings)
                self.actions: list[str] = []

            async def execute_step(self, settings, task_id, step_id, action):
                self.actions.append(str(action.get("action")))
                return await self.inner.execute_step(settings, task_id, step_id, action)

        async def exercise():
            backend = RecordingBackend()
            task = asyncio.create_task(run_computer_loop(
                settings,
                "continue safely",
                planner=ScriptedPlanner([{"action": "done", "summary": "finished"}]),
                backend=backend,
                max_steps=4,
                max_seconds=30,
                direct_mode=True,
            ))
            await asyncio.sleep(0.3)
            self.assertEqual(backend.actions, [])
            self.assertTrue(takeover_blocks_automation(settings))
            store.complete(takeover["id"])
            result = await asyncio.wait_for(task, timeout=5)
            return backend.actions, result

        actions, result = asyncio.run(exercise())
        self.assertEqual(actions, ["observe"])
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["steps_used"], 1)


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

    def test_novnc_listeners_are_loopback_only_and_start_output_hides_password(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts" / "novnc_handoff.sh").read_text(encoding="utf-8")
        self.assertIn('LISTEN_HOST="127.0.0.1"', script)
        self.assertIn('"$LISTEN_HOST:$NOVNC_PORT"', script)
        self.assertIn('"$LISTEN_HOST:$VNC_PORT"', script)
        self.assertIn("    -localhost \\", script)
        self.assertNotIn("0.0.0.0", script)
        startup_output = script.split("cat <<EOF", 1)[1].split("EOF", 1)[0]
        self.assertNotIn("$password", startup_output)

    def test_handoffctl_refuses_resume_while_transport_is_live(self) -> None:
        import os
        from unittest import mock

        from scripts.handoffctl import _transport_running

        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "x11vnc.pid"
            pid_file.write_text(str(os.getpid()), encoding="ascii")
            with mock.patch.dict(os.environ, {"CONVEYOR_HANDOFF_STATE_DIR": directory}):
                self.assertTrue(_transport_running())
                pid_file.unlink()
                self.assertFalse(_transport_running())


if __name__ == "__main__":
    unittest.main()
