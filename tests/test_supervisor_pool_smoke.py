"""Runs scripts/supervisor_pool_smoke.py: the pool executor through the real
Supervisor subprocess path, with fake claude/agy binaries."""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "scripts" / "supervisor_pool_smoke.py"


@unittest.skipUnless(os.name == "posix", "fake executables are POSIX shebang scripts")
class SupervisorPoolSmokeTest(unittest.TestCase):
    def test_pool_smoke_end_to_end(self) -> None:
        res = subprocess.run(
            [sys.executable, str(SMOKE)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        output = res.stdout + res.stderr
        self.assertEqual(res.returncode, 0, output)
        self.assertIn("supervisor_pool_smoke: PASS", res.stdout, output)
        self.assertNotIn("FAIL:", res.stdout, output)


if __name__ == "__main__":
    unittest.main()
