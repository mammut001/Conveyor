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
    def settings(self, root: str):
        return SimpleNamespace(
            codex_memory_root=root,
            conveyor_computer_max_seconds=1,
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
            self.assertEqual(lease["state"], "waiting_for_human")
            self.assertTrue(status["transport_allowed"])
            self.assertTrue(takeover_blocks_automation(settings))
            self.assertTrue(all(item is None for item in gate_seen_while_claimed))
            self.assertEqual(read_transport_gate(settings)["session_id"], lease["id"])

            active = web.activate(lease["id"])
            self.assertEqual(active["takeover"]["state"], "human_active")

            closing = web.close(lease["id"], "complete")
            self.assertEqual(closing["closing"], "complete")
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


if __name__ == "__main__":
    unittest.main()
