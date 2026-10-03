from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Settings
from runner.claude_code import ClaudeCodeBackend
from runner.core import CodexRunner
from security.secrets import child_env_from, scope_provider_keys


def _make_settings(root: Path, **kwargs) -> Settings:
    state_root = root / "state"
    defaults = {
        "telegram_bot_token": "unused",
        "telegram_allowed_user_id": 1,
        "codex_workspace_root": root,
        "codex_bin": "codex",
        "codex_task_root": state_root / "tasks",
        "codex_model": None,
        "codex_timeout_seconds": 5,
        "telegram_progress_seconds": 1,
        "codex_retry_429_delays_seconds": (),
        "codex_memory_root": state_root / "memory",
        "user_timezone": "UTC",
    }
    defaults.update(kwargs)
    return Settings(**defaults)


class ChildEnvScopingTests(unittest.TestCase):
    def test_scope_provider_keys_keeps_selected_and_drops_others(self) -> None:
        raw_env = {
            "HOME": "/home/user",
            "PATH": "/usr/bin",
            "OPENAI_API_KEY": "sk-openai-secret",
            "OPENAI_BASE_URL": "https://api.openai.com/v1",
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "ANTHROPIC_MODEL": "claude-3-5-sonnet",
            "MINIMAX_API_KEY": "minimax-secret",
            "AZURE_OPENAI_API_KEY": "azure-secret",
            "DEEPSEEK_API_KEY": "deepseek-secret",
            "CODEX_TELEGRAM_JOB": "1",
        }
        scoped, dropped = scope_provider_keys(raw_env, keep={"OPENAI_API_KEY"})
        self.assertIn("OPENAI_API_KEY", scoped)
        self.assertEqual(scoped["OPENAI_API_KEY"], "sk-openai-secret")
        self.assertIn("OPENAI_BASE_URL", scoped)
        self.assertIn("ANTHROPIC_MODEL", scoped)
        self.assertIn("HOME", scoped)
        self.assertIn("PATH", scoped)
        self.assertIn("CODEX_TELEGRAM_JOB", scoped)

        # Dropped credential keys
        self.assertNotIn("ANTHROPIC_API_KEY", scoped)
        self.assertNotIn("MINIMAX_API_KEY", scoped)
        self.assertNotIn("AZURE_OPENAI_API_KEY", scoped)
        self.assertNotIn("DEEPSEEK_API_KEY", scoped)

        self.assertEqual(
            dropped,
            ["ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY", "DEEPSEEK_API_KEY", "MINIMAX_API_KEY"],
        )

    def test_scope_provider_keys_explicit_prefixes_win(self) -> None:
        raw_env = {
            "OPENAI_API_KEY": "sk-openai-secret",
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "MINIMAX_API_KEY": "minimax-secret",
        }
        # Explicitly allowed ANTHROPIC_ prefix should preserve ANTHROPIC_API_KEY even if not in keep
        scoped, dropped = scope_provider_keys(
            raw_env,
            keep={"OPENAI_API_KEY"},
            explicit_prefixes={"ANTHROPIC_"},
        )
        self.assertIn("OPENAI_API_KEY", scoped)
        self.assertIn("ANTHROPIC_API_KEY", scoped)
        self.assertNotIn("MINIMAX_API_KEY", scoped)
        self.assertEqual(dropped, ["MINIMAX_API_KEY"])

    def test_flag_off_preserves_exact_legacy_child_env(self) -> None:
        env_sample = {
            "HOME": "/home/user",
            "PATH": "/bin",
            "OPENAI_API_KEY": "sk-openai",
            "ANTHROPIC_API_KEY": "sk-ant",
            "MINIMAX_API_KEY": "minimax-key",
            "DEEPSEEK_API_KEY": "deepseek-key",
        }
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, env_sample, clear=True):
            settings = _make_settings(Path(temp), child_env_scope_provider_keys=False)
            runner = CodexRunner(settings)
            child = runner._child_env()

            # Baseline legacy behavior:
            legacy = child_env_from(env_sample)
            legacy["CODEX_TELEGRAM_JOB"] = "1"
            legacy["CODEX_RUNNER_HOME"] = str(Path(runner._child_env()["CODEX_RUNNER_HOME"]))

            self.assertEqual(child, legacy)
            self.assertIn("ANTHROPIC_API_KEY", child)
            self.assertIn("MINIMAX_API_KEY", child)
            self.assertIn("DEEPSEEK_API_KEY", child)

    def test_flag_on_scopes_codex_backend_keys_and_logs_names_only(self) -> None:
        env_sample = {
            "HOME": "/home/user",
            "PATH": "/bin",
            "OPENAI_API_KEY": "sk-openai-val",
            "ANTHROPIC_API_KEY": "sk-ant-val",
            "MINIMAX_API_KEY": "minimax-val",
            "OPENAI_BASE_URL": "https://api.openai.com/v1",
        }
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, env_sample, clear=True):
            settings = _make_settings(Path(temp), child_env_scope_provider_keys=True)
            runner = CodexRunner(settings)

            with self.assertLogs("conveyor.security", level="INFO") as log_cm:
                child = runner._child_env()

            self.assertIn("OPENAI_API_KEY", child)
            self.assertIn("OPENAI_BASE_URL", child)
            self.assertNotIn("ANTHROPIC_API_KEY", child)
            self.assertNotIn("MINIMAX_API_KEY", child)

            # Log check: names logged, but NEVER values
            log_output = "\n".join(log_cm.output)
            self.assertIn("ANTHROPIC_API_KEY", log_output)
            self.assertIn("MINIMAX_API_KEY", log_output)
            self.assertNotIn("sk-ant-val", log_output)
            self.assertNotIn("minimax-val", log_output)

    def test_claude_backend_keeps_anthropic_credentials_only(self) -> None:
        env_sample = {
            "HOME": "/home/user",
            "PATH": "/bin",
            "OPENAI_API_KEY": "sk-openai-val",
            "ANTHROPIC_API_KEY": "sk-ant-val",
            "ANTHROPIC_AUTH_TOKEN": "ant-token-val",
            "MINIMAX_API_KEY": "minimax-val",
            "ANTHROPIC_MODEL": "claude-3-opus",
        }
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, env_sample, clear=True):
            settings = _make_settings(Path(temp), child_env_scope_provider_keys=True)
            backend = ClaudeCodeBackend(settings)
            child = backend._child_env()

            self.assertIn("ANTHROPIC_API_KEY", child)
            self.assertIn("ANTHROPIC_AUTH_TOKEN", child)
            self.assertIn("ANTHROPIC_MODEL", child)
            self.assertNotIn("OPENAI_API_KEY", child)
            self.assertNotIn("MINIMAX_API_KEY", child)

    def test_provider_config_error_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            settings = _make_settings(Path(temp), child_env_scope_provider_keys=True)
            runner = CodexRunner(settings)

            with patch("provider_config.get_provider_config", side_effect=RuntimeError("disk unreadable")):
                with self.assertLogs("conveyor.worktree", level="WARNING") as cm:
                    keys = runner._provider_credential_keys()

            self.assertEqual(keys, {"OPENAI_API_KEY"})
            log_output = "\n".join(cm.output)
            self.assertIn("RuntimeError", log_output)
            self.assertNotIn("disk unreadable", log_output)


if __name__ == "__main__":
    unittest.main()
