"""Mac desktop agent: a bad action fails its step, reports are retried, the server is idempotent."""
from __future__ import annotations

import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from conveyor.desktop.desktop_computer_requests import (
    claim_computer_step,
    complete_computer_step,
    create_computer_step,
    create_computer_task,
    fail_computer_step,
    get_computer_task,
)
from conveyor.desktop.desktop_cua import LocalCuaTransport
from conveyor.entrypoints import desktop_agent
from tests.test_computer_windows import _settings

_PROBE_OK = {"available": True, "path": "/usr/local/bin/cua-driver", "error": None}


class _Server:
    """Records the agent's HTTP calls; complete can fail a few times first."""

    def __init__(self, action: dict, *, complete_failures: int = 0) -> None:
        self.action = action
        self.complete_failures = complete_failures
        self.posts: list[tuple[str, dict]] = []

    def get_json(self, url, token):
        return {"steps": [{"step_id": "step-1"}]}

    def post_json(self, url, token, data):
        path = url.split("/desktop/computer/task/step/")[-1]
        self.posts.append((path, data))
        if path == "claim":
            return {"ok": True, "step": {"step_id": "step-1", "action": dict(self.action)}}
        if path == "complete" and self.complete_failures > 0:
            self.complete_failures -= 1
            raise urllib.error.URLError("connection reset")
        return {"ok": True}

    def calls(self, path: str) -> list[dict]:
        return [data for name, data in self.posts if name == path]


def _agent_settings() -> SimpleNamespace:
    return SimpleNamespace(conveyor_desktop_agent_token="tok", conveyor_desktop_node_id="mac-1")


class _RaisingDriver:
    def execute(self, action):
        raise RuntimeError("secret text typed by the planner")


def _transport_driver(case: unittest.TestCase) -> SimpleNamespace:
    """A driver backed by the real LocalCuaTransport with the cua binary mocked."""
    transport = LocalCuaTransport("cua-driver call", settings=_settings(Path(tempfile.mkdtemp())))
    transport._call_tool = mock.Mock(side_effect=AssertionError("no driver call for invalid input"))  # type: ignore[method-assign]
    for patcher in (
        mock.patch("conveyor.desktop.desktop_cua.sys.platform", "darwin"),
        mock.patch("conveyor.desktop.desktop_cua.probe_cua_driver", return_value=_PROBE_OK),
        mock.patch("conveyor.desktop.desktop_cua.get_active_app_macos", return_value="Calculator"),
    ):
        patcher.start()
        case.addCleanup(patcher.stop)
    return SimpleNamespace(execute=lambda action: transport.execute(action, "mac-1"))


class PollComputerTest(unittest.TestCase):
    def _poll(self, server: _Server, driver) -> None:
        with mock.patch.object(desktop_agent, "get_json", server.get_json), \
                mock.patch.object(desktop_agent, "post_json", server.post_json), \
                mock.patch.object(desktop_agent, "build_driver", return_value=driver), \
                mock.patch.object(desktop_agent.time, "sleep") as sleep:
            desktop_agent.poll_computer_once(_agent_settings())
        self.sleeps = [call.args[0] for call in sleep.call_args_list]

    def test_driver_exception_is_reported_as_step_fail(self) -> None:
        server = _Server({"action": "observe"})
        self._poll(server, _RaisingDriver())
        [fail] = server.calls("fail")
        self.assertEqual(fail["error"], "driver_exception")
        self.assertEqual(fail["message"], "Driver raised RuntimeError.")
        self.assertNotIn("secret", fail["message"])
        self.assertEqual(server.calls("complete"), [])

    def test_type_with_non_numeric_pid_fails_the_step(self) -> None:
        server = _Server({"action": "type", "pid": "abc", "text": "hello"})
        self._poll(server, _transport_driver(self))
        [fail] = server.calls("fail")
        self.assertEqual(fail["error"], "invalid_ax_fields")

    def test_hotkey_with_non_numeric_window_fails_the_step(self) -> None:
        server = _Server({"action": "hotkey", "pid": 12, "window_id": "w", "keys": ["cmd", "a"]})
        with mock.patch("conveyor.desktop.desktop_cua.get_app_name_for_pid", return_value="Calculator"):
            self._poll(server, _transport_driver(self))
        [fail] = server.calls("fail")
        self.assertEqual(fail["error"], "invalid_ax_fields")

    def test_scroll_with_non_numeric_delta_fails_the_step(self) -> None:
        for dx in ("x", "inf"):
            with self.subTest(dx=dx):
                server = _Server({"action": "scroll", "pid": 12, "dx": dx, "dy": 0})
                with mock.patch("conveyor.desktop.desktop_cua.get_app_name_for_pid", return_value="Calculator"):
                    self._poll(server, _transport_driver(self))
                [fail] = server.calls("fail")
                self.assertEqual(fail["error"], "invalid_scroll_delta")

    def test_complete_is_retried_with_backoff(self) -> None:
        server = _Server({"action": "observe"}, complete_failures=2)
        driver = SimpleNamespace(execute=lambda action: {"result_ok": True, "action_type": "observe"})
        self._poll(server, driver)
        self.assertEqual(len(server.calls("complete")), 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])
        self.assertEqual(server.calls("fail"), [])

    def test_complete_gives_up_after_three_attempts(self) -> None:
        server = _Server({"action": "observe"}, complete_failures=5)
        driver = SimpleNamespace(execute=lambda action: {"result_ok": True, "action_type": "observe"})
        self._poll(server, driver)
        self.assertEqual(len(server.calls("complete")), 3)

    def test_a_4xx_rejection_is_not_retried(self) -> None:
        server = _Server({"action": "observe"})
        rejected = urllib.error.HTTPError("u", 409, "Conflict", {}, None)
        rejected.read = lambda: b'{"ok": false, "error": "invalid_status"}'  # type: ignore[method-assign]
        calls = []

        def post(url, token, data):
            if url.endswith("/complete"):
                calls.append(data)
                raise rejected
            return server.post_json(url, token, data)

        driver = SimpleNamespace(execute=lambda action: {"result_ok": True, "action_type": "observe"})
        with mock.patch.object(desktop_agent, "get_json", server.get_json), \
                mock.patch.object(desktop_agent, "post_json", post), \
                mock.patch.object(desktop_agent, "build_driver", return_value=driver), \
                mock.patch.object(desktop_agent.time, "sleep"):
            desktop_agent.poll_computer_once(_agent_settings())
        self.assertEqual(len(calls), 1)


class _StopLoop(BaseException):
    pass


class HeartbeatLoopTest(unittest.TestCase):
    def test_a_failing_poll_does_not_end_the_loop(self) -> None:
        settings = SimpleNamespace(
            conveyor_desktop_agent_token="tok", conveyor_desktop_node_id="mac-1",
            conveyor_desktop_observe_poll_interval_seconds=0,
        )
        computer = mock.Mock(side_effect=ValueError("bad"), __name__="poll_computer_once")
        sleeps = {"n": 0}

        def sleep(_seconds):
            sleeps["n"] += 1
            if sleeps["n"] >= 3:
                raise _StopLoop

        with mock.patch.object(desktop_agent, "register_agent", return_value={"ok": True}), \
                mock.patch.object(desktop_agent, "send_heartbeat_once", return_value={"ok": True}), \
                mock.patch.object(desktop_agent, "poll_computer_once", computer), \
                mock.patch.object(desktop_agent.time, "sleep", sleep), \
                mock.patch("builtins.print"):
            with self.assertRaises(_StopLoop):
                desktop_agent.heartbeat_loop(settings, poll_computer=True)
        self.assertEqual(computer.call_count, 3)


class ServerIdempotencyTest(unittest.TestCase):
    def _claimed_step(self):
        settings = _settings(Path(tempfile.mkdtemp()))
        created = create_computer_task(settings, "open calculator", direct_mode=True, max_steps=4, max_seconds=30)
        step = create_computer_step(settings, created["task_id"], {"action": "observe"})
        self.assertTrue(claim_computer_step(settings, step["step_id"], "node").get("ok"))
        return settings, created["task_id"], step["step_id"]

    def _step(self, settings, task_id, step_id) -> dict:
        task = get_computer_task(settings, task_id)
        steps = task.get("steps") or {}
        if isinstance(steps, dict):
            return steps[step_id]
        return next(item for item in steps if item.get("step_id") == step_id)

    def test_repeated_complete_is_a_no_op_success(self) -> None:
        settings, task_id, step_id = self._claimed_step()
        result = {"result_ok": True, "action_type": "observe", "node_id": "node"}
        first = complete_computer_step(settings, step_id, "node", result)
        self.assertTrue(first["ok"], first)
        before = dict(self._step(settings, task_id, step_id))
        second = complete_computer_step(settings, step_id, "node", dict(result))
        self.assertTrue(second["ok"], second)
        self.assertTrue(second.get("duplicate"))
        self.assertEqual(self._step(settings, task_id, step_id), before)

    def test_a_different_second_result_is_still_refused(self) -> None:
        settings, _task_id, step_id = self._claimed_step()
        complete_computer_step(settings, step_id, "node", {"result_ok": True, "action_type": "observe"})
        other = complete_computer_step(settings, step_id, "node", {"result_ok": True, "action_type": "click"})
        self.assertFalse(other["ok"])
        self.assertEqual(other["error"], "invalid_status")
        late_fail = fail_computer_step(settings, step_id, "node", "driver_exception")
        self.assertFalse(late_fail["ok"])

    def test_repeated_fail_is_a_no_op_success(self) -> None:
        settings, _task_id, step_id = self._claimed_step()
        self.assertTrue(fail_computer_step(settings, step_id, "node", "driver_exception")["ok"])
        again = fail_computer_step(settings, step_id, "node", "driver_exception")
        self.assertTrue(again["ok"])
        self.assertTrue(again.get("duplicate"))
        self.assertFalse(complete_computer_step(
            settings, step_id, "node", {"result_ok": True, "action_type": "observe"})["ok"])


if __name__ == "__main__":
    unittest.main()
