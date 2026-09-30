from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import handoff_sidecar
from human_takeover import HumanTakeoverStore, takeover_blocks_automation
from web_takeover import (
    WebTakeover,
    read_close_request,
    read_sidecar_status,
    request_close,
)


class WebTakeoverTests(unittest.TestCase):
    def settings(self, root: str):
        return SimpleNamespace(
            codex_memory_root=root,
            conveyor_computer_max_seconds=1,
        )

    def test_web_start_activate_and_close_request_keep_agent_paused(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.settings(root)
            web = WebTakeover(settings)
            with (
                mock.patch("web_takeover.cancel_pending_computer_steps"),
                mock.patch("web_takeover.cancel_pending_observe_requests"),
                mock.patch("web_takeover.has_claimed_computer_steps", return_value=False),
                mock.patch("web_takeover.has_claimed_observe_requests", return_value=False),
            ):
                status = web.start({"reason": "operator_requested", "ttl_seconds": 300})

            lease = status["takeover"]
            self.assertIsNotNone(lease)
            self.assertEqual(lease["state"], "waiting_for_human")
            self.assertTrue(takeover_blocks_automation(settings))

            active = web.activate(lease["id"])
            self.assertEqual(active["takeover"]["state"], "human_active")

            closing = web.close(lease["id"], "complete")
            self.assertEqual(closing["closing"], "complete")
            self.assertTrue(takeover_blocks_automation(settings))
            self.assertEqual(read_close_request(settings)["session_id"], lease["id"])

    def test_sidecar_finalizes_only_after_transport_stop(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.settings(root)
            store = HumanTakeoverStore(settings)
            lease = store.start(reason="operator_requested", ttl_seconds=300)
            store.activate(lease["id"])
            request_close(settings, lease["id"], "complete")

            with (
                mock.patch("handoff_sidecar.transport_running", side_effect=[True, False]),
                mock.patch("handoff_sidecar._run_transport", return_value=(True, "", None, None)) as stop,
            ):
                handoff_sidecar.run_once(settings)

            stop.assert_called_once_with("stop")
            self.assertIsNone(store.current())
            self.assertEqual(store.get(lease["id"])["state"], "completed")
            self.assertIsNone(read_close_request(settings))
            self.assertFalse(takeover_blocks_automation(settings))

    def test_sidecar_does_not_finalize_when_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.settings(root)
            store = HumanTakeoverStore(settings)
            lease = store.start(reason="operator_requested", ttl_seconds=300)
            request_close(settings, lease["id"], "cancel")

            with (
                mock.patch("handoff_sidecar.transport_running", side_effect=[True, True]),
                mock.patch("handoff_sidecar._run_transport", return_value=(False, "cleanup failed", None, None)),
            ):
                handoff_sidecar.run_once(settings)

            self.assertIsNotNone(store.current())
            self.assertTrue(takeover_blocks_automation(settings))
            status = read_sidecar_status(settings)
            self.assertEqual(status["phase"], "closing")
            self.assertIn("cleanup failed", status["error"])

    def test_sidecar_starts_transport_for_open_lease(self):
        with tempfile.TemporaryDirectory() as root:
            settings = self.settings(root)
            HumanTakeoverStore(settings).start(reason="operator_requested", ttl_seconds=300)
            with (
                mock.patch("handoff_sidecar.transport_running", side_effect=[False, True]),
                mock.patch(
                    "handoff_sidecar._run_transport",
                    return_value=(True, "", "https://vps.tailnet.ts.net:8443/vnc.html", "http://127.0.0.1:6080/vnc.html"),
                ) as start,
            ):
                handoff_sidecar.run_once(settings)

            start.assert_called_once_with("start")
            status = read_sidecar_status(settings)
            self.assertTrue(status["ready"])
            self.assertEqual(status["url"], "https://vps.tailnet.ts.net:8443/vnc.html")


if __name__ == "__main__":
    unittest.main()
