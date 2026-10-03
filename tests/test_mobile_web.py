from __future__ import annotations

import asyncio
import http.client
import json
import threading
import unittest
from unittest.mock import patch

from config import Settings, load_runtime_settings
from web_console import WebConsoleHandler, WebConsoleServer


TOKEN = "test-token-0123456789-abcdefghijklmnopqrstuvwxyz"


def _make_settings(**kwargs) -> Settings:
    from pathlib import Path
    root = Path("/tmp/conveyor-test-mobile-root")
    defaults = {
        "telegram_bot_token": "unused",
        "telegram_allowed_user_id": 1,
        "codex_workspace_root": root,
        "codex_bin": "codex",
        "codex_task_root": root / "tasks",
        "codex_model": None,
        "codex_timeout_seconds": 5,
        "telegram_progress_seconds": 1,
        "codex_retry_429_delays_seconds": (),
        "codex_memory_root": root / "memory",
        "user_timezone": "UTC",
    }
    defaults.update(kwargs)
    return Settings(**defaults)


class DummyControl:
    runner = object()

    def __init__(self, settings: Settings):
        self.settings = settings

    def system_status(self):
        return {
            "uptime_seconds": 1,
            "load_average": [0.0, 0.0, 0.0],
            "cpu_count": 2,
            "memory": {"total": None, "available": None},
            "disk": {"total": 1, "used": 0, "free": 1},
            "queue": {"depth": 0, "paused": False, "states": {}},
            "channels": {
                "telegram": {"configured": False},
                "feishu": {"configured": False},
            },
            "nodes": [],
            "features": {
                "long_term_memory": False,
                "routines": False,
                "webhooks": False,
                "approval_inbox": False,
                "skills": False,
                "provider_key_scoping": self.settings.child_env_scope_provider_keys,
                "mobile_ui": self.settings.web_mobile_ui,
            },
        }

    def artifact_path(self, _artifact_id):
        return None


class MobileWebTests(unittest.TestCase):
    BASE_ENV = {
        "CODEX_WORKSPACE_ROOT": "/tmp/test-ws",
        "CODEX_TASK_ROOT": "/tmp/test-tasks",
        "CODEX_MEMORY_ROOT": "/tmp/test-mem",
    }

    def test_config_flag_default_off(self):
        with patch.dict("os.environ", self.BASE_ENV, clear=True):
            settings = load_runtime_settings(env_file="")
            self.assertFalse(settings.web_mobile_ui)

    def test_config_flag_parsing(self):
        env_true = {**self.BASE_ENV, "CONVEYOR_WEB_MOBILE_UI": "true"}
        with patch.dict("os.environ", env_true, clear=True):
            settings = load_runtime_settings(env_file="")
            self.assertTrue(settings.web_mobile_ui)

        env_one = {**self.BASE_ENV, "CONVEYOR_WEB_MOBILE_UI": "1"}
        with patch.dict("os.environ", env_one, clear=True):
            settings = load_runtime_settings(env_file="")
            self.assertTrue(settings.web_mobile_ui)

        env_false = {**self.BASE_ENV, "CONVEYOR_WEB_MOBILE_UI": "false"}
        with patch.dict("os.environ", env_false, clear=True):
            settings = load_runtime_settings(env_file="")
            self.assertFalse(settings.web_mobile_ui)

    def test_features_mobile_ui_reported_in_system_status(self):
        env_true = {**self.BASE_ENV, "CONVEYOR_WEB_MOBILE_UI": "true"}
        with patch.dict("os.environ", env_true, clear=True):
            settings = load_runtime_settings(env_file="")
            control = DummyControl(settings)
            status = control.system_status()
            self.assertTrue(status["features"]["mobile_ui"])

        with patch.dict("os.environ", self.BASE_ENV, clear=True):
            settings = load_runtime_settings(env_file="")
            control = DummyControl(settings)
            status = control.system_status()
            self.assertFalse(status["features"]["mobile_ui"])


class WebConsoleManifestAndIconsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()
        settings = _make_settings(web_mobile_ui=True)
        cls.server = WebConsoleServer(
            ("127.0.0.1", 0),
            WebConsoleHandler,
            control=DummyControl(settings),
            loop=cls.loop,
            token=TOKEN,
        )
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.loop_thread.join(timeout=2)
        cls.server_thread.join(timeout=2)

    def _get(self, path: str, authorized: bool = False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if authorized:
            headers["Authorization"] = f"Bearer {TOKEN}"
        conn.request("GET", path, headers=headers)
        res = conn.getresponse()
        data = res.read()
        content_type = res.getheader("Content-Type", "")
        status = res.status
        conn.close()
        return status, content_type, data

    def test_manifest_served_without_auth(self):
        status, content_type, data = self._get("/manifest.webmanifest", authorized=False)
        self.assertEqual(status, 200)
        self.assertIn("application/manifest+json", content_type)
        manifest = json.loads(data.decode("utf-8"))
        self.assertEqual(manifest.get("name"), "Conveyor")
        self.assertEqual(manifest.get("short_name"), "Conveyor")
        self.assertEqual(manifest.get("start_url"), "/")
        self.assertEqual(manifest.get("display"), "standalone")
        self.assertEqual(manifest.get("theme_color"), "#f5f6f7")
        self.assertIsInstance(manifest.get("icons"), list)
        icon_srcs = [icon["src"] for icon in manifest["icons"]]
        self.assertIn("/icon.svg", icon_srcs)
        self.assertIn("/icon-192.png", icon_srcs)
        self.assertIn("/icon-512.png", icon_srcs)

    def test_icons_served_without_auth(self):
        # SVG icon
        status, content_type, data = self._get("/icon.svg", authorized=False)
        self.assertEqual(status, 200)
        self.assertIn("image/svg+xml", content_type)
        self.assertTrue(len(data) > 0)
        self.assertIn(b"<svg", data)

        # 192x192 PNG icon
        status, content_type, data = self._get("/icon-192.png", authorized=False)
        self.assertEqual(status, 200)
        self.assertIn("image/png", content_type)
        self.assertTrue(len(data) > 0)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")

        # 512x512 PNG icon
        status, content_type, data = self._get("/icon-512.png", authorized=False)
        self.assertEqual(status, 200)
        self.assertIn("image/png", content_type)
        self.assertTrue(len(data) > 0)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
