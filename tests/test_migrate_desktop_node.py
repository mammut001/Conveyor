"""Control-plane desktop node migration keeps secrets and file mode."""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "migrate-desktop-node-id.sh"


class MigrateDesktopNodeTests(unittest.TestCase):
    def test_backup_is_restrictive_and_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            secret = "supersecretvalue"
            env.write_text(
                f"SECRET={secret}\n"
                "CONVEYOR_DESKTOP_NODE_ID=macbook-payton\n"
                'CONVEYOR_DESKTOP_NODE_NAME="Payton MacBook"\n',
                encoding="utf-8",
            )
            os.chmod(env, 0o640)
            result = subprocess.run(
                [str(SCRIPT), str(env)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(secret, result.stdout)
            self.assertNotIn(secret, result.stderr)
            text = env.read_text(encoding="utf-8")
            self.assertIn("CONVEYOR_DESKTOP_NODE_ID=vps-desktop\n", text)
            self.assertIn("CONVEYOR_DESKTOP_NODE_NAME=VPS desktop\n", text)
            self.assertIn(f"SECRET={secret}\n", text)
            self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o640)
            backups = list(Path(tmp).glob(".env.bak-desktop-node-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
            self.assertIn("macbook-payton", backups[0].read_text(encoding="utf-8"))

    def test_duplicate_or_custom_id_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            original = (
                "CONVEYOR_DESKTOP_NODE_ID=custom-server\n"
                "CONVEYOR_DESKTOP_NODE_NAME=Payton MacBook\n"
            )
            env.write_text(original, encoding="utf-8")
            result = subprocess.run([str(SCRIPT), str(env)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(env.read_text(encoding="utf-8"), original)
            self.assertEqual(list(Path(tmp).glob(".env.bak-desktop-node-*")), [])

            env.write_text(
                "CONVEYOR_DESKTOP_NODE_ID=macbook-payton\n"
                "CONVEYOR_DESKTOP_NODE_ID=macbook-payton\n",
                encoding="utf-8",
            )
            result = subprocess.run([str(SCRIPT), str(env)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(env.read_text(encoding="utf-8").count("macbook-payton"), 2)
            self.assertNotIn("secret", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
