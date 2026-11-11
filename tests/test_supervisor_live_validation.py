"""tests/test_supervisor_live_validation.py

Focused unit tests for the opt-in real AGY integration harness validation runner:
1. Normal validator output and exit on compliant clamp.py.
2. Normal validator rejection on stub implementation.
3. Dummy provider secrets are absent in the child environment (credential-free).
4. Hanging candidate or validator is bounded by deadline and reaped (no orphans).
5. Infinite output candidate is stopped actively at the byte cap and reaped.
6. Zero-exit leader leaving lingering background descendant with closed stdio fails
   and owned process group is reaped (no background writers tolerated).
7. Zero-exit leader leaving background descendant with inherited open pipe fails
   and owned process group is reaped within deadline.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conveyor.agents import agent_supervisor as ags
from scripts.agent_supervisor_live import (
    CLAMP_STUB_SOURCE,
    run_frozen_validator_cmd,
    write_frozen_validator,
)


class TestSupervisorLiveValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="test-live-val-")).resolve()
        self.val_path = self.tmp / "validate_clamp.py"
        write_frozen_validator(self.val_path)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_normal_validator_success(self) -> None:
        """Validator exits 0 with expected summary on correct clamp implementation."""
        wt = self.tmp / "wt_good"
        wt.mkdir(parents=True, exist_ok=True)
        good_clamp = (
            "def clamp(value, lower, upper):\n"
            "    if lower > upper:\n"
            "        raise ValueError('lower > upper')\n"
            "    if value < lower:\n"
            "        return lower\n"
            "    if value > upper:\n"
            "        return upper\n"
            "    return value\n"
        )
        (wt / "clamp.py").write_text(good_clamp, encoding="utf-8")

        start = time.monotonic()
        rc, out = run_frozen_validator_cmd(self.val_path, wt, timeout_seconds=10.0)
        elapsed = time.monotonic() - start

        self.assertEqual(rc, 0, f"Validator failed unexpectedly: {out}")
        self.assertIn("VALIDATION PASSED", out)
        self.assertLess(elapsed, 5.0, "Validator execution should be fast")

    def test_normal_validator_failure_on_stub(self) -> None:
        """Validator exits non-zero on initial unimplemented stub."""
        wt = self.tmp / "wt_stub"
        wt.mkdir(parents=True, exist_ok=True)
        (wt / "clamp.py").write_text(CLAMP_STUB_SOURCE, encoding="utf-8")

        rc, out = run_frozen_validator_cmd(self.val_path, wt, timeout_seconds=10.0)
        self.assertNotEqual(rc, 0, "Validator should reject stub")
        self.assertTrue(
            "VALIDATION FAILED" in out or "VALIDATION ERROR" in out,
            f"Unexpected stub failure message: {out}",
        )

    def test_dummy_provider_secret_absent_in_child_environment(self) -> None:
        """Validator child process does not inherit provider secrets from os.environ."""
        wt = self.tmp / "wt_secret_probe"
        wt.mkdir(parents=True, exist_ok=True)

        probe_py = wt / "probe_secrets.py"
        probe_py.write_text(
            "import os, sys\n"
            "secrets = ['OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GEMINI_API_KEY', 'DEEPSEEK_API_KEY', 'TELEGRAM_BOT_TOKEN']\n"
            "found = [k for k in secrets if k in os.environ]\n"
            "if found:\n"
            "    print(f'LEAK_DETECTED: {found}', file=sys.stderr)\n"
            "    sys.exit(42)\n"
            "if os.environ.get('PYTHONDONTWRITEBYTECODE') != '1':\n"
            "    print('PYTHONDONTWRITEBYTECODE not set', file=sys.stderr)\n"
            "    sys.exit(43)\n"
            "print('CREDENTIAL_FREE_PASS')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )

        test_keys = {
            "OPENAI_API_KEY": "sk-dummy-openai-key-must-not-leak",
            "ANTHROPIC_API_KEY": "sk-ant-dummy-anthropic-key-must-not-leak",
            "GEMINI_API_KEY": "dummy-gemini-key-must-not-leak",
            "DEEPSEEK_API_KEY": "dummy-deepseek-key-must-not-leak",
            "TELEGRAM_BOT_TOKEN": "123456:secret-token-must-not-leak",
        }
        for k, v in test_keys.items():
            os.environ[k] = v

        try:
            rc, out = run_frozen_validator_cmd(probe_py, wt, timeout_seconds=5.0)
            self.assertEqual(rc, 0, f"Probe detected leaked secrets or missing config: {out}")
            self.assertIn("CREDENTIAL_FREE_PASS", out)
        finally:
            for k in test_keys:
                os.environ.pop(k, None)

    def test_hanging_candidate_bounded_and_reaped(self) -> None:
        """Hanging validator process is stopped within deadline and reaped."""
        wt = self.tmp / "wt_hang"
        wt.mkdir(parents=True, exist_ok=True)
        pid_file = wt / "child.pid"

        hang_py = wt / "hang_probe.py"
        hang_py.write_text(
            f"import os, sys, time\n"
            f"with open(r'{pid_file}', 'w') as f:\n"
            f"    f.write(str(os.getpid()))\n"
            f"sys.stdout.write('HANG_RUNNING\\n')\n"
            f"sys.stdout.flush()\n"
            f"time.sleep(120)\n",
            encoding="utf-8",
        )

        start = time.monotonic()
        rc, out = run_frozen_validator_cmd(hang_py, wt, timeout_seconds=0.25)
        elapsed = time.monotonic() - start

        self.assertEqual(rc, 124, f"Expected timeout exit code 124, got {rc}")
        self.assertIn("execution timed out", out)
        self.assertLess(elapsed, 2.0, "Timeout enforcement should be bounded")

        self.assertTrue(pid_file.is_file(), "Child PID file was not created")
        child_pid = int(pid_file.read_text().strip())

        # Verify child process is dead
        deadline = time.monotonic() + 1.5
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)

        self.assertFalse(alive, f"Child process {child_pid} was not reaped after timeout")

    def test_infinite_output_stopped_at_cap(self) -> None:
        """Validator candidate producing runaway output is actively capped and reaped."""
        wt = self.tmp / "wt_flood"
        wt.mkdir(parents=True, exist_ok=True)
        pid_file = wt / "flood.pid"

        flood_py = wt / "flood_probe.py"
        flood_py.write_text(
            f"import os, sys\n"
            f"with open(r'{pid_file}', 'w') as f:\n"
            f"    f.write(str(os.getpid()))\n"
            f"while True:\n"
            f"    sys.stdout.write('A' * 512)\n"
            f"    sys.stdout.flush()\n",
            encoding="utf-8",
        )

        rc, out = run_frozen_validator_cmd(flood_py, wt, max_bytes=1024, timeout_seconds=5.0)

        self.assertEqual(rc, 125, f"Expected overflow exit code 125, got {rc}")
        self.assertIn("output limit exceeded (1024 bytes cap)", out)
        # Ensure returned output buffer did not run away
        self.assertLessEqual(len(out.encode("utf-8")), 8192)

        self.assertTrue(pid_file.is_file(), "Child PID file was not created")
        child_pid = int(pid_file.read_text().strip())

        # Verify child process is dead
        deadline = time.monotonic() + 1.5
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)

        self.assertFalse(alive, f"Child process {child_pid} was not reaped after byte cap breach")

    def test_leader_exit_with_lingering_descendant_fails_and_cleans_group(self) -> None:
        """Zero-exit leader leaving lingering background child with DEVNULL stdio fails non-zero and cleans group."""
        wt = self.tmp / "wt_descendant"
        wt.mkdir(parents=True, exist_ok=True)
        marker = wt / "descendant_pids.json"

        program = wt / "fork_descendant.py"
        program.write_text(
            f"import json, os, subprocess, sys\n"
            f"from pathlib import Path\n"
            f"child = subprocess.Popen(\n"
            f"    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            f"    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
            f")\n"
            f"data = {{'leader': os.getpid(), 'child': child.pid}}\n"
            f"Path(r'{marker}').write_text(json.dumps(data))\n"
            f"print('validator leader finished')\n",
            encoding="utf-8",
        )

        rc, out = run_frozen_validator_cmd(program, wt, timeout_seconds=3.0)

        # Must return non-zero: cannot return success while background writer remains
        self.assertNotEqual(rc, 0, f"Validator falsely returned success despite lingering descendant: {out}")
        self.assertEqual(rc, 126)
        self.assertIn("lingering background descendant", out)
        self.assertIn("validator leader finished", out)

        self.assertTrue(marker.is_file(), "PIDs marker file was not created by leader")
        pids = json.loads(marker.read_text())
        child_pid = pids["child"]
        child_ident = ags._process_identity(child_pid)

        # Verify child process is provably dead after run_frozen_validator_cmd returned
        deadline = time.monotonic() + 1.5
        alive = True
        while time.monotonic() < deadline:
            if child_ident and ags._verify_process_identity(child_pid, child_ident) is False:
                alive = False
                break
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)

        self.assertFalse(alive, f"Descendant child PID {child_pid} was not reaped and is still alive!")

    def test_leader_exit_with_inherited_open_pipe_fails_and_cleans_group(self) -> None:
        """Zero-exit leader leaving background child with inherited open stdout fails non-zero without hanging."""
        wt = self.tmp / "wt_pipe_descendant"
        wt.mkdir(parents=True, exist_ok=True)
        marker = wt / "pipe_descendant_pids.json"

        program = wt / "fork_pipe_descendant.py"
        program.write_text(
            f"import json, os, subprocess, sys\n"
            f"from pathlib import Path\n"
            f"# Child inherits stdout pipe from leader\n"
            f"child = subprocess.Popen(\n"
            f"    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
            f")\n"
            f"data = {{'leader': os.getpid(), 'child': child.pid}}\n"
            f"Path(r'{marker}').write_text(json.dumps(data))\n"
            f"print('leader output with open pipe')\n",
            encoding="utf-8",
        )

        start = time.monotonic()
        rc, out = run_frozen_validator_cmd(program, wt, timeout_seconds=4.0)
        elapsed = time.monotonic() - start

        self.assertNotEqual(rc, 0, f"Validator falsely returned success despite inherited pipe descendant: {out}")
        self.assertEqual(rc, 126)
        self.assertIn("lingering background descendant", out)
        self.assertIn("leader output with open pipe", out)
        self.assertLess(elapsed, 2.5, "Leader exit with inherited pipe should fail promptly without waiting for deadline")

        self.assertTrue(marker.is_file(), "PIDs marker file was not created by leader")
        pids = json.loads(marker.read_text())
        child_pid = pids["child"]

        # Verify child process is dead
        deadline = time.monotonic() + 1.5
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)

        self.assertFalse(alive, f"Descendant child PID {child_pid} with inherited pipe was not reaped!")


if __name__ == "__main__":
    unittest.main()
