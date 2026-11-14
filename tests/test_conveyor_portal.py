"""Tailnet portal keeps the browser Host so app CSRF checks still match."""
from __future__ import annotations

import http.client
import importlib.util
import json
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import unittest


ROOT = Path(__file__).resolve().parents[1]


def _load_portal():
    path = ROOT / "deploy" / "conveyor_portal.py"
    spec = importlib.util.spec_from_file_location("conveyor_portal_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingUpstream(BaseHTTPRequestHandler):
    recorded: list[dict] = []

    def log_message(self, *_args):
        return

    def _capture(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        self.recorded.append({
            "method": self.command,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        })
        payload = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def do_GET(self):
        self._capture()

    def do_POST(self):
        self._capture()

    def do_HEAD(self):
        self._capture()


class PortalForwardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.portal_mod = _load_portal()
        _RecordingUpstream.recorded = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingUpstream)
        self.upstream_port = int(self.upstream.server_address[1])
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()

        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-portal-")
        self.env_file = Path(self._tmp.name) / "env"
        self.env_file.write_text('CONVEYOR_WEB_TOKEN="portal-test-token"\n', encoding="utf-8")

        self.portal_mod.UPSTREAM_HOST = "127.0.0.1"
        self.portal_mod.UPSTREAM_PORT = self.upstream_port
        self.portal_mod.ENV_FILE = str(self.env_file)
        self.portal = ThreadingHTTPServer(("127.0.0.1", 0), self.portal_mod.PortalProxyHandler)
        self.portal_port = int(self.portal.server_address[1])
        self.portal_thread = threading.Thread(target=self.portal.serve_forever, daemon=True)
        self.portal_thread.start()

    def tearDown(self) -> None:
        self.portal.shutdown()
        self.portal.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.portal_thread.join(timeout=3)
        self.upstream_thread.join(timeout=3)
        self._tmp.cleanup()

    def _post(self, headers: dict[str, str], body: bytes = b"{}") -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.portal_port, timeout=5)
        try:
            conn.request("POST", "/api/agent-conversations/new", body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            self.assertEqual(response.status, 200)
            json.loads(raw.decode("utf-8"))
        finally:
            conn.close()

    def test_preserves_browser_authority_and_injects_bearer(self) -> None:
        self._post({
            "Host": "parlemiao-dev.tailb856ea.ts.net:18787",
            "Origin": "http://parlemiao-dev.tailb856ea.ts.net:18787",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "X-Forwarded-Host": "evil.example",
        })
        self.assertEqual(len(_RecordingUpstream.recorded), 1)
        seen = _RecordingUpstream.recorded[0]
        headers = seen["headers"]
        self.assertEqual(headers["host"], "parlemiao-dev.tailb856ea.ts.net:18787")
        self.assertNotEqual(headers["host"], f"127.0.0.1:{self.upstream_port}")
        self.assertEqual(headers["origin"], "http://parlemiao-dev.tailb856ea.ts.net:18787")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["x-requested-with"], "XMLHttpRequest")
        self.assertEqual(headers["authorization"], "Bearer portal-test-token")
        self.assertEqual(headers["x-forwarded-host"], "evil.example")
        self.assertEqual(seen["body"], b"{}")

    def test_existing_authorization_is_not_replaced(self) -> None:
        self._post({
            "Host": "parlemiao-dev.tailb856ea.ts.net:18787",
            "Origin": "http://parlemiao-dev.tailb856ea.ts.net:18787",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Authorization": "Bearer client-supplied",
        })
        headers = _RecordingUpstream.recorded[0]["headers"]
        self.assertEqual(headers["authorization"], "Bearer client-supplied")
        self.assertNotIn("portal-test-token", headers["authorization"])

    def test_absent_host_falls_back_to_upstream_and_ignores_forwarded_host(self) -> None:
        raw = (
            "POST /api/agent-conversations/new HTTP/1.0\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 2\r\n"
            "X-Requested-With: XMLHttpRequest\r\n"
            "Origin: http://parlemiao-dev.tailb856ea.ts.net:18787\r\n"
            "X-Forwarded-Host: evil.example\r\n"
            "\r\n"
            "{}"
        )
        sock = socket.create_connection(("127.0.0.1", self.portal_port), timeout=5)
        try:
            sock.sendall(raw.encode("ascii"))
            blob = b""
            while b"\r\n\r\n" not in blob:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                blob += chunk
        finally:
            sock.close()
        self.assertIn(b"200", blob.split(b"\r\n", 1)[0])
        headers = _RecordingUpstream.recorded[0]["headers"]
        self.assertEqual(headers["host"], f"127.0.0.1:{self.upstream_port}")
        self.assertNotEqual(headers["host"], "evil.example")
        self.assertEqual(headers["origin"], "http://parlemiao-dev.tailb856ea.ts.net:18787")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["x-requested-with"], "XMLHttpRequest")
        self.assertEqual(headers["authorization"], "Bearer portal-test-token")

    def test_request_authority_rejects_unsafe_host_without_using_forwarded_host(self) -> None:
        fallback = "127.0.0.1:18787"
        self.assertEqual(self.portal_mod.request_authority(None, fallback), fallback)
        self.assertEqual(self.portal_mod.request_authority("", fallback), fallback)
        self.assertEqual(self.portal_mod.request_authority("bad\r\nX-Injected: 1", fallback), fallback)
        self.assertEqual(
            self.portal_mod.request_authority("parlemiao-dev.tailb856ea.ts.net:18787", fallback),
            "parlemiao-dev.tailb856ea.ts.net:18787",
        )
        self.assertEqual(self.portal_mod.request_authority("bad host", fallback), fallback)

    def test_boot_script_still_injects_token(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.portal_port, timeout=5)
        try:
            conn.request("GET", "/conveyor-tailnet-boot.js")
            response = conn.getresponse()
            body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
        finally:
            conn.close()
        self.assertIn("portal-test-token", body)
        self.assertIn("conveyor-token", body)
        self.assertEqual(_RecordingUpstream.recorded, [])


if __name__ == "__main__":
    unittest.main()
