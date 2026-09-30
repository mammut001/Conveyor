from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import handoff_sidecar
from human_takeover import HumanTakeoverStore, takeover_blocks_automation
from web_takeover import (
    WebTakeover,
    allow_transport,
    read_close_request,
    read_sidecar_status,
    read_transport_gate,
    request_close,
)


class WebTakeoverTests(unittest.TestCase):
    def settings(self, root: str, enabled: bool = True):
        return SimpleNamespace(
            codex_memory_root=root,
            conveyor_computer_max_seconds=1,
            conveyor_takeover_enabled=enabled,
        )

    def test_web_start_opens_transport_gate_only_after_idle_barrier(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.settings(root)
            web = WebTakeover(settings)
            claimed = mock.Mock(side_effect=[True, False])
            gate_seen_while_claimed: list[object] = []

            def observe_claimed(_settings):
                gate_seen_while_claimed.append(read_transport_gate(settings))
                return False

            with (
                mock.patch("web_takeover.cancel_pending_computer_steps"),
                mock.patch("web_takeover.cancel_pending_observe_requests"),
                mock.patch("web_takeover.has_claimed_computer_steps", claimed),
                mock.patch("web_takeover.has_claimed_observe_requests", side_effect=observe_claimed),
                mock.patch("web_takeover.time.sleep"),
            ):
                status = web.start({"reason": "operator_requested", "ttl_seconds": 300})

            lease = status["takeover"]
            self.assertIsNotNone(lease)
            self.assertTrue(status["enabled"])
            self.assertEqual(lease["state"], "waiting_for_human")
            self.assertTrue(status["transport_allowed"])
            self.assertTrue(takeover_blocks_automation(settings))
            self.assertTrue(all(item is None for item in gate_seen_while_claimed))
            self.assertEqual(read_transport_gate(settings)["session_id"], lease["id"])

            active = web.activate(lease["id"])
            self.assertEqual(active["takeover"]["state"], "human_active")
            self.assertTrue(active["enabled"])

            closing = web.close(lease["id"], "complete")
            self.assertEqual(closing["closing"], "complete")
            self.assertTrue(closing["enabled"])
            self.assertTrue(takeover_blocks_automation(settings))
            self.assertEqual(read_close_request(settings)["session_id"], lease["id"])

    def test_sidecar_does_not_start_transport_without_web_gate(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.settings(root)
            HumanTakeoverStore(settings).start(reason="operator_requested", ttl_seconds=300)
            with (
                mock.patch("handoff_sidecar.transport_running", return_value=False),
                mock.patch("handoff_sidecar._run_transport") as transport,
            ):
                handoff_sidecar.run_once(settings)

            transport.assert_not_called()
            status = read_sidecar_status(settings)
            self.assertEqual(status["phase"], "waiting_for_idle")
            self.assertFalse(status["ready"])

    def test_sidecar_finalizes_only_after_transport_stop(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.settings(root)
            store = HumanTakeoverStore(settings)
            lease = store.start(reason="operator_requested", ttl_seconds=300)
            store.activate(lease["id"])
            allow_transport(settings, lease["id"])
            request_close(settings, lease["id"], "complete")

            with (
                mock.patch("handoff_sidecar.transport_running", side_effect=[True, False]),
                mock.patch(
                    "handoff_sidecar._run_transport",
                    return_value=(True, "", None, None),
                ) as stop,
            ):
                handoff_sidecar.run_once(settings)

            stop.assert_called_once_with("stop")
            self.assertIsNone(store.current())
            self.assertEqual(store.get(lease["id"])["state"], "completed")
            self.assertIsNone(read_close_request(settings))
            self.assertIsNone(read_transport_gate(settings))
            self.assertFalse(takeover_blocks_automation(settings))

    def test_sidecar_does_not_finalize_when_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.settings(root)
            store = HumanTakeoverStore(settings)
            lease = store.start(reason="operator_requested", ttl_seconds=300)
            allow_transport(settings, lease["id"])
            request_close(settings, lease["id"], "cancel")

            with (
                mock.patch("handoff_sidecar.transport_running", side_effect=[True, True]),
                mock.patch(
                    "handoff_sidecar._run_transport",
                    return_value=(False, "cleanup failed", None, None),
                ),
            ):
                handoff_sidecar.run_once(settings)

            self.assertIsNotNone(store.current())
            self.assertTrue(takeover_blocks_automation(settings))
            self.assertIsNotNone(read_transport_gate(settings))
            status = read_sidecar_status(settings)
            self.assertEqual(status["phase"], "closing")
            self.assertIn("cleanup failed", status["error"])

    def test_sidecar_starts_transport_only_for_gated_open_lease(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.settings(root)
            lease = HumanTakeoverStore(settings).start(
                reason="operator_requested",
                ttl_seconds=300,
            )
            allow_transport(settings, lease["id"])
            with (
                mock.patch("handoff_sidecar.transport_running", side_effect=[False, True]),
                mock.patch(
                    "handoff_sidecar._run_transport",
                    return_value=(
                        True,
                        "",
                        "https://vps.tailnet.ts.net:8443/vnc.html",
                        "http://127.0.0.1:6080/vnc.html",
                    ),
                ) as start,
            ):
                handoff_sidecar.run_once(settings)

            start.assert_called_once_with("start")
            status = read_sidecar_status(settings)
            self.assertTrue(status["ready"])
            self.assertEqual(
                status["url"],
                "https://vps.tailnet.ts.net:8443/vnc.html",
            )

    def test_web_takeover_disabled_status_and_start_raises(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.settings(root, enabled=False)
            web = WebTakeover(settings)
            status = web.status()
            self.assertEqual(
                status,
                {
                    "enabled": False,
                    "takeover": None,
                    "privacy_mode": False,
                    "closing": None,
                    "transport_allowed": False,
                    "transport": None,
                    "message": "Human takeover is disabled (set CONVEYOR_TAKEOVER_ENABLED=true)",
                },
            )
            with self.assertRaises(RuntimeError) as ctx:
                web.start({"reason": "operator_requested"})
            self.assertIn("Human takeover is disabled", str(ctx.exception))

    def test_sidecar_disabled_does_not_start_transport(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.settings(root, enabled=False)
            HumanTakeoverStore(settings).start(reason="operator_requested", ttl_seconds=300)
            allow_transport(settings, "fake-session")
            with (
                mock.patch("handoff_sidecar.transport_running", return_value=False),
                mock.patch("handoff_sidecar._run_transport") as transport,
            ):
                handoff_sidecar.run_once(settings)

            transport.assert_not_called()
            status = read_sidecar_status(settings)
            self.assertEqual(status["phase"], "disabled")
            self.assertFalse(status["ready"])

    def test_sidecar_disabled_close_request_stops_and_finalizes(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.settings(root, enabled=False)
            store = HumanTakeoverStore(settings)
            lease = store.start(reason="operator_requested", ttl_seconds=300)
            store.activate(lease["id"])
            allow_transport(settings, lease["id"])
            request_close(settings, lease["id"], "complete")

            with (
                mock.patch("handoff_sidecar.transport_running", side_effect=[True, False]),
                mock.patch(
                    "handoff_sidecar._run_transport",
                    return_value=(True, "", None, None),
                ) as stop,
            ):
                handoff_sidecar.run_once(settings)

            stop.assert_called_once_with("stop")
            self.assertIsNone(store.current())
            self.assertEqual(store.get(lease["id"])["state"], "completed")
            self.assertIsNone(read_close_request(settings))
            self.assertIsNone(read_transport_gate(settings))
            self.assertFalse(takeover_blocks_automation(settings))


    def test_sidecar_disabled_stops_live_owned_transport_and_cancels(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.settings(root, enabled=False)
            store = HumanTakeoverStore(settings)
            lease = store.start(reason="operator_requested", ttl_seconds=300)
            store.activate(lease["id"])
            allow_transport(settings, lease["id"])

            with (
                mock.patch("handoff_sidecar.transport_running", side_effect=[True, False, False]),
                mock.patch(
                    "handoff_sidecar._run_transport",
                    return_value=(True, "", None, None),
                ) as transport,
            ):
                handoff_sidecar.run_once(settings)

            transport.assert_called_once_with("stop")
            self.assertIsNone(store.current())
            self.assertEqual(store.get(lease["id"])["state"], "cancelled")
            self.assertIsNone(read_transport_gate(settings))
            self.assertEqual(read_sidecar_status(settings)["phase"], "disabled")

class TakeoverSettingsTests(unittest.TestCase):
    def test_settings_default_and_env_flag(self):
        import os
        from pathlib import Path
        from config import Settings, load_runtime_settings, load_settings

        s = Settings(
            telegram_bot_token="token",
            telegram_allowed_user_id=123,
            codex_workspace_root=Path("/tmp"),
            codex_bin="codex",
            codex_task_root=Path("/tmp"),
            codex_model=None,
            codex_timeout_seconds=3600,
            telegram_progress_seconds=3,
            codex_retry_429_delays_seconds=(300,),
            codex_memory_root=Path("/tmp"),
            user_timezone="UTC",
        )
        self.assertFalse(s.conveyor_takeover_enabled)

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            ws = tdp / "workspace"
            tasks = tdp / "tasks"
            mem = tdp / "memory"
            ws.mkdir()
            tasks.mkdir()
            mem.mkdir()
            env_file = tdp / "test.env"
            env_file.write_text(
                f"CODEX_WORKSPACE_ROOT={ws}\n"
                f"CODEX_TASK_ROOT={tasks}\n"
                f"CODEX_MEMORY_ROOT={mem}\n"
            )

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CONVEYOR_TAKEOVER_ENABLED", None)
                os.environ["TELEGRAM_BOT_TOKEN"] = "token"
                os.environ["TELEGRAM_ALLOWED_USER_ID"] = "123"

                loaded = load_settings(env_file=env_file)
                self.assertFalse(loaded.conveyor_takeover_enabled)
                loaded_rt = load_runtime_settings(env_file=env_file)
                self.assertFalse(loaded_rt.conveyor_takeover_enabled)

                for truthy in ("true", "1", "yes", "on"):
                    os.environ["CONVEYOR_TAKEOVER_ENABLED"] = truthy
                    loaded = load_settings(env_file=env_file)
                    self.assertTrue(loaded.conveyor_takeover_enabled)
                    loaded_rt = load_runtime_settings(env_file=env_file)
                    self.assertTrue(loaded_rt.conveyor_takeover_enabled)


class TakeoverEntrypointConfigTests(unittest.TestCase):
    """Takeover processes must run in web-only deployments (no Telegram)."""

    def test_entrypoints_use_runtime_settings_and_shared_logging(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for rel in ("web_console_takeover.py", "handoff_sidecar.py", "scripts/handoffctl.py"):
            text = (root / rel).read_text(encoding="utf-8")
            with self.subTest(file=rel):
                self.assertIn("load_runtime_settings", text)
                self.assertNotRegex(text, r"\bload_settings\(")
        for rel in ("web_console_takeover.py", "handoff_sidecar.py"):
            text = (root / rel).read_text(encoding="utf-8")
            with self.subTest(file=rel):
                self.assertIn("configure_logging(", text)
                self.assertNotIn("logging.basicConfig", text)


if __name__ == "__main__":
    unittest.main()
