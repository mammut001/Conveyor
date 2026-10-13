"""Loopback integration tests for opt-in HTTP/CONNECT egress enforcement."""
from __future__ import annotations

import http.client
import ipaddress
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

    def proxy(self, rules=("echo.fixture.test",), *, cidrs=("198.18.0.0/15",)):
        # The policy sees a fake DNS sandbox address, and only the test
        # dialer maps it to the loopback echo server. Production never does.
        def dns(host, port, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.1", port))]
        def dialer(addr, timeout=None):
            self.assertEqual(addr[0], "198.18.0.1")
            return socket.create_connection(("127.0.0.1", addr[1]), timeout)
        proxy = egress_proxy.EgressProxy(
            rules, self.root / "decisions.jsonl",
            cidrs=tuple(ipaddress.ip_network(c) for c in cidrs),
            resolver=dns, dialer=dialer)
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
        self.assertEqual(self.request(proxy, "echo.fixture.test"), (200, b"echo-from-local-server"))
        self.assertEqual(self.request(proxy, "echo.fixture.test", "POST", b"private-body-sentinel"),
                         (200, b"private-body-sentinel"))
        records = self.log()
        self.assertEqual([row["decision"] for row in records], ["allow", "allow"])
        self.assertEqual(records[0]["host"], "echo.fixture.test")
        self.assertEqual(records[0]["port"], self.port)
        self.assertNotIn("private-body-sentinel",
                         (self.root / "decisions.jsonl").read_text())

    def test_unlisted_host_denied_without_dns_or_connection(self):
        proxy = self.proxy()
        self.assertEqual(self.request(proxy, "not-allowed.example")[0], 403)
        def unexpected_dns(*args, **kwargs):
            raise AssertionError("DNS must not run for an unlisted host")
        self.assertEqual(egress_proxy.destination("not-allowed.example", self.port,
                         ("localhost",), resolver=unexpected_dns),
                         (None, "not_allowlisted"))
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
        self.assertEqual(reason, "forbidden_dns")

    def test_dns_mixed_public_and_private_is_denied(self):
        def mixed(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        self.assertEqual(egress_proxy.destination("rebind.example", 443,
                                                   ("*.example",), resolver=mixed),
                         (None, "forbidden_dns"))

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
            sock.sendall(f"CONNECT echo.fixture.test:{remote.server_address[1]} HTTP/1.1\r\n"
                         f"Host: echo.fixture.test:{remote.server_address[1]}\r\n\r\n".encode())
            result = sock.recv(4096)
            self.assertIn(b"200 Connection Established", result)
            sock.sendall(b"tunnel-echo")
            self.assertEqual(sock.recv(4096), b"tunnel-echo")


    def test_exact_name_never_reaches_forbidden_dns_answers(self):
        # Exact entries cannot override local/metadata/IPv4-mapped bans.
        for ip in ("127.0.0.1", "::1", "169.254.169.254",
                   "::ffff:127.0.0.1", "64:ff9b::7f00:1",
                   "64:ff9b::a9fe:a9fe",
                   "64:ff9b:1:7f00:100::", "::"):
            with self.subTest(ip=ip):
                def dns(*args, **kwargs):
                    return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 443, 0, 0))]
                self.assertEqual(egress_proxy.destination(
                    "exact.example", 443, ("exact.example",), resolver=dns,
                    cidrs=(ipaddress.ip_network("::/0"), ipaddress.ip_network("0.0.0.0/0"))),
                    (None, "forbidden_dns"))

    def test_explicit_fake_dns_cidr_is_required_for_exact_and_wildcard(self):
        def fake_dns(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.1", 443))]
        allowed = (ipaddress.ip_network("198.18.0.0/15"),)
        for rules in (("exact.example",), ("*.example",)):
            name = "exact.example"
            self.assertEqual(egress_proxy.destination(name, 443, rules, resolver=fake_dns),
                             (None, "private_dns"))
            self.assertEqual(egress_proxy.destination(
                name, 443, rules, resolver=fake_dns, cidrs=allowed),
                ("198.18.0.1", "private_cidr_allowlisted"))

    def test_private_ip_literal_needs_cidr_and_forbidden_always_denied(self):
        from egress_policy import private_cidrs
        with patch.dict("os.environ", {"CONVEYOR_EGRESS_ALLOW_PRIVATE_CIDRS": "198.18.0.0/15"}):
            cidrs = private_cidrs()
        self.assertEqual(egress_proxy.destination(
            "198.18.0.1", 443, ("198.18.0.1",), cidrs=cidrs),
            ("198.18.0.1", "private_cidr_allowlisted"))
        for ip in ("127.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"):
            self.assertIsNone(egress_proxy.destination(
                ip, 443, (ip,), cidrs=(ipaddress.ip_network("0.0.0.0/0"),
                                      ipaddress.ip_network("::/0")))[0])

    def test_protected_web_and_proxy_ports_denied_on_every_ip(self):
        for port in (8787, 13131):
            ip, reason = egress_proxy.destination(
                "8.8.8.8", port, ("8.8.8.8",), protected_ports={8787, 13131})
            self.assertIsNone(ip)
            self.assertEqual(reason, "protected_port")
        proxy = self.proxy()
        port = proxy.server.server_address[1]
        self.assertEqual(self.request(proxy, "echo.fixture.test")[0], 200)
        self.assertEqual(egress_proxy.destination(
            "8.8.8.8", port, ("8.8.8.8",),
            protected_ports={port}), (None, "protected_port"))

    def test_deadline_and_connection_slots_are_bounded(self):
        import time
        handler = SimpleNamespace()
        with self.assertRaisesRegex(ValueError, "header_deadline"):
            egress_proxy._Handler._deadline_line(handler, 8193, time.monotonic() - 1)
        proxy = self.proxy()
        server = proxy.server
        acquired = [server.slots.acquire(blocking=False) for _ in range(64)]
        self.assertTrue(all(acquired))
        self.assertFalse(server.slots.acquire(blocking=False))
        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=3) as sock:
            sock.settimeout(3)
            sock.sendall(b"GET http://echo.fixture.test/ HTTP/1.1\r\nHost: echo.fixture.test\r\n\r\n")
            self.assertIn(b"503 Service Unavailable", sock.recv(2048))
        for _ in acquired:
            server.slots.release()

if __name__ == "__main__":
    unittest.main()
