from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from provider_config import (
    get_provider_config,
    get_saved_provider_credentials,
    refresh_provider_env,
    save_provider_config,
)
from runner.chat_client import (
    ChatConfig,
    ChatError,
    DEFAULT_BASE_URL,
    _clean_chat_error,
    complete_chat,
    config_from_settings,
)


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


class ChatTierFallbackTests(unittest.TestCase):
    def test_fallback_used_when_only_provider_is_saved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / ".codex"
            codex.mkdir()
            env = root / ".env"
            settings = SimpleNamespace(codex_memory_root=codex, chat_mode="")
            with patch.dict(os.environ, {"CONVEYOR_ENV_FILE": str(env)}, clear=True):
                save_provider_config(settings, {
                    "provider_id": "deepseek",
                    "provider_name": "DeepSeek",
                    "model": "deepseek-chat",
                    "base_url": "https://api.deepseek.com/v1",
                    "wire_api": "chat",
                    "reasoning_effort": "minimal",
                    "env_key": "DEEPSEEK_API_KEY",
                    "api_key": "sk-secret-fallback-key-9999",
                })
                # Mode unset
                config = config_from_settings(settings)
                self.assertIsNotNone(config)
                self.assertEqual(config.base_url, "https://api.deepseek.com/v1")
                self.assertEqual(config.model, "deepseek-chat")
                self.assertEqual(config.api_key, "sk-secret-fallback-key-9999")

                # Mode auto also uses saved provider
                settings.chat_mode = "auto"
                config = config_from_settings(settings)
                self.assertIsNotNone(config)
                self.assertEqual(config.base_url, "https://api.deepseek.com/v1")
                self.assertEqual(config.model, "deepseek-chat")

    def test_explicit_off_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / ".codex"
            codex.mkdir()
            env = root / ".env"
            settings = SimpleNamespace(codex_memory_root=codex, chat_mode="")
            with patch.dict(os.environ, {"CONVEYOR_ENV_FILE": str(env)}, clear=True):
                save_provider_config(settings, {
                    "provider_id": "deepseek",
                    "provider_name": "DeepSeek",
                    "model": "deepseek-chat",
                    "base_url": "https://api.deepseek.com/v1",
                    "wire_api": "chat",
                    "reasoning_effort": "minimal",
                    "env_key": "DEEPSEEK_API_KEY",
                    "api_key": "sk-secret-fallback-key-9999",
                })
                # Explicit off in settings
                settings.chat_mode = "off"
                self.assertIsNone(config_from_settings(settings))

                # Explicit off in env
                settings.chat_mode = "auto"
                with patch.dict(os.environ, {"CONVEYOR_CHAT_MODE": "off", "CONVEYOR_ENV_FILE": str(env)}, clear=True):
                    self.assertIsNone(config_from_settings(settings))

    def test_explicit_chat_env_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / ".codex"
            codex.mkdir()
            env = root / ".env"
            settings = SimpleNamespace(codex_memory_root=codex, chat_mode="")
            with patch.dict(os.environ, {"CONVEYOR_ENV_FILE": str(env)}, clear=True):
                save_provider_config(settings, {
                    "provider_id": "deepseek",
                    "provider_name": "DeepSeek",
                    "model": "deepseek-chat",
                    "base_url": "https://api.deepseek.com/v1",
                    "wire_api": "chat",
                    "reasoning_effort": "minimal",
                    "env_key": "DEEPSEEK_API_KEY",
                    "api_key": "sk-secret-fallback-key-9999",
                })
                # Explicit CONVEYOR_CHAT_* key+model set with custom base_url
                with patch.dict(os.environ, {
                    "CONVEYOR_ENV_FILE": str(env),
                    "CONVEYOR_CHAT_API_KEY": "sk-explicit-chat-key",
                    "CONVEYOR_CHAT_MODEL": "explicit-chat-model",
                    "CONVEYOR_CHAT_BASE_URL": "https://explicit-chat.example.com/v1",
                }, clear=True):
                    config = config_from_settings(settings)
                    self.assertIsNotNone(config)
                    self.assertEqual(config.base_url, "https://explicit-chat.example.com/v1")
                    self.assertEqual(config.model, "explicit-chat-model")
                    self.assertEqual(config.api_key, "sk-explicit-chat-key")

                # Explicit key+model without base_url falls back to DEFAULT_BASE_URL
                with patch.dict(os.environ, {
                    "CONVEYOR_ENV_FILE": str(env),
                    "CONVEYOR_CHAT_API_KEY": "sk-explicit-chat-key",
                    "CONVEYOR_CHAT_MODEL": "explicit-chat-model",
                }, clear=True):
                    config = config_from_settings(settings)
                    self.assertIsNotNone(config)
                    self.assertEqual(config.base_url, DEFAULT_BASE_URL.rstrip("/"))
                    self.assertEqual(config.model, "explicit-chat-model")
                    self.assertEqual(config.api_key, "sk-explicit-chat-key")

    def test_no_provider_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / ".codex"
            codex.mkdir()
            env = root / ".env"
            settings = SimpleNamespace(codex_memory_root=codex, chat_mode="")
            with patch.dict(os.environ, {"CONVEYOR_ENV_FILE": str(env)}, clear=True):
                self.assertIsNone(config_from_settings(settings))

    def test_key_never_appears_in_logs_or_errors(self) -> None:
        secret_key = "sk-ultra-sensitive-key-9876543210"
        config = ChatConfig(
            base_url="https://api.example.com/v1",
            api_key=secret_key,
            model="test-model",
        )
        # 1. repr and str of ChatConfig must not expose the key
        self.assertNotIn(secret_key, repr(config))
        self.assertNotIn(secret_key, str(config))

        # 2. _clean_chat_error redacts the key
        error_msg = f"Failed to connect using authorization Bearer {secret_key} to endpoint"
        cleaned = _clean_chat_error(error_msg, secret_key)
        self.assertNotIn(secret_key, cleaned)
        self.assertIn("[REDACTED]", cleaned)

        # 3. Exception in complete_chat cleans key
        import asyncio
        from unittest.mock import AsyncMock

        with patch("httpx.AsyncClient.post", side_effect=RuntimeError(f"HTTP failed: auth={secret_key}")):
            with self.assertRaises(ChatError) as cm:
                asyncio.run(complete_chat(config, [{"role": "user", "content": "hi"}]))
            self.assertNotIn(secret_key, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
