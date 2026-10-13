"""Credential isolation and Web Console secret store regression tests."""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import console_secrets
import subprocess_env
from web_console import WebConsoleHandler, WebConsoleServer


class TestSubprocessIsolation(unittest.TestCase):
    SAMPLE = {
        "PATH": "/usr/bin", "HOME": "/home/test", "LANG": "en_US.UTF-8",
        "LC_MESSAGES": "en_US", "TERM": "xterm", "TMPDIR": "/tmp",
        "TELEGRAM_BOT_TOKEN": "telegram-secret-value",
        "GITHUB_TOKEN": "github-secret-value",
        "GH_TOKEN": "gh-secret-value",
        "FEISHU_APP_SECRET": "feishu-secret-value",
        "DEEPSEEK_API_KEY": "deepseek-secret-value",
        "OPENAI_API_KEY": "openai-secret-value",
        "ANTHROPIC_API_KEY": "anthropic-secret-value",
        "AWS_ACCESS_KEY_ID": "aws-id",
        "EXTRA_SECRET": "unexpected-secret-value",
        "OTHER_API_KEY": "unexpected-key",
        "CONVEYOR_WEB_TOKEN": "web-secret-value",
        "OPENAI_BASE_URL": "https://gateway.invalid",
        "CODEX_RUNNER_HOME": "/opt/runner",
        "CODEX_LEAK_TOKEN": "bad-token",
    }

    def test_minimal_allowlist_no_implicit_creds(self):
        child = subprocess_env.build_child_env(self.SAMPLE)
        self.assertEqual(child["PATH"], "/usr/bin")
        self.assertEqual(child["LC_MESSAGES"], "en_US")
        self.assertEqual(child["OPENAI_BASE_URL"], "https://gateway.invalid")
        self.assertEqual(child["CODEX_RUNNER_HOME"], "/opt/runner")
        for name in (
            "TELEGRAM_BOT_TOKEN", "GITHUB_TOKEN", "GH_TOKEN", "FEISHU_APP_SECRET",
            "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "AWS_ACCESS_KEY_ID", "EXTRA_SECRET",
            "OTHER_API_KEY", "CONVEYOR_WEB_TOKEN", "CODEX_LEAK_TOKEN",
        ):
            self.assertNotIn(name, child)

    def test_granted_only_active_provider_key(self):
        child = subprocess_env.build_child_env(
            self.SAMPLE, grants={"DEEPSEEK_API_KEY", "TELEGRAM_BOT_TOKEN", "GITHUB_TOKEN"}
        )
        self.assertEqual(child["DEEPSEEK_API_KEY"], "deepseek-secret-value")
        self.assertNotIn("OPENAI_API_KEY", child)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", child)
        self.assertNotIn("GITHUB_TOKEN", child)

    def test_denylist_even_if_requested_as_extra(self):
        child = subprocess_env.build_child_env(
            self.SAMPLE, extra={"EXTRA_SECRET", "OTHER_API_KEY", "AWS_ACCESS_KEY_ID",
                                "CODEX_LEAK_TOKEN", "TELEGRAM_BOT_TOKEN"}
        )
        self.assertNotIn("EXTRA_SECRET", child)
        self.assertNotIn("OTHER_API_KEY", child)
        self.assertNotIn("CODEX_LEAK_TOKEN", child)
        self.assertNotIn("AWS_ACCESS_KEY_ID", child)

    def test_flag_off_preserves_legacy_subprocess_inheritance(self):
        settings = SimpleNamespace(env_isolation=False)
        with patch.dict(os.environ, {"CONVEYOR_ENV_ISOLATION": "1"}):
            self.assertIsNone(subprocess_env.utility_env(settings))
        self.assertFalse(subprocess_env.enabled(settings))

    def test_utility_children_have_no_sensitive_variables(self):
        settings = SimpleNamespace(env_isolation=True)
        env = subprocess_env.utility_env(settings, self.SAMPLE)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_claude_grants_are_scoped(self):
        self.assertEqual(subprocess_env.model_grants(None, claude=True),
                         {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"})
        with patch("provider_config.get_provider_config", return_value={"env_key": "DEEPSEEK_API_KEY"}):
            self.assertEqual(subprocess_env.model_grants(None), {"DEEPSEEK_API_KEY"})

    def test_managed_secrets_only_selected_grant(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.json"
            settings = SimpleNamespace(secrets_file=path)
            console_secrets.set_secret(settings, "OPENAI_API_KEY", "managed-openai")
            console_secrets.set_secret(settings, "DEEPSEEK_API_KEY", "managed-deepseek")
            env = subprocess_env.build_child_env({"PATH": "/bin"}, grants={"OPENAI_API_KEY"}, secrets_file=path)
            self.assertEqual(env["OPENAI_API_KEY"], "managed-openai")
            self.assertNotIn("DEEPSEEK_API_KEY", env)

    def test_startup_logs_only_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(env_isolation=True, secrets_file=Path(tmp) / "secrets.json")
            with patch("subprocess_env.model_grants", return_value={"OPENAI_API_KEY"}):
                with self.assertLogs("conveyor.security", level="INFO") as logs:
                    subprocess_env.log_startup_check(settings, self.SAMPLE)
            message = "\n".join(logs.output)
            self.assertIn("OPENAI_API_KEY", message)
            self.assertNotIn("openai-secret-value", message)
            self.assertNotIn("telegram-secret-value", message)


class TestSecretStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "data" / "secrets.json"
        self.settings = SimpleNamespace(secrets_file=self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_validation_atomic_mode_metadata_delete(self):
        with self.assertRaises(console_secrets.SecretStoreError):
            console_secrets.set_secret(self.settings, "lowercase", "abc")
        with self.assertRaises(console_secrets.SecretStoreError):
            console_secrets.set_secret(self.settings, "GOOD_NAME", "")
        secret_value = "VERY_PRIVATE_DO_NOT_LOG"
        meta = console_secrets.set_secret(self.settings, "GOOD_NAME", secret_value)
        self.assertEqual(set(meta), {"name", "set", "updated_at"})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(console_secrets.read_secret(self.settings, "GOOD_NAME"), secret_value)
        listing = json.dumps(console_secrets.list_secrets(self.settings))
        self.assertIn("GOOD_NAME", listing)
        self.assertNotIn(secret_value, listing)
        old_data = self.path.read_bytes()
        with patch("console_secrets.os.replace", side_effect=OSError("failure")):
            with self.assertRaises(OSError):
                console_secrets.set_secret(self.settings, "GOOD_NAME", "other-private")
        self.assertEqual(self.path.read_bytes(), old_data)
        self.assertFalse(any(p.name.startswith(".secrets-") for p in self.path.parent.iterdir()))
        self.assertTrue(console_secrets.delete_secret(self.settings, "GOOD_NAME"))
        self.assertIsNone(console_secrets.read_secret(self.settings, "GOOD_NAME"))
        self.assertEqual(console_secrets.list_secrets(self.settings)[0]["set"], False)
        self.assertFalse(console_secrets.delete_secret(self.settings, "GOOD_NAME"))

    def test_reject_insecure_files_and_symlink(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{}")
        os.chmod(self.path, 0o644)
        with self.assertRaises(console_secrets.SecretStoreError):
            console_secrets.list_secrets(self.settings)
        self.path.unlink()
        target = self.path.parent / "target"
        target.write_text("{}")
        self.path.symlink_to(target)
        with self.assertRaises(console_secrets.SecretStoreError):
            console_secrets.set_secret(self.settings, "GOOD_NAME", "private")


class TestWebSecretsApi(unittest.TestCase):
    TOKEN = "test-isolation-token-private"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.secrets_path = Path(cls.tmp.name) / "test-secrets.json"
        cls.control = SimpleNamespace(settings=SimpleNamespace(secrets_file=cls.secrets_path))
        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()
        cls.server = WebConsoleServer(
            ("127.0.0.1", 0), WebConsoleHandler, control=cls.control,
            loop=cls.loop, token=cls.TOKEN,
        )
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2)
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.loop_thread.join(timeout=2)
        cls.loop.close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, auth=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        headers = {"Authorization": "Bearer " + self.TOKEN} if auth else {}
        payload = json.dumps(body).encode() if body is not None else None
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, headers)
        res = conn.getresponse()
        raw = res.read().decode()
        status = res.status
        conn.close()
        return status, json.loads(raw)

    def test_auth_validation_no_echo_delete(self):
        value = "my-test-ultra-private-credential"
        for method, path, body in (
            ("GET", "/api/secrets", None),
            ("POST", "/api/secrets", {"name": "VALID_NAME", "value": value}),
            ("DELETE", "/api/secrets/VALID_NAME", None),
        ):
            self.assertEqual(self.request(method, path, body, auth=False)[0], 401)
        self.assertFalse(self.secrets_path.exists())
        for name in ("x", "A", "BAD-DASH", "A" * 65, "1_INVALID"):
            status, result = self.request("POST", "/api/secrets", {"name": name, "value": value})
            self.assertEqual(status, 400)
            self.assertNotIn(value, json.dumps(result))
        status, result = self.request("POST", "/api/secrets", {"name": "VALID_NAME", "value": value})
        self.assertEqual(status, 201)
        self.assertNotIn(value, json.dumps(result))
        self.assertEqual(stat.S_IMODE(self.secrets_path.stat().st_mode), 0o600)
        status, result = self.request("GET", "/api/secrets")
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["name"], "VALID_NAME")
        self.assertTrue(result["items"][0]["set"])
        self.assertNotIn(value, json.dumps(result))
        status, result = self.request("DELETE", "/api/secrets/VALID_NAME")
        self.assertEqual(status, 200)
        self.assertNotIn(value, json.dumps(result))
        status, result = self.request("GET", "/api/secrets")
        self.assertEqual(status, 200)
        self.assertFalse(result["items"][0]["set"])
