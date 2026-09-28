"""Exercise the optional Serve route without a real desktop or tailnet."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class TailnetHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        scripts = self.root / "scripts"
        scripts.mkdir()
        self.script = scripts / "novnc_handoff.sh"
        shutil.copyfile(ROOT / "scripts" / "novnc_handoff.sh", self.script)
        (scripts / "handoffctl.py").write_text(
            'import json,os\nprint(json.dumps({"takeover": json.load(open(os.environ["TEST_LEASE_FILE"]))}))\n',
            encoding="utf-8",
        )
        self.lease_file = self.root / "lease.json"
        self.set_lease(remaining=120)
        self.state = self.root / "serve.json"
        self.state.write_text('{"route": false}', encoding="utf-8")
        self.state_dir = self.root / "runtime"
        web = self.root / "novnc"
        web.mkdir()
        (web / "vnc.html").touch()
        xauth = self.root / "Xauthority"
        xauth.touch()
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        (fake_bin / "x11vnc").write_text(
            '#!/usr/bin/env bash\n'
            'if [[ "$1" == "-storepasswd" ]]; then touch "$3"; exit 0; fi\n'
            'exec sleep 120\n', encoding="utf-8",
        )
        (fake_bin / "websockify").write_text(
            '#!/usr/bin/env bash\nexec sleep 120\n', encoding="utf-8",
        )
        (fake_bin / "tailscale").write_text(
            '#!/usr/bin/env python3\n'
            'import json, os, pathlib, sys\n'
            'p = pathlib.Path(os.environ["TEST_TAILSCALE_STATE"])\n'
            'args = sys.argv[1:]\n'
            'if args == ["status", "--json"]:\n'
            '    print(json.dumps({"Self": {"DNSName": "vps.example.ts.net."}}))\n'
            'elif args == ["serve", "status", "--json"]:\n'
            '    route = json.loads(p.read_text())["route"]\n'
            '    print(json.dumps({"Web": {"vps:8443": {}}} if route else {}))\n'
            'elif args == ["serve", "--https=8443", "off"]:\n'
            '    if os.environ.get("TEST_FAIL_OFF") == "1": sys.exit(1)\n'
            '    p.write_text(json.dumps({"route": False}))\n'
            'elif args == ["serve", "--yes", "--https=8443", "http://127.0.0.1:6080"]:\n'
            '    if os.environ.get("TEST_FAIL_SERVE_START") == "1": sys.exit(1)\n'
            '    p.write_text(json.dumps({"route": True}))\n'
            '    os.execvp("sleep", ["sleep", "120"])\n'
            'else:\n'
            '    sys.exit(2)\n', encoding="utf-8",
        )
        for binary in fake_bin.iterdir():
            binary.chmod(0o755)
        self.env = os.environ | {
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "CONVEYOR_HANDOFF_TAILSCALE_SERVE": "1",
            "CONVEYOR_HANDOFF_STATE_DIR": str(self.state_dir),
            "CONVEYOR_NOVNC_WEB_ROOT": str(web),
            "CONVEYOR_HANDOFF_XAUTHORITY": str(xauth),
            "TEST_LEASE_FILE": str(self.lease_file),
            "TEST_TAILSCALE_STATE": str(self.state),
        }
        self.addCleanup(self._cleanup_transport)

    def _cleanup_transport(self) -> None:
        self.env.pop("TEST_FAIL_OFF", None)
        self.run_script("stop")

    def set_lease(self, remaining: int) -> None:
        self.lease_file.write_text(json.dumps({
            "id": "test-lease", "state": "human_active", "remaining_seconds": remaining,
        }), encoding="utf-8")

    def run_script(self, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.script), command], env=self.env,
            capture_output=True, text=True, timeout=20,
        )

    def test_phone_route_starts_only_for_lease_then_cleans_on_stop(self) -> None:
        started = self.run_script("start")
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertIn("https://vps.example.ts.net:8443/vnc.html", started.stdout)
        self.assertTrue(json.loads(self.state.read_text())["route"])
        self.assertTrue((self.state_dir / "tailscale-serve.port").exists())

        from scripts.handoffctl import _transport_running
        with mock.patch.dict(os.environ, {"CONVEYOR_HANDOFF_STATE_DIR": str(self.state_dir)}):
            self.assertTrue(_transport_running())
            stopped = self.run_script("stop")
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            self.assertFalse(_transport_running())
        self.assertFalse(json.loads(self.state.read_text())["route"])
        self.assertFalse((self.state_dir / "x11vnc.pid").exists())
        self.assertFalse((self.state_dir / "websockify.pid").exists())

    def test_existing_serve_port_is_not_replaced(self) -> None:
        self.state.write_text('{"route": true}', encoding="utf-8")
        result = self.run_script("start")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already in use", result.stderr)
        self.assertTrue(json.loads(self.state.read_text())["route"])
        self.assertFalse((self.state_dir / "x11vnc.pid").exists())
        # The preexisting route belongs to another operator; never remove it.
        self.state.write_text('{"route": false}', encoding="utf-8")

    def test_failed_serve_cleanup_blocks_resume_and_stops_vnc(self) -> None:
        self.assertEqual(self.run_script("start").returncode, 0)
        self.env["TEST_FAIL_OFF"] = "1"
        failed = self.run_script("stop")
        self.assertNotEqual(failed.returncode, 0)
        self.assertTrue((self.state_dir / "tailscale-serve.port").exists())
        self.assertFalse((self.state_dir / "x11vnc.pid").exists())
        self.assertFalse((self.state_dir / "websockify.pid").exists())
        from scripts.handoffctl import _transport_running
        with mock.patch.dict(os.environ, {"CONVEYOR_HANDOFF_STATE_DIR": str(self.state_dir)}):
            self.assertTrue(_transport_running())
        self.env.pop("TEST_FAIL_OFF")
        self.assertEqual(self.run_script("stop").returncode, 0)

    def test_serve_start_failure_closes_local_transport(self) -> None:
        self.env["TEST_FAIL_SERVE_START"] = "1"
        failed = self.run_script("start")
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(json.loads(self.state.read_text())["route"])
        self.assertFalse((self.state_dir / "tailscale-serve.port").exists())
        self.assertFalse((self.state_dir / "x11vnc.pid").exists())
        self.assertFalse((self.state_dir / "websockify.pid").exists())

    def test_watchdog_removes_route_before_lease_expiry(self) -> None:
        self.assertEqual(self.run_script("start").returncode, 0)
        self.set_lease(remaining=9)
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline and (
            (self.state_dir / "tailscale-serve.port").exists()
            or (self.state_dir / "x11vnc.pid").exists()
        ):
            time.sleep(0.2)
        self.assertFalse((self.state_dir / "tailscale-serve.port").exists())
        self.assertFalse(json.loads(self.state.read_text())["route"])
        self.assertFalse((self.state_dir / "x11vnc.pid").exists())


if __name__ == "__main__":
    unittest.main()
