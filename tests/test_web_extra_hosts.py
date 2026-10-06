"""Extra private listen addresses for the Web Console (VPN access from a phone)."""
from __future__ import annotations

import asyncio
import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from web_console import (
    WebConsoleHandler,
    WebConsoleServer,
    _validate_extra_host,
    start_extra_listeners,
    validate_web_config,
)

TOKEN = "t" * 40


class ValidationTests(unittest.TestCase):
    def test_private_addresses_are_accepted(self) -> None:
        for host in ("10.10.0.1", "192.168.1.5", "100.64.0.7", "127.0.0.1", "fd00::1"):
            _validate_extra_host(host)

    def test_wildcard_public_and_names_are_rejected(self) -> None:
        for host in ("0.0.0.0", "::", "8.8.8.8", "2606:4700:4700::1111", "example.com", "224.0.0.1"):
            with self.assertRaises(RuntimeError, msg=host):
                _validate_extra_host(host)

    def test_web_config_validation_covers_extra_hosts(self) -> None:
        settings = SimpleNamespace(
            conveyor_web_enabled=True, conveyor_web_token=TOKEN, conveyor_web_port=8787,
            conveyor_web_extra_hosts=("0.0.0.0",),
        )
        with self.assertRaises(RuntimeError):
            validate_web_config(settings)

    def test_env_is_parsed_into_a_tuple(self) -> None:
        from unittest import mock

        from config import load_runtime_settings

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "CODEX_WORKSPACE_ROOT": tmp, "CODEX_TASK_ROOT": f"{tmp}/tasks", "CODEX_MEMORY_ROOT": f"{tmp}/mem",
                "CONVEYOR_WEB_EXTRA_HOSTS": " 10.10.0.1, ,100.64.0.7 ",
            }
            with mock.patch.dict("os.environ", env, clear=True):
                settings = load_runtime_settings(env_file="")
        self.assertEqual(settings.conveyor_web_extra_hosts, ("10.10.0.1", "100.64.0.7"))


class ExtraListenerTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            probe = socket.create_server(("::1", 0), family=socket.AF_INET6)
            probe.close()
        except OSError:
            self.skipTest("IPv6 loopback is not available")
        self.loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        loop_thread.start()
        self.server = WebConsoleServer(
            ("127.0.0.1", 0), WebConsoleHandler, control=SimpleNamespace(), loop=self.loop, token=TOKEN,
        )
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.port = self.server.server_address[1]

        def stop() -> None:
            self.server.shutdown()
            self.server.server_close()
            self.loop.call_soon_threadsafe(self.loop.stop)
            loop_thread.join(timeout=2)
            thread.join(timeout=2)

        self.addCleanup(stop)

    def get(self, host: str, path: str, authorized: bool):
        conn = http.client.HTTPConnection(host, self.port, timeout=5)
        conn.request("GET", path, headers={"Authorization": f"Bearer {TOKEN}"} if authorized else {})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, body

    def test_extra_address_serves_the_same_console_with_the_same_auth(self) -> None:
        settings = SimpleNamespace(
            conveyor_web_host="127.0.0.1", conveyor_web_port=self.port,
            conveyor_web_extra_hosts=("::1", "127.0.0.1"),
        )
        threads = start_extra_listeners(self.server, settings)
        self.assertEqual(len(threads), 1)  # the primary address is not bound twice

        deadline = time.monotonic() + 5
        while True:
            try:
                status, body = self.get("::1", "/api/health", authorized=False)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

        self.assertEqual(self.get("::1", "/api/screen/status", authorized=False)[0], 401)
        status, body = self.get("::1", "/api/screen/status", authorized=True)
        self.assertEqual(status, 200)
        # Same server object: the extra address sees the one shared LiveScreen.
        self.assertFalse(json.loads(body)["enabled"])
        self.assertEqual(self.get("127.0.0.1", "/api/screen/status", authorized=True)[0], 200)


if __name__ == "__main__":
    unittest.main()
