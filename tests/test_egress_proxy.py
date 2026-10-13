"""Loopback integration tests for opt-in HTTP/CONNECT egress enforcement."""
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import egress_proxy
import subprocess_env


class Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        payload = b"echo-from-local-server"
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class ProxyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="egress-proxy-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.echo = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
        thread = threading.Thread(target=self.echo.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(self.echo.server_close)
        self.addCleanup(self.echo.shutdown)
        self.port = self.echo.server_address[1]

    def proxy(self, rules=("localhost",)):
        proxy = egress_proxy.EgressProxy(rules, self.root / "decisions.jsonl")
        self.addCleanup(proxy.close)
        return proxy

    def request(self, proxy, host, method="GET", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", proxy.server.server_address[1], timeout=3)
        try:
            conn.request(method, f"http://{host}:{self.port}/echo", body=body,
                         headers={"Host": f"{host}:{self.port}"})
            response = conn.getresponse()
            result = (response.status, response.read())
            return result
        finally:
            conn.close()

    def log(self):
        return [json.loads(row) for row in
                (self.root / "decisions.jsonl").read_text().splitlines()]

    def test_allowlisted_local_echo_passes_and_body_not_logged(self):
        proxy = self.proxy()
        self.assertEqual(self.request(proxy, "localhost"), (200, b"echo-from-local-server"))
        self.assertEqual(self.request(proxy, "localhost", "POST", b"private-body-sentinel"),
                         (200, b"private-body-sentinel"))
        records = self.log()
        self.assertEqual([row["decision"] for row in records], ["allow", "allow"])
        self.assertEqual(records[0]["host"], "localhost")
        self.assertEqual(records[0]["port"], self.port)
        self.assertNotIn("private-body-sentinel",
                         (self.root / "decisions.jsonl").read_text())

    def test_unlisted_host_denied_without_dns_or_connection(self):
        proxy = self.proxy()
        with patch.object(egress_proxy.socket, "getaddrinfo",
                          side_effect=AssertionError("DNS must not run")):
            self.assertEqual(self.request(proxy, "not-allowed.example")[0], 403)
        self.assertEqual(self.log()[-1]["reason"], "not_allowlisted")

    def test_ip_literal_denied_when_not_explicitly_listed(self):
        proxy = self.proxy()
        self.assertEqual(self.request(proxy, "127.0.0.1")[0], 403)
        self.assertEqual(self.log()[-1]["decision"], "deny")

    def test_dns_rebinding_in_wildcard_is_denied(self):
        def rebinding(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("127.0.0.1", self.port))]
        addr, reason = egress_proxy.destination("rebind.example", self.port,
                                                 ("*.example",), resolver=rebinding)
        self.assertIsNone(addr)
        self.assertEqual(reason, "private_dns")

    def test_dns_mixed_public_and_private_is_denied(self):
        def mixed(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        self.assertEqual(egress_proxy.destination("rebind.example", 443,
                                                   ("*.example",), resolver=mixed),
                         (None, "private_dns"))

    def test_wildcard_subdomain_not_apex(self):
        self.assertEqual(egress_proxy.destination("example.test", 443,
                                                   ("*.example.test",))[1], "not_allowlisted")

    def test_no_flag_preserves_existing_child_environment(self):
        settings = SimpleNamespace(egress_proxy_enabled=False, env_isolation=False)
        initial = {"PATH": "/bin", "HTTPS_PROXY": "https://other.invalid:123",
                   "NO_PROXY": "localhost"}
        self.assertEqual(egress_proxy.proxy_env(initial, settings), initial)
        self.assertIsNone(subprocess_env.utility_env(settings, initial))

    def test_flag_adds_proxy_for_utilities_without_env_isolation(self):
        settings = SimpleNamespace(egress_proxy_enabled=True, egress_allowlist="localhost",
                                   codex_memory_root=self.root, egress_proxy_port=0,
                                   env_isolation=False)
        result = subprocess_env.utility_env(settings, {"PATH": "/bin", "NO_PROXY": "*"})
        self.assertTrue(result["HTTPS_PROXY"].startswith("http://127.0.0.1:"))
        self.assertEqual(result["HTTP_PROXY"], result["HTTPS_PROXY"])
        self.assertNotIn("NO_PROXY", result)
        self.addCleanup(egress_proxy._CURRENT.close)

    def test_connect_tunnels_only_allowlisted_destination(self):
        class TCP(socketserver.BaseRequestHandler):
            def handle(self):
                while True:
                    data = self.request.recv(4096)
                    if not data:
                        return
                    self.request.sendall(data)

        class TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
            daemon_threads = True
            allow_reuse_address = True

        remote = TCPServer(("127.0.0.1", 0), TCP)
        task = threading.Thread(target=remote.serve_forever, daemon=True)
        task.start()
        self.addCleanup(task.join, 3)
        self.addCleanup(remote.server_close)
        self.addCleanup(remote.shutdown)
        proxy = self.proxy()
        with socket.create_connection(("127.0.0.1", proxy.server.server_address[1]), timeout=3) as sock:
            sock.settimeout(3)
            sock.sendall(f"CONNECT localhost:{remote.server_address[1]} HTTP/1.1\r\n"
                         f"Host: localhost:{remote.server_address[1]}\r\n\r\n".encode())
            result = sock.recv(4096)
            self.assertIn(b"200 Connection Established", result)
            sock.sendall(b"tunnel-echo")
            self.assertEqual(sock.recv(4096), b"tunnel-echo")


if __name__ == "__main__":
    unittest.main()
