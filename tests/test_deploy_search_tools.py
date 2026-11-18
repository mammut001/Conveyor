"""Production deploys (scripts/deploy_vps.sh, run by .github/workflows/deploy.yml)
must keep Pi's code-search tools installed; see PR 229."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DeploySearchToolsTests(unittest.TestCase):
    def test_workflow_runs_deploy_vps(self) -> None:
        self.assertIn("scripts/deploy_vps.sh", (ROOT / ".github/workflows/deploy.yml").read_text())

    def test_both_deploy_scripts_install_rg_and_fd(self) -> None:
        for name in ("deploy_vps.sh", "deploy.sh"):
            text = (ROOT / "scripts" / name).read_text()
            self.assertIn("apt-get install -y -q ripgrep fd-find", text, name)
            self.assertIn("sudo -n env DEBIAN_FRONTEND=noninteractive", text, name)
            self.assertIn("command -v fdfind", text, name)

    def test_scripts_parse(self) -> None:
        for name in ("deploy_vps.sh", "deploy.sh"):
            subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)], check=True)


if __name__ == "__main__":
    unittest.main()
