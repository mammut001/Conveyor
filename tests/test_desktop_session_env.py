"""The desktop services must borrow their display from the session itself."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "desktop_session_env.py"
spec = importlib.util.spec_from_file_location("desktop_session_env", SCRIPT)
desktop_session_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop_session_env)


class SessionEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.proc = Path(tmp.name)

    def process(self, pid: int, comm: str, cmdline: str, env: dict[str, str]) -> None:
        entry = self.proc / str(pid)
        entry.mkdir()
        (entry / "comm").write_text(comm + "\n")
        (entry / "cmdline").write_text(cmdline)
        (entry / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")

    def find(self) -> dict[str, str]:
        return desktop_session_env.session_env(self.proc, uid=os.getuid())

    def test_a_command_that_only_mentions_the_session_is_not_the_session(self) -> None:
        self.process(100, "bash", "bash -c pgrep xfce4-session", {"XDG_RUNTIME_DIR": "/run/user/1", "HOME": "/h"})
        self.assertEqual(self.find(), {})
        self.process(200, "xfce4-session", "xfce4-session", {"DISPLAY": ":9", "DBUS_SESSION_BUS_ADDRESS": "unix:x", "HOME": "/h"})
        self.assertEqual(self.find(), {"DISPLAY": ":9", "DBUS_SESSION_BUS_ADDRESS": "unix:x"})

    def test_a_session_without_a_display_is_skipped(self) -> None:
        self.process(100, "xfce4-session", "xfce4-session", {"HOME": "/h"})
        self.assertEqual(self.find(), {})
        self.process(200, "xfce4-session", "xfce4-session", {"DISPLAY": ":10"})
        self.assertEqual(self.find()["DISPLAY"], ":10")

    def test_other_users_and_non_process_entries_are_ignored(self) -> None:
        (self.proc / "meminfo").write_text("x")
        self.process(100, "xfce4-session", "xfce4-session", {"DISPLAY": ":9"})
        self.assertEqual(desktop_session_env.session_env(self.proc, uid=os.getuid() + 1), {})


if __name__ == "__main__":
    unittest.main()
