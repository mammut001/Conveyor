"""Computer use on an agent's own desktop (phase 2b)."""
from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import agents
import desktop_x11
from agents import AgentStore
from desktop_computer_loop import ComputerBackendError, HttpComputerBackend, build_backend, run_computer_loop
from desktop_computer_planner import CodexPlanner
from desktop_computer_requests import (
    cancel_pending_computer_steps,
    claim_computer_step,
    create_computer_step,
    create_computer_task,
    get_computer_task,
    has_claimed_computer_steps,
    list_pending_computer_steps,
    task_scope,
    x11_node_id,
)
from desktop_x11 import X11ComputerBackend, X11Desktop, X11Error, hotkey_argument
from human_takeover import HumanTakeoverStore

# 1x1 PNG, enough for the screenshot bookkeeping to read real dimensions.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"
)


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
        agents_enabled=True,
        agent_desktops_enabled=True,
        conveyor_computer_use_enabled=True,
        conveyor_computer_direct_enabled=True,
        conveyor_computer_allowed_actions=("observe", "click", "type", "hotkey", "scroll", "wait"),
        conveyor_computer_blocked_keywords=("password", "payment"),
    )
    return replace(base, **overrides)


class _Display:
    """Fake X display: records xdotool calls, writes a PNG for `import`."""

    def __init__(self) -> None:
        self.xdotool: list[list[str]] = []
        self.envs: list[dict] = []
        self.windows = "12345\n"
        self.searches: list[list[str]] = []
        self.active_id = "12345"
        self.active_pid = "4242"
        self.active_title = "Example Domain"
        self.imports = 0

    def run(self, command, env=None, **_kwargs):
        self.envs.append(dict(env or {}))
        if command[0] == "xprop":
            return subprocess.CompletedProcess(
                command, 0,
                stdout=(
                    'WM_CLASS(STRING) = "Navigator", "firefox"\n'
                    "WM_STATE(WM_STATE):\n"
                    "\t\twindow state: Normal\n"
                    "\t\ticon window: 0x0\n"
                    "_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL\n"
                ),
                stderr="",
            )
        if command[0] == "import":
            self.imports += 1
            Path(command[-1].split(":", 1)[1]).write_bytes(PNG)
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        args = list(command[1:])
        out = ""
        if args[:1] == ["getdisplaygeometry"]:
            out = "1440 900\n"
        elif args == ["getactivewindow"]:
            out = self.active_id + "\n"
        elif args[:1] == ["getwindowpid"]:
            out = self.active_pid + "\n"
        elif args[:1] == ["getwindowname"]:
            out = self.active_title + "\n"
            if getattr(self, "move_focus_after_observe", False) and not getattr(self, "_focus_moved", False):
                self._focus_moved = True
                self.active_id = "99999"
                self.active_pid = "88"
        elif args[:1] == ["getwindowgeometry"]:
            out = "Position: 0,0\nGeometry: 100x100\n"
        elif args[:1] == ["search"]:
            self.searches.append(args)
            out = self.windows
        else:
            self.xdotool.append(args)
        return subprocess.CompletedProcess(command, 0, stdout=out, stderr="")


class _Sequence:
    """Planner that plays its actions in order, however many observes the
    loop inserts in between (ScriptedPlanner indexes by total steps)."""

    def __init__(self, actions: list[dict]) -> None:
        self._actions = list(actions)

    async def next_action(self, **_kwargs) -> dict:
        return dict(self._actions.pop(0)) if self._actions else {"action": "done", "summary": "ran out"}


class Case(unittest.TestCase):
    def desktop(self) -> X11Desktop:
        return X11Desktop(self.settings, agent_id=self.agent["id"], display=self.display)

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = _settings(self.root)
        self.store = AgentStore(self.settings)
        self.agent = self.store.create({"name": "Astra"})
        self.display = self.store.ensure_display(self.agent["id"])
        self.chat = agents.chat_id_for(self.agent["id"])
        self.scope = agents.takeover_scope(self.agent["id"])
        self.x = _Display()
        for patcher in (
            mock.patch.object(desktop_x11.subprocess, "run", self.x.run),
            mock.patch.object(desktop_x11.shutil, "which", lambda name: f"/usr/bin/{name}"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def task(self, channel="web", chat_id=None, **kwargs) -> dict:
        created = create_computer_task(
            self.settings, "open example.com", direct_mode=True, max_steps=8, max_seconds=60,
            operator_id="web-console", chat_id=self.chat if chat_id is None else chat_id, channel=channel, **kwargs,
        )
        self.assertTrue(created["ok"], created)
        return created["task"]


class RoutingTests(Case):
    def test_tasks_follow_their_conversation_to_a_desktop(self) -> None:
        mine = self.task()
        self.assertEqual((task_scope(mine), mine["agent_id"]), (self.scope, self.agent["id"]))
        for channel, chat_id in (("telegram", "42"), ("web", "webchat-abc"), ("web", "agent-default")):
            other = self.task(channel=channel, chat_id=chat_id)
            self.assertEqual(task_scope(other), "default")
            self.assertNotIn("agent_id", other)

    def test_without_agent_desktops_everything_is_the_host(self) -> None:
        self.settings = _settings(self.root, agent_desktops_enabled=False)
        self.assertEqual(task_scope(self.task()), "default")

    def test_backend_is_chosen_from_the_task(self) -> None:
        mine, host = self.task(), self.task(channel="telegram", chat_id="42")
        backend = build_backend(self.settings, mine["task_id"])
        self.assertIsInstance(backend, X11ComputerBackend)
        self.assertEqual(backend.desktop.env["DISPLAY"], f":{self.display}")
        self.assertIsInstance(build_backend(self.settings, host["task_id"]), HttpComputerBackend)
        self.assertIsInstance(build_backend(self.settings), HttpComputerBackend)
        self.store.archive(self.agent["id"])
        with self.assertRaises(ComputerBackendError):
            build_backend(self.settings, mine["task_id"])

    def test_host_node_never_sees_or_claims_agent_steps(self) -> None:
        mine, host = self.task(), self.task(channel="telegram", chat_id="42")
        mine_step = create_computer_step(self.settings, mine["task_id"], {"action": "observe"})["step_id"]
        host_step = create_computer_step(self.settings, host["task_id"], {"action": "observe"})["step_id"]
        self.assertEqual([s["step_id"] for s in list_pending_computer_steps(self.settings, limit=10)], [host_step])
        self.assertEqual(claim_computer_step(self.settings, mine_step, "macbook-payton")["error"], "wrong_node")
        self.assertEqual(claim_computer_step(self.settings, host_step, x11_node_id(self.scope))["error"], "wrong_node")
        self.assertTrue(claim_computer_step(self.settings, mine_step, x11_node_id(self.scope))["ok"])
        self.assertTrue(claim_computer_step(self.settings, host_step, "macbook-payton")["ok"])

    def test_one_running_task_per_desktop(self) -> None:
        self.task(single_active=True)
        again = create_computer_task(self.settings, "x", direct_mode=True, max_steps=1, max_seconds=30,
                                     chat_id=self.chat, channel="web", single_active=True)
        self.assertEqual(again["error"], "computer_task_active")
        # The host desktop is a different desktop: its task is not blocked.
        self.assertTrue(create_computer_task(self.settings, "x", direct_mode=True, max_steps=1, max_seconds=30,
                                             chat_id="42", channel="telegram", single_active=True)["ok"])


class TakeoverTests(Case):
    def test_a_takeover_pauses_only_its_own_desktop(self) -> None:
        mine, host = self.task(), self.task(channel="telegram", chat_id="42")
        leases = HumanTakeoverStore(self.settings)
        lease = leases.start(reason="operator_requested", scope=self.scope)
        self.assertEqual(create_computer_step(self.settings, mine["task_id"], {"action": "observe"})["error"],
                         "human_takeover_active")
        host_step = create_computer_step(self.settings, host["task_id"], {"action": "observe"})
        self.assertTrue(host_step["ok"])
        self.assertTrue(claim_computer_step(self.settings, host_step["step_id"], "macbook-payton")["ok"])
        leases.complete(lease["id"])

        leases.start(reason="operator_requested")  # now a human is on the host desktop
        mine_step = create_computer_step(self.settings, mine["task_id"], {"action": "observe"})
        self.assertTrue(mine_step["ok"])
        self.assertTrue(claim_computer_step(self.settings, mine_step["step_id"], x11_node_id(self.scope))["ok"])
        self.assertEqual(create_computer_step(self.settings, host["task_id"], {"action": "observe"})["error"],
                         "human_takeover_active")

    def test_cancel_and_in_flight_checks_are_per_desktop(self) -> None:
        mine, host = self.task(), self.task(channel="telegram", chat_id="42")
        mine_step = create_computer_step(self.settings, mine["task_id"], {"action": "observe"})["step_id"]
        host_step = create_computer_step(self.settings, host["task_id"], {"action": "observe"})["step_id"]
        self.assertEqual(cancel_pending_computer_steps(self.settings, scope=self.scope), 1)
        self.assertEqual(get_computer_task(self.settings, mine["task_id"])["steps"][mine_step]["status"], "cancelled")
        self.assertEqual(get_computer_task(self.settings, host["task_id"])["steps"][host_step]["status"], "pending")
        claim_computer_step(self.settings, host_step, "macbook-payton")
        self.assertTrue(has_claimed_computer_steps(self.settings))
        self.assertFalse(has_claimed_computer_steps(self.settings, scope=self.scope))


class DesktopTests(Case):
    def test_every_call_addresses_the_agents_display_and_nothing_else(self) -> None:
        self.desktop().execute({"action": "click", "x": 10, "y": 20})
        self.assertTrue(self.x.envs)
        for env in self.x.envs:
            self.assertEqual(env["DISPLAY"], f":{self.display}")
            self.assertEqual(set(env), {"PATH", "HOME", "DISPLAY", "XAUTHORITY"})  # no deployment secrets

    def test_actions_become_xdotool_calls(self) -> None:
        d = self.desktop()
        self.assertTrue(d.execute({"action": "click", "x": 640.4, "y": 360})["result_ok"])
        self.assertEqual(self.x.xdotool[-1], ["mousemove", "640", "360", "click", "1"])
        d.execute({"action": "click", "x": 5, "y": 6, "button": "right"})
        self.assertEqual(self.x.xdotool[-1][-1], "3")
        result = d.execute({"action": "type", "text": "--help; rm -rf ~"})
        self.assertEqual(self.x.xdotool[-1][-2:], ["--", "--help; rm -rf ~"])   # one literal argument
        self.assertEqual((result["text_len"], "text" in result), (16, False))    # the text never enters the result
        d.execute({"action": "hotkey", "keys": ["cmd", "l"]})
        self.assertEqual(self.x.xdotool[-1], ["key", "--clearmodifiers", "ctrl+l"])
        d.execute({"action": "scroll", "dx": 0, "dy": 500})
        self.assertEqual(self.x.xdotool[-1], ["click", "--repeat", "4", "5"])
        d.execute({"action": "scroll", "dx": 0, "dy": -120, "x": 100, "y": 100})
        self.assertEqual(self.x.xdotool[-1], ["mousemove", "100", "100", "click", "--repeat", "1", "4"])

    def test_bad_actions_fail_without_touching_the_screen(self) -> None:
        d = self.desktop()
        for action, error in (
            ({"action": "click", "x": 5000, "y": 5}, "point_outside_screen"),
            ({"action": "click", "x": -1, "y": 5}, "point_outside_screen"),
            ({"action": "click"}, "click_needs_xy"),
            ({"action": "click", "x": 1, "y": 1, "button": "fourth"}, "bad_button"),
            ({"action": "type", "text": ""}, "type_needs_text"),
            ({"action": "hotkey", "keys": ["ctrl", "no-such-key"]}, "bad_hotkey"),
            ({"action": "scroll"}, "bad_scroll"),
            ({"action": "launch", "cmd": "id"}, "unsupported_action"),
        ):
            result = d.execute(action)
            self.assertEqual((result["result_ok"], result["error"]), (False, error), action)
        self.assertEqual(self.x.xdotool, [])

    def test_hotkey_names(self) -> None:
        self.assertEqual(hotkey_argument(["ctrl", "shift", "t"]), "ctrl+shift+t")
        self.assertEqual(hotkey_argument(["enter"]), "Return")
        self.assertEqual(hotkey_argument(["alt", "left"]), "alt+Left")
        self.assertEqual(hotkey_argument(["ctrl"]), "ctrl")
        with self.assertRaises(X11Error):
            hotkey_argument([])

    def test_observe_stores_a_screenshot_the_planner_can_open(self) -> None:
        from desktop_computer_planner import planner_screenshot_path

        result = self.desktop().execute({"action": "observe"})
        self.assertTrue(result["result_ok"], result)
        self.assertEqual((result["width"], result["height"], result["active_app"]), (1, 1, "firefox"))
        self.assertEqual((result["window_id"], result["pid"], result["window_title"]), (12345, 4242, "Example Domain"))
        path = planner_screenshot_path(self.settings, result)
        self.assertIsNotNone(path)
        self.assertEqual(path.read_bytes(), PNG)


class LoopTests(Case):
    def run_loop(self, actions, task):
        backend = build_backend(self.settings, task["task_id"])
        backend._prepared = True  # the browser wait is covered separately
        return asyncio.run(run_computer_loop(
            self.settings, task["goal"], planner=_Sequence(actions), backend=backend,
            max_steps=8, max_seconds=30, direct_mode=True, task_id=task["task_id"],
        ))

    def test_a_task_runs_end_to_end_on_the_agents_desktop(self) -> None:
        task = self.task()
        result = self.run_loop([
            {"action": "observe"},
            {"action": "hotkey", "keys": ["ctrl", "l"]},
            {"action": "type", "text": "example.com"},
            {"action": "hotkey", "keys": ["enter"]},
            {"action": "done", "summary": "opened"},
        ], task)
        self.assertEqual((result["status"], result["summary"]), ("done", "opened"))
        self.assertEqual(
            [call[0] for call in self.x.xdotool], ["key", "type", "key"],
        )
        final = get_computer_task(self.settings, task["task_id"])
        statuses = {step["status"] for step in final["steps"].values()}
        self.assertEqual(statuses, {"completed"})
        # The stored trail is redacted like any other task's.
        self.assertNotIn("example.com", str(final["trajectory"]))
        self.assertTrue(result["screenshot_id"])

    def test_a_human_on_the_agents_desktop_pauses_the_task(self) -> None:
        task = self.task()
        leases = HumanTakeoverStore(self.settings)
        lease = leases.start(reason="operator_requested", scope=self.scope, ttl_seconds=30)

        async def scenario():
            backend = build_backend(self.settings, task["task_id"])
            backend._prepared = True
            running = asyncio.create_task(run_computer_loop(
                self.settings, task["goal"],
                planner=_Sequence([{"action": "click", "x": 5, "y": 5}, {"action": "done", "summary": "ok"}]),
                backend=backend, max_steps=6, max_seconds=30, direct_mode=True, task_id=task["task_id"],
            ))
            await asyncio.sleep(0.8)
            paused_calls = list(self.x.xdotool)
            leases.complete(lease["id"])
            return paused_calls, await asyncio.wait_for(running, timeout=20)

        paused_calls, result = asyncio.run(scenario())
        self.assertEqual(paused_calls, [])                 # nothing moved while the human had it
        self.assertEqual(result["status"], "done")
        self.assertEqual(self.x.xdotool, [["mousemove", "5", "5", "click", "1"]])

    def test_agent_desktop_does_not_use_the_host_browser_controller(self) -> None:
        created = create_computer_task(
            self.settings, "open firefox and read the page", direct_mode=True,
            max_steps=6, max_seconds=30, operator_id="web-console", chat_id=self.chat, channel="web",
        )
        self.assertTrue(created["ok"], created)
        task = created["task"]
        with mock.patch(
            "desktop_linux_browser.LinuxBrowserController.ensure",
            side_effect=AssertionError("host browser controller"),
        ):
            result = self.run_loop([
                {"action": "observe"},
                {"action": "done", "summary": "opened"},
            ], task)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["summary"], "opened")

    def test_focus_change_between_observe_and_type_refuses_keystrokes(self) -> None:
        self.x.move_focus_after_observe = True
        task = self.task()
        result = self.run_loop([
            {"action": "observe"},
            {"action": "type", "text": "hello"},
            {"action": "done", "summary": "typed"},
        ], task)
        self.assertFalse(any(call[:1] == ["type"] for call in self.x.xdotool))
        self.assertNotEqual(result["status"], "done")
        final = get_computer_task(self.settings, task["task_id"])
        typed = [
            step for step in final["steps"].values()
            if (step.get("action") or {}).get("action") == "type"
        ]
        self.assertTrue(typed)
        self.assertEqual(typed[0]["action"].get("pid"), 4242)
        self.assertEqual(typed[0]["action"].get("window_id"), 12345)
        self.assertNotIn("hello", str(final["trajectory"]))

    def test_type_stops_when_the_bound_window_is_not_foreground(self) -> None:
        desktop = self.desktop()
        result = desktop.execute({"action": "type", "text": "secret", "window_id": 999, "pid": 5})
        self.assertEqual(result["error"], "keyboard_target_not_foreground")
        self.assertFalse(any(call[:1] == ["type"] for call in self.x.xdotool))

    def test_blocked_keywords_still_stop_the_task(self) -> None:
        task = self.task()
        result = self.run_loop([{"action": "type", "text": "my password is hunter2"}], task)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.x.xdotool, [])

    def test_backend_asks_for_the_browser_on_an_empty_desktop(self) -> None:
        task = self.task()
        self.x.windows = ""
        backend = build_backend(self.settings, task["task_id"])

        async def scenario():
            with mock.patch.object(desktop_x11, "BROWSER_WAIT_SECONDS", 1.5):
                await backend._prepare()

        asyncio.run(scenario())
        self.assertTrue((agents.desktop_dir(self.settings, self.agent["id"]) / "want_browser").exists())
        # It looks for a browser window specifically: the window manager's own
        # helper windows would make "any window" true on an empty desktop.
        self.assertTrue(self.x.searches)
        for search in self.x.searches:
            self.assertEqual(search[search.index("--class") + 1], desktop_x11.BROWSER_CLASSES)

    def test_lease_before_execute_does_not_launch_or_observe(self) -> None:
        task = self.task()
        self.x.windows = ""
        backend = build_backend(self.settings, task["task_id"])
        step_id = create_computer_step(self.settings, task["task_id"], {"action": "observe"})["step_id"]
        HumanTakeoverStore(self.settings).start(
            reason="operator_requested", scope=self.scope, ttl_seconds=30,
        )

        async def scenario():
            with self.assertRaises(ComputerBackendError) as caught:
                await backend.execute_step(self.settings, task["task_id"], step_id, {"action": "observe"})
            self.assertEqual(str(caught.exception), "human_takeover_active")

        asyncio.run(scenario())
        self.assertEqual(self.x.searches, [])
        self.assertEqual(self.x.imports, 0)
        self.assertFalse((agents.desktop_dir(self.settings, self.agent["id"]) / "want_browser").exists())
        self.assertEqual(backend.node_id, x11_node_id(self.scope))

    def test_lease_during_prepare_races_ahead_of_launch_and_observe(self) -> None:
        task = self.task()
        self.x.windows = ""
        backend = build_backend(self.settings, task["task_id"])
        step_id = create_computer_step(self.settings, task["task_id"], {"action": "observe"})["step_id"]
        leases = HumanTakeoverStore(self.settings)
        original = backend.desktop.has_browser_window

        def has_browser():
            leases.start(reason="operator_requested", scope=self.scope, ttl_seconds=30)
            return original()

        backend.desktop.has_browser_window = has_browser

        async def scenario():
            with self.assertRaises(ComputerBackendError) as caught:
                await backend.execute_step(self.settings, task["task_id"], step_id, {"action": "observe"})
            self.assertEqual(str(caught.exception), "human_takeover_active")

        asyncio.run(scenario())
        self.assertFalse((agents.desktop_dir(self.settings, self.agent["id"]) / "want_browser").exists())
        self.assertEqual(self.x.imports, 0)
        self.assertEqual(backend.node_id, x11_node_id(self.scope))


class PlannerPromptTests(unittest.TestCase):
    def prompt(self, **kwargs) -> str:
        from types import SimpleNamespace

        planner = CodexPlanner(SimpleNamespace(), **kwargs)
        return planner._build_prompt(goal="打开 example.com", observation={"screenshot_id": "s"},
                                     trajectory=[], steps_used=1, max_steps=8)

    def test_agent_desktop_prompt_uses_screen_pixels(self) -> None:
        own = self.prompt(screen_coordinates=True)
        self.assertIn("屏幕像素", own)
        self.assertNotIn("element_index\":5", own)
        self.assertNotIn("必须带上目标窗口的 pid", own)
        host = self.prompt()
        self.assertIn("必须带上目标窗口的 pid", host)
        self.assertNotIn("屏幕像素，直接点", host)


if __name__ == "__main__":
    unittest.main()
