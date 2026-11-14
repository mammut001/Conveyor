"""Frontend regression fixture for discussion drafts and failed-task retry.

Runs the Node checker. It does not start a browser or build the web bundle.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DraftHandoffFixtureTests(unittest.TestCase):
    def test_node_fixture_covers_draft_retry_and_new_conversation_403(self) -> None:
        script = ROOT / "tests" / "fixtures" / "check_draft_handoff.mjs"
        proc = subprocess.run(
            ["node", "--experimental-strip-types", str(script)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + "\n" + proc.stderr)
        self.assertIn("draft handoff fixture ok", proc.stdout)


if __name__ == "__main__":
    unittest.main()
