"""Agent desktops: one always-on virtual desktop per agent (phase 2)."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import agent_desktops
import agents
from agent_desktops import Supervisor
from agents import AgentStore
from human_takeover import HumanTakeoverStore, takeover_blocks_automation
from live_screen import LiveScreen


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
        live_screen_enabled=True,
    )
    return replace(base, **overrides)


class _Processes:
    """Fake process table: records spawns and lets a test kill things."""

    def __init__(self) -> None:
        self.next_pid = 5000
        self.live: dict[int, list[str]] = {}
        self.spawned: list[tuple[list[str], dict[str, str]]] = []
        self.terminated: list[int] = []

    def spawn(self, command, env, log_path) -> int:
        self.next_pid += 1
        self.live[self.next_pid] = list(command)
        self.spawned.append((list(command), dict(env)))
        return self.next_pid

    def alive(self, pid: int, needle: str = "") -> bool:
        return pid in self.live and needle in " ".join(self.live[pid]) + " "

    def terminate(self, pid: int) -> None:
        self.terminated.append(pid)
        self.live.pop(pid, None)

    def commands(self) -> list[str]:
        return [Path(command[0]).name for command, _ in self.spawned]

    def pid_of(self, name: str) -> int:
        return next(pid for pid, command in self.live.items() if Path(command[0]).name == name)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = _settings(self.root)
        self.store = AgentStore(self.settings)
        self.procs = _Processes()
        ok = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        for patcher in (
            mock.patch.object(agent_desktops.shutil, "which", lambda name: f"/usr/bin/{name}"),
            mock.patch.object(agent_desktops.subprocess, "run", lambda *a, **k: ok),
            mock.patch.object(agent_desktops, "SNAP_FIREFOX", Path("/nonexistent/snap/firefox")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.supervisor = Supervisor(
            self.settings, spawn=self.procs.spawn, alive=self.procs.alive, terminate=self.procs.terminate,
        )


class DisplayAllocationTests(Case):
    def test_agents_get_distinct_displays_and_default_gets_none(self) -> None:
        a = self.store.create({"name": "A"})
        b = self.store.create({"name": "B"})
        self.assertIsNone(self.store.ensure_display("default"))
        self.assertEqual(self.store.ensure_display(a["id"]), 101)
        self.assertEqual(self.store.ensure_display(b["id"]), 102)
        self.assertEqual(self.store.ensure_display(a["id"]), 101)  # stable
        self.store.archive(a["id"])
        self.assertIsNone(self.store.ensure_display(a["id"]))
        c = self.store.create({"name": "C"})
        self.assertEqual(self.store.ensure_display(c["id"]), 101)  # freed number is reused


class SupervisorTests(Case):
    def test_off_means_no_desktops(self) -> None:
        self.store.create({"name": "A"})
        off = Supervisor(_settings(self.root, agent_desktops_enabled=False),
                         spawn=self.procs.spawn, alive=self.procs.alive, terminate=self.procs.terminate)
        self.assertEqual(off.reconcile(), {})
        self.assertEqual(self.procs.spawned, [])

    def test_starts_an_authenticated_display_and_a_window_manager(self) -> None:
        agent = self.store.create({"name": "A"})
        self.assertEqual(self.supervisor.reconcile(), {agent["id"]: 101})
        self.assertEqual(self.procs.commands(), ["Xvfb", "xfwm4"])
        xvfb, env = self.procs.spawned[0]
        self.assertEqual(xvfb[:4], ["Xvfb", ":101", "-screen", "0"])
        self.assertIn("1440x900x24", xvfb)
        self.assertEqual(xvfb[xvfb.index("-nolisten") + 1], "tcp")
        auth = xvfb[xvfb.index("-auth") + 1]
        self.assertEqual(auth, str(agents.xauthority_path(self.settings, agent["id"])))
        self.assertEqual(os.stat(auth).st_mode & 0o777, 0o600)
        self.assertEqual((env["DISPLAY"], env["XAUTHORITY"]), (":101", auth))

    def test_children_never_inherit_deployment_secrets(self) -> None:
        self.store.create({"name": "A"})
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "s3cret", "CONVEYOR_WEB_TOKEN": "s3cret", "DEEPSEEK_API_KEY": "s3cret"}):
            self.supervisor.reconcile()
        for _, env in self.procs.spawned:
            self.assertNotIn("s3cret", " ".join(env.values()))

    def test_running_desktop_is_adopted_not_restarted(self) -> None:
        self.store.create({"name": "A"})
        self.supervisor.reconcile()
        fresh = Supervisor(self.settings, spawn=self.procs.spawn, alive=self.procs.alive, terminate=self.procs.terminate)
        fresh.reconcile()
        fresh.reconcile()
        self.assertEqual(self.procs.commands(), ["Xvfb", "xfwm4"])
        self.assertEqual(self.procs.terminated, [])

    def test_crashed_display_comes_back(self) -> None:
        self.store.create({"name": "A"})
        self.supervisor.reconcile()
        del self.procs.live[self.procs.pid_of("Xvfb")]
        self.supervisor.reconcile()
        self.assertEqual(self.procs.commands(), ["Xvfb", "xfwm4", "Xvfb", "xfwm4"])

    def test_browser_opens_on_request_only_and_once(self) -> None:
        agent = self.store.create({"name": "A"})
        self.supervisor.reconcile()
        self.assertNotIn("firefox", self.procs.commands())

        agent_desktops.request_browser(self.settings, agent["id"])
        self.supervisor.reconcile()
        self.assertEqual(self.procs.commands().count("firefox"), 1)
        browser, env = self.procs.spawned[-1]
        self.assertIn("--no-remote", browser)
        profile = browser[browser.index("--profile") + 1]
        self.assertIn(agent["id"], profile)
        self.assertEqual(env["DISPLAY"], ":101")

        agent_desktops.request_browser(self.settings, agent["id"])  # already running
        self.supervisor.reconcile()
        self.assertEqual(self.procs.commands().count("firefox"), 1)

        del self.procs.live[self.procs.pid_of("firefox")]  # the operator closed it
        self.supervisor.reconcile()
        self.assertEqual(self.procs.commands().count("firefox"), 1)
        agent_desktops.request_browser(self.settings, agent["id"])
        self.supervisor.reconcile()
        self.assertEqual(self.procs.commands().count("firefox"), 2)

    def test_each_agent_has_its_own_browser_profile(self) -> None:
        a = self.store.create({"name": "A"})
        b = self.store.create({"name": "B"})
        self.supervisor.reconcile()
        for agent in (a, b):
            agent_desktops.request_browser(self.settings, agent["id"])
        self.supervisor.reconcile()
        launches = [(command, env) for command, env in self.procs.spawned if Path(command[0]).name == "firefox"]
        profiles = {command[command.index("--profile") + 1] for command, _ in launches}
        self.assertEqual(len(profiles), 2)
        self.assertEqual({env["DISPLAY"] for _, env in launches}, {":101", ":102"})

    def test_removed_agent_loses_its_desktop(self) -> None:
        agent = self.store.create({"name": "A"})
        agent_desktops.request_browser(self.settings, agent["id"])
        self.supervisor.reconcile()
        tracked = set(self.procs.live)
        self.store.archive(agent["id"])
        self.assertEqual(self.supervisor.reconcile(), {})
        self.assertEqual(set(self.procs.terminated), tracked)
        self.assertFalse((agents.desktop_dir(self.settings, agent["id"]) / "pids.json").exists())
        self.assertFalse(agents.xauthority_path(self.settings, agent["id"]).exists())

    def test_recycled_pids_are_never_killed(self) -> None:
        agent = self.store.create({"name": "A"})
        self.supervisor.reconcile()
        # A reboot: our processes are gone and their pids now belong to others.
        self.procs.live = {pid: ["sshd"] for pid in self.procs.live}
        self.supervisor.reconcile()
        self.assertEqual(self.procs.terminated, [])
        self.store.archive(agent["id"])

    def test_size_is_validated(self) -> None:
        self.assertEqual(agent_desktops.parse_size("1600x900"), (1600, 900))
        for bad in ("", "huge", "10x10", "99999x99999", "1440"):
            self.assertEqual(agent_desktops.parse_size(bad), (1440, 900))


class ScreenScopeTests(Case):
    def test_agent_screen_uses_its_display_and_cookie(self) -> None:
        screen = LiveScreen(self.settings, display=":101", xauthority="/x/Xauthority", scope="agent:a")
        env = screen._display_env()
        self.assertEqual((env["DISPLAY"], env["XAUTHORITY"]), (":101", "/x/Xauthority"))
        self.assertTrue(screen.status()["dedicated"])
        self.assertFalse(LiveScreen(self.settings).status()["dedicated"])

    def test_takeovers_are_independent_per_desktop(self) -> None:
        store = HumanTakeoverStore(self.settings)
        a = store.start(reason="operator_requested", scope="agent:a")
        self.assertFalse(takeover_blocks_automation(self.settings))            # host desktop untouched
        self.assertTrue(takeover_blocks_automation(self.settings, "agent:a"))
        self.assertFalse(takeover_blocks_automation(self.settings, "agent:b"))
        store.start(reason="operator_requested", scope="agent:b")              # a second desktop is fine
        store.start(reason="operator_requested")                                # and so is the host's
        with self.assertRaises(RuntimeError):
            store.start(reason="operator_requested", scope="agent:a")          # but not the same one twice
        store.complete(a["id"])
        self.assertFalse(takeover_blocks_automation(self.settings, "agent:a"))
        self.assertTrue(takeover_blocks_automation(self.settings, "agent:b"))

    def test_old_takeover_database_gets_the_scope_column(self) -> None:
        import sqlite3

        path = self.root / "state" / "human_takeover.sqlite3"
        path.unlink(missing_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute("""CREATE TABLE human_takeovers (id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL,
            task_id TEXT, requested_by TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            expires_at REAL NOT NULL, activated_at REAL, closed_at REAL, close_reason TEXT)""")
        conn.execute("INSERT INTO human_takeovers VALUES ('old','human_active','payment',NULL,'web-console',1,1,9999999999,NULL,NULL,NULL)")
        conn.commit(); conn.close()
        store = HumanTakeoverStore(self.settings)
        self.assertEqual(store.current()["id"], "old")  # an open pre-upgrade lease still blocks the host desktop
        self.assertIsNone(store.current("agent:a"))


class ScreenRoutingTests(Case):
    def _server(self):
        import asyncio
        from types import SimpleNamespace
        from web_console import WebConsoleHandler, WebConsoleServer

        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        server = WebConsoleServer(("127.0.0.1", 0), WebConsoleHandler,
                                  control=SimpleNamespace(settings=self.settings), loop=loop, token="t" * 40)
        self.addCleanup(server.server_close)
        return server

    def test_each_agent_resolves_to_its_own_screen(self) -> None:
        a = self.store.create({"name": "A"})
        b = self.store.create({"name": "B"})
        self.supervisor.reconcile()
        server = self._server()
        host = server.screen_for("")
        self.assertIs(server.screen_for("default"), host)       # the default agent shares the host desktop
        screen_a, screen_b = server.screen_for(a["id"]), server.screen_for(b["id"])
        self.assertIsNot(screen_a, screen_b)
        self.assertIs(server.screen_for(a["id"]), screen_a)     # one instance per agent desktop
        self.assertEqual(screen_a._display_env()["DISPLAY"], ":101")
        self.assertEqual(screen_b._display_env()["DISPLAY"], ":102")
        self.assertEqual(screen_a._scope, f"agent:{a['id']}")
        self.assertIsNone(server.screen_for("nope"))
        self.store.archive(b["id"])
        self.assertIsNone(server.screen_for(b["id"]))

    def test_without_desktops_every_agent_shares_the_host_screen(self) -> None:
        a = self.store.create({"name": "A"})
        self.settings = _settings(self.root, agent_desktops_enabled=False)
        server = self._server()
        self.assertIs(server.screen_for(a["id"]), server.live_screen)


if __name__ == "__main__":
    unittest.main()
