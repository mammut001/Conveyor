"""P3-5 PWA, Web Push and authenticated API regression tests."""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import web_push
from config import load_runtime_settings
from web_console import WebConsoleHandler, WebConsoleServer

TOKEN = "pwa-test-token-" + "0123456789abcdef" * 3
SUB = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/test-endpoint",
    "keys": {"p256dh": "A" * 86, "auth": "B" * 22},
}


class PushLogicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = SimpleNamespace(pwa_enabled=True, codex_memory_root=Path(self.temp.name),
                                        operator_name="operator-A")
        # Offline fake sender tests must not depend on public DNS availability.
        dns = patch.object(web_push.socket, "getaddrinfo", return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.72.202", 443)),
        ])
        dns.start()
        self.addCleanup(dns.stop)

    def test_off_is_noop_and_does_not_create_database(self):
        self.settings.pwa_enabled = False
        with self.assertRaises(ValueError):
            web_push.subscribe(self.settings, SUB)
        deliveries = []
        self.assertEqual(web_push.notify_pending(self.settings, "token1",
                         sender=lambda *args: deliveries.append(args)), 0)
        web_push.queue_notification(self.settings, "token1")
        web_push.unsubscribe(self.settings, "a" * 64)
        self.assertEqual(deliveries, [])
        self.assertFalse(web_push._db_path(self.settings).exists())

    def test_fake_sender_payload_has_only_generic_title_and_id(self):
        sub_id = web_push.subscribe(self.settings, SUB)
        self.assertEqual(len(sub_id), 64)
        outbound = []
        self.assertEqual(web_push.notify_pending(self.settings, "draft123", "draft",
                         sender=lambda subscription, message: outbound.append((subscription, message))), 1)
        self.assertEqual(len(outbound), 1)
        self.assertEqual(json.loads(outbound[0][1]),
                         {"title": "Conveyor needs your review", "id": "draft123"})
        for forbidden in ("message", "action", "secret", "approval_url", "draft", "token", "args", "content"):
            self.assertNotIn(forbidden, json.loads(outbound[0][1]))
        self.assertEqual(outbound[0][0]["endpoint"], SUB["endpoint"])
        self.assertEqual(web_push._db_path(self.settings).stat().st_mode & 0o777, 0o600)

    def test_operator_isolation_and_delete(self):
        id_a = web_push.subscribe(self.settings, SUB)
        self.settings.operator_name = "operator-B"
        delivered = []
        self.assertEqual(web_push.notify_pending(self.settings, "approval12",
                         sender=lambda *args: delivered.append(args)), 0)
        web_push.unsubscribe(self.settings, id_a)
        self.settings.operator_name = "operator-A"
        self.assertEqual(web_push.notify_pending(self.settings, "approval12",
                         sender=lambda *args: delivered.append(args)), 1)
        web_push.unsubscribe(self.settings, id_a)
        self.assertEqual(web_push.notify_pending(self.settings, "approval12",
                         sender=lambda *args: delivered.append(args)), 0)

    def test_reject_private_push_endpoints_and_invalid_payloads(self):
        for endpoint in ("http://example.com/send", "https://localhost/send",
                         "https://10.0.0.1/send", "https://example.internal/send",
                         "https://example.com:8080/send", "https://user:pass@example.com/send",
                         "https://2130706433/", "https://0x7f000001/", "https://127.1/",
                         "https://0177.0.0.1/", "https://127.0.0.1.nip.io/",
                         "https://169.254.169.254.nip.io/", "https://evil.example.com/",
                         "https://fcm.googleapis.com.evil.example.com/send",
                         "https://FCM.GOOGLEAPIS.COM/send",
                         "https://fcm.googleapis.com./send",
                         "https://fcm.googleapis.com:444/send",
                         "https://fcm.googleapis.com@127.0.0.1/send"): 
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                web_push.subscribe(self.settings, {**SUB, "endpoint": endpoint})
        with self.assertRaises(ValueError):
            web_push.subscribe(self.settings, {"endpoint": SUB["endpoint"], "keys": {}})
        self.assertFalse(web_push._db_path(self.settings).exists())

    def test_allowlisted_push_origins(self):
        for host in ("fcm.googleapis.com", "updates.push.services.mozilla.com",
                     "web.push.apple.com", "a.b.push.apple.com",
                     "db5.notify.windows.com", "android.googleapis.com"):
            with self.subTest(host=host):
                endpoint = "https://" + host + "/push/opaque"
                record = web_push._valid_subscription({**SUB, "endpoint": endpoint})
                self.assertEqual(record[0], endpoint)

    def test_fail_closed_on_private_or_mixed_dns_at_send(self):
        web_push.subscribe(self.settings, SUB)
        deliveries = []
        for addr in ("127.0.0.1", "169.254.169.254", "10.1.2.3",
                     "192.168.1.4", "::1", "fd00::1", "0.0.0.0"):
            with self.subTest(addr=addr):
                family = socket.AF_INET6 if ":" in addr else socket.AF_INET
                answer = [(family, socket.SOCK_STREAM, 6, "", (addr, 443))]
                with patch.object(web_push.socket, "getaddrinfo", return_value=answer):
                    self.assertEqual(web_push.notify_pending(
                        self.settings, "review123",
                        sender=lambda *args: deliveries.append(args)), 0)
        mixed = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.72.202", 443)),
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch.object(web_push.socket, "getaddrinfo", return_value=mixed):
            self.assertEqual(web_push.notify_pending(
                self.settings, "review123",
                sender=lambda *args: deliveries.append(args)), 0)
        with patch.object(web_push.socket, "getaddrinfo", side_effect=socket.gaierror("DNS refused")):
            self.assertEqual(web_push.notify_pending(
                self.settings, "review123",
                sender=lambda *args: deliveries.append(args)), 0)
        self.assertEqual(deliveries, [])

    def test_sender_revalidates_legacy_rows(self):
        import hashlib
        web_push.subscribe(self.settings, SUB)
        endpoint = "https://127.0.0.1.nip.io/secret"
        with web_push._connection(self.settings) as db:
            db.execute("INSERT INTO push_subscriptions VALUES (?,?,?,?,?,?)", (
                hashlib.sha256(endpoint.encode()).hexdigest(), "operator-A",
                endpoint, "A" * 86, "B" * 22, 0.0,
            ))
        deliveries = []
        self.assertEqual(web_push.notify_pending(self.settings, "review123",
                         sender=lambda subscription, _: deliveries.append(subscription)), 1)
        self.assertEqual([d["endpoint"] for d in deliveries], [SUB["endpoint"]])

    def test_worker_has_strict_static_only_cache_and_private_deeplink(self):
        text = (Path(__file__).resolve().parents[1] / "web/public/sw.js").read_text()
        self.assertIn("url.pathname.startsWith('/api/')", text)
        self.assertIn("request.mode === 'navigate'", text)
        self.assertIn("request.headers.has('Authorization')", text)
        self.assertIn("credentials: 'omit'", text)
        self.assertIn("response.headers.has('Set-Cookie')", text)
        self.assertIn("CACHE", text)
        self.assertIn("caches.delete(name)", text)
        self.assertIn("type === 'LOGOUT'", text)
        self.assertIn("#approvals", text)
        self.assertNotIn("actions:", text)
        self.assertNotIn("approve(", text)

    def test_flag_default_off_and_on_from_environment(self):
        env = {"CODEX_WORKSPACE_ROOT": self.temp.name,
               "CODEX_TASK_ROOT": self.temp.name + "/tasks",
               "CODEX_MEMORY_ROOT": self.temp.name + "/memory"}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(load_runtime_settings(env_file="").pwa_enabled)
        with patch.dict(os.environ, {**env, "CONVEYOR_PWA": "1"}, clear=True):
            self.assertTrue(load_runtime_settings(env_file="").pwa_enabled)


class _Control:
    runner = object()

    def __init__(self, settings):
        self.settings = settings


class PushHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.settings = SimpleNamespace(pwa_enabled=False, codex_memory_root=Path(cls.temp.name),
                                       operator_name="operator-A")
        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()
        cls.server = WebConsoleServer(("127.0.0.1", 0), WebConsoleHandler,
                                       control=_Control(cls.settings), loop=cls.loop, token=TOKEN)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.loop_thread.join(timeout=3)
        cls.server_thread.join(timeout=3)
        cls.temp.cleanup()

    def setUp(self):
        self.settings.pwa_enabled = False

    def request(self, method, path, body=None, auth=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=4)
        headers = {"Authorization": "Bearer " + TOKEN} if auth else {}
        content = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = json.dumps(body)
        conn.request(method, path, body=content, headers=headers)
        response = conn.getresponse()
        result = (response.status, response.read(), dict(response.getheaders()))
        conn.close()
        return result

    def test_manifest_icons_ungated_worker_flagged(self):
        for active in (False, True):
            self.settings.pwa_enabled = active
            with self.subTest(active=active):
                code, body, headers = self.request("GET", "/manifest.webmanifest")
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(body)["name"], "Conveyor")
                self.assertIn("application/manifest+json", headers["Content-Type"])
                for path in ("/icon.svg", "/icon-192.png", "/icon-512.png"):
                    self.assertEqual(self.request("GET", path)[0], 200, path)
                code, body, headers = self.request("GET", "/sw.js")
                self.assertEqual(code, 200 if active else 404)
                if active:
                    self.assertIn(b"addEventListener('fetch'", body)
                    self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_spa_deep_links_and_missing_assets_fallback_with_flag_off_and_on(self):
        for active in (False, True):
            self.settings.pwa_enabled = active
            code, index, headers = self.request("GET", "/")
            self.assertEqual(code, 200)
            self.assertIn("text/html", headers["Content-Type"])
            for path in ("/memory", "/skills", "/approvals", "/favicon.ico",
                         "/assets/nonexist.js"):
                with self.subTest(active=active, path=path):
                    status, data, response_headers = self.request("GET", path)
                    self.assertEqual(status, 200)
                    self.assertEqual(data, index)
                    self.assertIn("text/html", response_headers["Content-Type"])

    def test_subscribe_delete_require_bearer_auth_and_flag(self):
        with patch.dict(os.environ, {"CONVEYOR_VAPID_PUBLIC_KEY": "public-test",
                                     "CONVEYOR_VAPID_PRIVATE_KEY": "private-test"}):
            self.assertEqual(self.request("POST", "/api/push/subscribe", SUB)[0], 401)
            self.assertFalse(web_push._db_path(self.settings).exists())
            self.assertEqual(self.request("POST", "/api/push/subscribe", SUB, auth=True)[0], 404)
            self.assertFalse(web_push._db_path(self.settings).exists())
            self.settings.pwa_enabled = True
            self.assertEqual(self.request("GET", "/api/push/config")[0], 401)
            self.assertEqual(self.request("GET", "/api/push/config", auth=True)[0], 200)
            code, body, _ = self.request("POST", "/api/push/subscribe", SUB, auth=True)
            self.assertEqual(code, 201)
            sub_id = json.loads(body)["id"]
            self.assertTrue(web_push._db_path(self.settings).exists())
            self.assertEqual(self.request("DELETE", "/api/push/subscribe", {"id": sub_id})[0], 401)
            self.assertEqual(self.request("DELETE", "/api/push/subscribe", {"id": sub_id}, auth=True)[0], 200)
            self.assertEqual(web_push.notify_pending(self.settings, "approval123",
                    sender=lambda *_: self.fail("unsubscribed device notified")), 0)


if __name__ == "__main__":
    unittest.main()
