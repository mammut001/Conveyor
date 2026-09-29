from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def test_shell_entrypoints_parse(self) -> None:
        for relative in ("scripts/bootstrap.sh", "scripts/install.sh", "scripts/conveyor"):
            with self.subTest(relative=relative):
                subprocess.run(
                    ["bash", "-n", str(ROOT / relative)],
                    check=True,
                    capture_output=True,
                    text=True,
                )

    def test_bootstrap_is_versioned_and_delegates_to_repo_installer(self) -> None:
        text = (ROOT / "scripts/bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn('REF="${CONVEYOR_VERSION:-main}"', text)
        self.assertIn("git clone", text)
        self.assertIn("checkout --quiet --detach", text)
        self.assertIn("scripts/install.sh", text)
        self.assertIn("CONVEYOR_INSTALL_REF", text)
        self.assertIn("/dev/tty", text)

    def test_installer_installs_dependencies_before_checking_them(self) -> None:
        text = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
        body = text.split("do_install() {", 1)[1].split("}", 1)[0]
        self.assertLess(body.index("install_system_deps"), body.index("check_deps"))
        self.assertIn("detect_service_identity", body)
        self.assertIn("install_cli", body)
        self.assertIn("run_smoke", body)

    def test_cli_has_expected_operator_commands(self) -> None:
        text = (ROOT / "scripts/conveyor").read_text(encoding="utf-8")
        for command in ("status", "logs", "restart", "doctor", "configure", "update", "uninstall", "version"):
            with self.subTest(command=command):
                self.assertIn(command, text)

    def test_configurator_honors_conveyor_dir_and_preserves_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("CONVEYOR_DIR")
            os.environ["CONVEYOR_DIR"] = tmp
            try:
                path = ROOT / "scripts/configure_env.py"
                spec = importlib.util.spec_from_file_location("configure_env_installer_test", path)
                self.assertIsNotNone(spec)
                self.assertIsNotNone(spec.loader)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                env_path = Path(tmp) / ".env"
                env_path.write_text("# keep me\nCONVEYOR_WEB_ENABLED=true\nCODEX_BIN=/old/codex\n", encoding="utf-8")
                module.write_env(
                    {
                        "TELEGRAM_BOT_TOKEN": "token",
                        "TELEGRAM_ALLOWED_USER_ID": "123",
                        "CODEX_WORKSPACE_ROOT": "/srv/repo",
                        "CODEX_BIN": "/usr/local/bin/codex",
                        "OPENAI_API_KEY": "",
                        "MINIMAX_API_KEY": "key",
                        "CODEX_TASK_ROOT": "/srv/conveyor",
                        "CODEX_TIMEOUT_SECONDS": "3600",
                        "TELEGRAM_PROGRESS_SECONDS": "20",
                    }
                )
                written = env_path.read_text(encoding="utf-8")
                self.assertIn("# keep me", written)
                self.assertIn("CONVEYOR_WEB_ENABLED=true", written)
                self.assertIn("CODEX_BIN=/usr/local/bin/codex", written)
                self.assertNotIn("CODEX_BIN=/old/codex", written)
                self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
            finally:
                if old is None:
                    os.environ.pop("CONVEYOR_DIR", None)
                else:
                    os.environ["CONVEYOR_DIR"] = old

    def test_installer_includes_tailscale_and_opt_out_flag(self) -> None:
        text = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
        self.assertIn("install_tailscale() {", text)
        self.assertIn("--no-tailscale", text)
        self.assertIn("CONVEYOR_NO_TAILSCALE", text)
        self.assertIn("tailscale set --operator", text)
        body = text.split("do_install() {", 1)[1].split("}", 1)[0]
        self.assertIn("install_tailscale", body)

    def test_configurator_tailscale_integration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("CONVEYOR_DIR")
            os.environ["CONVEYOR_DIR"] = tmp
            try:
                path = ROOT / "scripts/configure_env.py"
                spec = importlib.util.spec_from_file_location("configure_env_ts_test", path)
                self.assertIsNotNone(spec)
                self.assertIsNotNone(spec.loader)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                env_path = Path(tmp) / ".env"
                env_path.write_text("CONVEYOR_HANDOFF_TAILSCALE_SERVE=1\n", encoding="utf-8")
                managed = module._managed_lines({
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_ALLOWED_USER_ID": "123",
                    "CODEX_WORKSPACE_ROOT": "/srv/repo",
                    "CODEX_BIN": "/usr/local/bin/codex",
                    "OPENAI_API_KEY": "",
                    "MINIMAX_API_KEY": "key",
                    "CODEX_TASK_ROOT": "/srv/conveyor",
                    "CODEX_TIMEOUT_SECONDS": "3600",
                    "TELEGRAM_PROGRESS_SECONDS": "20",
                    "CONVEYOR_HANDOFF_TAILSCALE_SERVE": "1",
                })
                self.assertEqual(managed.get("CONVEYOR_HANDOFF_TAILSCALE_SERVE"), "1")
                module.write_env(managed)
                written = env_path.read_text(encoding="utf-8")
                self.assertIn("CONVEYOR_HANDOFF_TAILSCALE_SERVE=1", written)
            finally:
                if old is None:
                    os.environ.pop("CONVEYOR_DIR", None)
                else:
                    os.environ["CONVEYOR_DIR"] = old


if __name__ == "__main__":
    unittest.main()
