"""Live screen: embedded host desktop view + one-click takeover.

The contract under test is the safety one: input only while the operator
holds the takeover lease, the Agent is blocked for exactly that long, and an
abandoned viewer gives the desktop back.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import live_screen
from human_takeover import HumanTakeoverStore, takeover_blocks_automation
from live_screen import LiveScreen, LiveScreenError

TOKEN = "t" * 40
JPEG_A = b"\xff\xd8frame-a\xff\xd9"
JPEG_B = b"\xff\xd8frame-b\xff\xd9"


def _settings(root: Path, **overrides):
    from config import Settings

    base = Settings(
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
        live_screen_enabled=True,
    )
    return replace(base, **overrides)


class _Host:
    """Stand-in for the X11 host: records xdotool calls, serves fake frames."""

    def __init__(self) -> None:
        self.frames = [JPEG_A]
        self.xdotool: list[list[str]] = []
        self.other: list[list[str]] = []

    def run(self, args, **_kwargs):
        if args[0] == "import":
            data = self.frames[0] if len(self.frames) == 1 else self.frames.pop(0)
            return subprocess.CompletedProcess(args, 0, stdout=data, stderr=b"")
        if args[0] != "xdotool":
            self.other.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        if args[:2] == ["xdotool", "getdisplaygeometry"]:
            return subprocess.CompletedProcess(args, 0, stdout="1024 768\n", stderr="")
        self.xdotool.append(list(args[1:]))
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")


class LiveScreenCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = _settings(self.root)
        self.host = _Host()
        for patcher in (
            mock.patch.object(live_screen.subprocess, "run", self.host.run),
            mock.patch.object(live_screen.shutil, "which", lambda name: f"/usr/bin/{name}"),
            mock.patch.object(LiveScreen, "_display_env", lambda _self: {"DISPLAY": ":99"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.screen = LiveScreen(self.settings)
        self.addCleanup(self.screen.release_control)


class DisabledTests(unittest.TestCase):
    def test_off_by_default_and_refuses_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            screen = LiveScreen(_settings(Path(tmp), live_screen_enabled=False))
            status = screen.status()
            self.assertFalse(status["enabled"])
            self.assertFalse(status["available"])
            with self.assertRaises(LiveScreenError):
                screen.frame()
            with self.assertRaises(LiveScreenError):
                screen.take_control()
            with self.assertRaises(LiveScreenError):
                screen.send_input([{"t": "move", "x": 1, "y": 1}])

    def test_mock_settings_do_not_enable_it(self) -> None:
        self.assertFalse(LiveScreen(mock.MagicMock()).enabled)


class FrameTests(LiveScreenCase):
    def test_frame_then_no_content_until_the_screen_changes(self) -> None:
        seq, data, size = self.screen.frame(since=0, wait=3)
        self.assertEqual((data, size), (JPEG_A, (1024, 768)))
        self.assertIsNone(self.screen.frame(since=seq, wait=0.4))
        self.host.frames = [JPEG_B]
        newer = self.screen.frame(since=seq, wait=3)
        self.assertIsNotNone(newer)
        self.assertGreater(newer[0], seq)
        self.assertEqual(newer[1], JPEG_B)

    def test_watching_wakes_the_screensaver(self) -> None:
        self.screen.frame(since=0, wait=3)
        self.assertIn(["xset", "s", "reset"], self.host.other)
        self.assertIn(["xfce4-screensaver-command", "--deactivate"], self.host.other)

    def test_frames_are_not_written_to_disk(self) -> None:
        self.screen.frame(since=0, wait=3)
        leaked = [p for p in self.root.rglob("*") if p.is_file() and JPEG_A in p.read_bytes()]
        self.assertEqual(leaked, [])


class ControlTests(LiveScreenCase):
    def test_input_requires_control(self) -> None:
        with self.assertRaises(LiveScreenError):
            self.screen.send_input([{"t": "click", "x": 5, "y": 5, "b": 1}])
        self.assertEqual(self.host.xdotool, [])

    def test_take_blocks_agent_and_release_resumes(self) -> None:
        self.assertFalse(takeover_blocks_automation(self.settings))
        status = self.screen.take_control()
        self.assertTrue(status["controlling"])
        self.assertTrue(status["agent_paused"])
        self.assertTrue(takeover_blocks_automation(self.settings))
        lease = HumanTakeoverStore(self.settings).current()
        self.assertEqual(lease["state"], "human_active")
        self.assertEqual(lease["requested_by"], "live-screen")

        self.screen.send_input([{"t": "click", "x": 10, "y": 20, "b": 1}])
        self.assertEqual(self.host.xdotool[-1], ["mousemove", "10", "20", "click", "--repeat", "1", "1"])

        status = self.screen.release_control()
        self.assertFalse(status["controlling"])
        self.assertFalse(takeover_blocks_automation(self.settings))
        # Buttons and modifiers are let go before the Agent gets the desktop back.
        self.assertIn("mouseup", self.host.xdotool[-1])
        self.assertIn("keyup", self.host.xdotool[-1])
        sent = len(self.host.xdotool)
        with self.assertRaises(LiveScreenError):
            self.screen.send_input([{"t": "move", "x": 1, "y": 1}])
        self.assertEqual(len(self.host.xdotool), sent)

    def test_input_triggers_an_immediate_refresh(self) -> None:
        self.screen.take_control()
        seq, _, _ = self.screen.frame(since=0, wait=3)
        self.host.frames = [JPEG_B]
        started = time.monotonic()
        self.screen.send_input([{"t": "click", "x": 10, "y": 20, "b": 1}])
        newer = self.screen.frame(since=seq, wait=3)
        self.assertIsNotNone(newer)
        # Well inside one idle capture interval (0.25s at the default 4 fps).
        self.assertLess(time.monotonic() - started, 0.2)

    def test_take_is_refused_while_another_takeover_is_open(self) -> None:
        HumanTakeoverStore(self.settings).start(reason="payment", requested_by="web-console")
        with self.assertRaises(LiveScreenError):
            self.screen.take_control()
        self.assertEqual(self.screen.status()["blocked_by"], "web-console")

    def test_take_waits_for_in_flight_agent_action_then_gives_up(self) -> None:
        with mock.patch("desktop_computer_requests.has_claimed_computer_steps", return_value=True), \
                mock.patch.object(live_screen, "IN_FLIGHT_WAIT_SECONDS", 0.3):
            with self.assertRaises(LiveScreenError):
                self.screen.take_control()
        self.assertFalse(takeover_blocks_automation(self.settings))

    def test_input_stops_when_the_lease_is_lost(self) -> None:
        self.screen.take_control()
        lease = HumanTakeoverStore(self.settings).current()
        HumanTakeoverStore(self.settings).cancel(lease["id"])
        with self.assertRaises(LiveScreenError):
            self.screen.send_input([{"t": "move", "x": 1, "y": 1}])
        self.assertFalse(self.screen.status()["controlling"])

    def test_abandoned_viewer_releases_control(self) -> None:
        self.screen.take_control()
        self.screen._control_seen_at = time.monotonic() - live_screen.CONTROL_IDLE_SECONDS - 1
        self.screen._tend_control(time.monotonic())
        self.assertFalse(takeover_blocks_automation(self.settings))

    def test_present_viewer_renews_the_short_lease(self) -> None:
        self.screen.take_control()
        store = HumanTakeoverStore(self.settings)
        before = store.current()["expires_at"]
        self.screen._control_renewed_at = time.monotonic() - live_screen.LEASE_RENEW_EVERY_SECONDS - 1
        time.sleep(0.05)
        self.screen._tend_control(time.monotonic())
        self.assertGreater(store.current()["expires_at"], before)


class TranslateTests(unittest.TestCase):
    def translate(self, events):
        return LiveScreen._translate(events, 1024, 768)

    def test_pointer_keyboard_and_scroll(self) -> None:
        self.assertEqual(
            self.translate([
                {"t": "down", "x": 1, "y": 2, "b": 1},
                {"t": "move", "x": 30, "y": 40},
                {"t": "up", "x": 30, "y": 40, "b": 1},
                {"t": "scroll", "x": 5, "y": 6, "dir": "down", "n": 3},
                {"t": "key", "key": "Enter", "mods": []},
                {"t": "key", "key": "c", "mods": ["ctrl", "bogus"]},
            ]),
            [[
                "mousemove", "1", "2", "mousedown", "1",
                "mousemove", "30", "40",
                "mousemove", "30", "40", "mouseup", "1",
                "mousemove", "5", "6", "click", "--repeat", "3", "5",
                "key", "--clearmodifiers", "Return",
                "key", "--clearmodifiers", "ctrl+c",
            ]],
        )

    def test_text_is_passed_as_one_literal_argument(self) -> None:
        commands = self.translate([{"t": "move", "x": 1, "y": 1}, {"t": "text", "text": "--help; rm -rf ~"}])
        self.assertEqual(commands[0], ["mousemove", "1", "1"])
        self.assertEqual(commands[1][-2:], ["--", "--help; rm -rf ~"])

    def test_rejects_bad_events(self) -> None:
        for bad in (
            [{"t": "click", "x": 5000, "y": 1, "b": 1}],
            [{"t": "click", "x": -1, "y": 1, "b": 1}],
            [{"t": "click", "x": 1, "y": 1, "b": 9}],
            [{"t": "scroll", "x": 1, "y": 1, "dir": "sideways"}],
            [{"t": "text", "text": "x" * 501}],
            [{"t": "exec", "cmd": "id"}],
            [{"t": "move"}],
            ["move"],
        ):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.translate(bad)

    def test_unknown_named_keys_are_dropped(self) -> None:
        self.assertEqual(self.translate([{"t": "key", "key": "MediaPlayPause", "mods": []}]), [])


class StoreExtendTests(unittest.TestCase):
    def test_extend_only_touches_open_leases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HumanTakeoverStore(_settings(Path(tmp)))
            lease = store.start(reason="operator_requested", ttl_seconds=60)
            extended = store.extend(lease["id"], 600)
            self.assertGreater(extended["expires_at"], lease["expires_at"] + 500)
            self.assertIsNone(store.extend(lease["id"], 5))
            store.complete(lease["id"])
            self.assertIsNone(store.extend(lease["id"], 600))


class HttpTests(LiveScreenCase):
    def setUp(self) -> None:
        super().setUp()
        from web_console import WebConsoleHandler, WebConsoleServer

        self.loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        loop_thread.start()
        self.server = WebConsoleServer(
            ("127.0.0.1", 0), WebConsoleHandler,
            control=SimpleNamespace(settings=self.settings), loop=self.loop, token=TOKEN,
        )
        self.addCleanup(self.server.live_screen.release_control)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            self.server.shutdown()
            self.server.server_close()
            self.loop.call_soon_threadsafe(self.loop.stop)
            loop_thread.join(timeout=2)
            thread.join(timeout=2)

        self.addCleanup(stop)

    def call(self, method: str, path: str, body=None, authorized: bool = True):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        headers = {"Authorization": f"Bearer {TOKEN}"} if authorized else {}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response, data

    def test_requires_auth(self) -> None:
        for method, path in (("GET", "/api/screen/status"), ("GET", "/api/screen/frame"),
                             ("POST", "/api/screen/control"), ("POST", "/api/screen/input")):
            response, _ = self.call(method, path, body={} if method == "POST" else None, authorized=False)
            self.assertEqual(response.status, 401, path)

    def test_view_take_input_release_over_http(self) -> None:
        response, data = self.call("GET", "/api/screen/frame?since=0")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "image/jpeg")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader("X-Screen-Width"), "1024")
        self.assertEqual(data, JPEG_A)

        response, data = self.call("POST", "/api/screen/input", {"events": [{"t": "move", "x": 1, "y": 1}]})
        self.assertEqual(response.status, 409)

        response, data = self.call("POST", "/api/screen/control", {"action": "take"})
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(data)["controlling"])
        self.assertTrue(takeover_blocks_automation(self.settings))

        response, _ = self.call("POST", "/api/screen/input", {"events": [{"t": "text", "text": "hi"}]})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.host.xdotool[-1][0], "type")
        response, _ = self.call("POST", "/api/screen/input", {"events": [{"t": "exec"}]})
        self.assertEqual(response.status, 400)

        response, data = self.call("POST", "/api/screen/control", {"action": "release"})
        self.assertFalse(json.loads(data)["controlling"])
        self.assertFalse(takeover_blocks_automation(self.settings))

    def test_disabled_console_reports_and_refuses(self) -> None:
        self.server.live_screen = LiveScreen(_settings(self.root, live_screen_enabled=False))
        response, data = self.call("GET", "/api/screen/status")
        self.assertFalse(json.loads(data)["enabled"])
        response, _ = self.call("GET", "/api/screen/frame")
        self.assertEqual(response.status, 409)
        response, _ = self.call("POST", "/api/screen/control", {"action": "take"})
        self.assertEqual(response.status, 409)


if __name__ == "__main__":
    unittest.main()
