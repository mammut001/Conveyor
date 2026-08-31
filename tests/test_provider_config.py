from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from provider_config import get_provider_config, refresh_provider_env, save_provider_config


class ProviderConfigTests(unittest.TestCase):
    def test_save_preserves_unrelated_toml_and_masks_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / ".codex"
            codex.mkdir()
            (codex / "config.toml").write_text(
                'model = "old"\nmodel_provider = "old"\n\n[model_providers.old]\nname = "Old"\nbase_url = "https://old.example/v1"\nenv_key = "OPENAI_API_KEY"\nwire_api = "responses"\n\n[projects."/srv/repo"]\ntrust_level = "trusted"\n'
            )
            env = root / ".env"
            env.write_text("KEEP_ME=yes\nOPENAI_API_KEY=old-secret\n")
            os.chmod(env, 0o600)
            settings = SimpleNamespace(codex_memory_root=codex)
            with patch.dict(os.environ, {"CONVEYOR_ENV_FILE": str(env)}, clear=False):
                result = save_provider_config(settings, {
                    "provider_id": "deepseek", "provider_name": "DeepSeek",
                    "model": "deepseek-chat", "base_url": "https://api.deepseek.com/v1",
                    "wire_api": "chat", "reasoning_effort": "minimal",
                    "env_key": "OPENAI_API_KEY", "api_key": "sk-new-secret-1234",
                })
            text = (codex / "config.toml").read_text()
            self.assertIn('[model_providers.deepseek]', text)
            self.assertIn('[model_providers.old]', text)
            self.assertIn('[projects."/srv/repo"]', text)
            self.assertEqual(result["api_key_hint"], "••••1234")
            self.assertNotIn("sk-new-secret", repr(result))
            self.assertIn("KEEP_ME=yes", env.read_text())
            self.assertIn("OPENAI_API_KEY=sk-new-secret-1234", env.read_text())
            self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)

    def test_blank_key_keeps_existing_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); codex = root / ".codex"; codex.mkdir()
            (codex / "config.toml").write_text('model = "m"\nmodel_provider = "p"\n[model_providers.p]\nname = "P"\nbase_url = "https://p.example/v1"\nenv_key = "OPENAI_API_KEY"\nwire_api = "responses"\n')
            env = root / ".env"; env.write_text("OPENAI_API_KEY=existing-key\n")
            with patch.dict(os.environ, {"CONVEYOR_ENV_FILE": str(env)}, clear=False):
                save_provider_config(SimpleNamespace(codex_memory_root=codex), {
                    "provider_id": "p", "provider_name": "P", "model": "m2",
                    "base_url": "https://p.example/v1", "wire_api": "responses",
                    "reasoning_effort": "low", "env_key": "OPENAI_API_KEY", "api_key": "",
                })
            self.assertIn("OPENAI_API_KEY=existing-key", env.read_text())

    def test_rejects_remote_plain_http_and_refreshes_allowed_provider_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); codex = root / ".codex"; codex.mkdir()
            env = root / ".env"; env.write_text("DEEPSEEK_API_KEY=dynamic-key\nTELEGRAM_BOT_TOKEN=do-not-copy\n")
            with patch.dict(os.environ, {"CONVEYOR_ENV_FILE": str(env)}, clear=False):
                self.assertEqual(refresh_provider_env(), {"DEEPSEEK_API_KEY": "dynamic-key"})
                with self.assertRaisesRegex(ValueError, "https"):
                    save_provider_config(SimpleNamespace(codex_memory_root=codex), {
                        "provider_id": "p", "provider_name": "P", "model": "m",
                        "base_url": "http://provider.example/v1", "wire_api": "responses",
                        "reasoning_effort": "minimal", "env_key": "OPENAI_API_KEY", "api_key": "12345678",
                    })


if __name__ == "__main__":
    unittest.main()
