"""Offline pinned-HTTPS response framing and deadline tests (no Playwright)."""
from __future__ import annotations

import io
import os
from pathlib import Path
import queue
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from workflow_browser_external_fetch import fetch_single_https_response


class FakeSocket:
    def settimeout(self, _timeout):
        pass

    def shutdown(self, _how):
        pass

    def close(self):
        pass


class FakeResponse:
    status = 200

    def __init__(self, headers, body, *, pause=0):
        self.headers = headers
        self.buf = io.BytesIO(body)
        self.pause = pause

    def getheaders(self):
        return self.headers

    def read1(self, count):
        if self.pause:
            time.sleep(self.pause)
        return self.buf.read(count)


class FakeConnection:
    response = None
    instances = []

    def __init__(self, *_args, **_kwargs):
        self.sock = FakeSocket()
        self.timeout = 8
        self.sent = None
        self.closed = False
        self.instances.append(self)

    def request(self, method, path, headers):
        self.sent = method, path, headers

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class FetchPolicyTests(unittest.TestCase):
    def setUp(self):
        FakeConnection.instances.clear()
        self.mock = patch("workflow_browser_external_fetch._PinnedHTTPSConnection",
                          FakeConnection)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def fetch(self, headers, body, *, method="GET", pause=0):
        FakeConnection.response = FakeResponse(headers, body, pause=pause)
        return fetch_single_https_response("approved.example.org", 443,
                                           "8.8.8.8", "/reviewed", method)

    def test_identity_body_and_exact_declared_length(self):
        result = self.fetch([("Content-Length", "5"),
                             ("Content-Encoding", "identity")], b"hello")
        self.assertEqual(result.body, b"hello")
        sent = FakeConnection.instances[-1].sent
        self.assertEqual(sent[2]["Accept-Encoding"], "identity")
        self.assertTrue(FakeConnection.instances[-1].closed)

    def test_rejects_mismatches_and_ambiguous_lengths(self):
        for headers, body in (
            ([("Content-Length", "9")], b"short"),
            ([("Content-Length", "four")], b"test"),
            ([("Content-Length", "4"), ("Content-Length", "4")], b"test"),
            ([("Content-Length", "4"), ("Transfer-Encoding", "chunked")], b"test"),
            ([("Content-Length", str(1024 * 1024 + 1))], b"short"),
        ):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                self.fetch(headers, body)

    def test_all_encoded_responses_fail_closed(self):
        for encoding in ("gzip", "br", "deflate", "identity, gzip", ""):
            with self.subTest(encoding=encoding), self.assertRaisesRegex(
                ValueError, "encoded_response_denied"
            ):
                self.fetch([("Content-Encoding", encoding)], b"compressed")

    def test_body_limit_and_deadline(self):
        with self.assertRaisesRegex(ValueError, "oversized_response"):
            self.fetch([], b"x" * (1024 * 1024 + 1))
        with patch("workflow_browser_external_fetch.FETCH_DEADLINE_SECONDS", 0.05):
            with self.assertRaisesRegex(TimeoutError, "fetch_deadline_exceeded"):
                self.fetch([], b"hello", pause=0.15)

    def test_socket_read_timeout_uses_fixed_deadline_code(self):
        FakeConnection.response = FakeResponse([], b"")
        with patch.object(FakeResponse, "read1",
                          side_effect=socket.timeout("The read operation timed out")):
            with self.assertRaisesRegex(TimeoutError, "fetch_deadline_exceeded"):
                fetch_single_https_response("approved.example.org", 443,
                                            "8.8.8.8", "/reviewed")

    def test_head_is_safe_and_never_reads_body(self):
        result = self.fetch([("Content-Length", "42")], b"ignored", method="HEAD")
        self.assertEqual(result.body, b"")


class SlowDripTLSTests(unittest.TestCase):
    def test_real_tls_http10_and_http11_connection_close_succeed(self):
        """http.client closes conn.sock on these responses but body is still readable."""
        body = b"read works"
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = Path(tmp) / "cert.pem", Path(tmp) / "key.pem"
            subprocess.run([
                "openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048",
                "-days", "1", "-keyout", str(key), "-out", str(cert),
                "-subj", "/CN=approved.fixture.test",
                "-addext", "subjectAltName=DNS:approved.fixture.test",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(cert), str(key))
            for version, headers in (
                ("HTTP/1.0", b""),
                ("HTTP/1.1", b"Content-Length: 10\\r\\nConnection: close\\r\\n"),
            ):
                with self.subTest(version=version):
                    class ResponseHandler(socketserver.BaseRequestHandler):
                        def handle(self):
                            with context.wrap_socket(self.request, server_side=True) as tls:
                                tls.settimeout(3)
                                incoming = tls.makefile("rb")
                                while incoming.readline() not in (b"\\r\\n", b"\\n", b""):
                                    pass
                                tls.sendall(
                                    (version + " 200 OK\\r\\n").encode("ascii")
                                    + b"Content-Type: text/plain\\r\\n"
                                    + headers + b"\\r\\n" + body
                                )

                    with socketserver.ThreadingTCPServer(
                        ("127.0.0.1", 0), ResponseHandler
                    ) as server:
                        server.daemon_threads = True
                        thread = threading.Thread(
                            target=server.serve_forever, daemon=True)
                        thread.start()
                        try:
                            with patch.dict(os.environ, {"SSL_CERT_FILE": str(cert)}):
                                result = fetch_single_https_response(
                                    "approved.fixture.test", server.server_address[1],
                                    "127.0.0.1", "/read")
                            self.assertEqual(result.status, 200)
                            self.assertEqual(result.body, body)
                        finally:
                            server.shutdown()
                            thread.join(timeout=2)

    def test_real_tls_bytewise_drip_expires_without_leaking_worker(self):
        """Send one byte every 0.5s over real TLS, but stop in <= deadline+2s."""
        class DripHandler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    with server_context.wrap_socket(self.request, server_side=True) as tls:
                        tls.settimeout(5)
                        incoming = tls.makefile("rb")
                        while incoming.readline() not in (b"\r\n", b"\n", b""):
                            pass
                        tls.sendall(
                            b"HTTP/1.1 200 OK\r\n"
                            b"Content-Type: text/plain\r\n"
                            b"Content-Length: 100\r\n"
                            b"Connection: close\r\n\r\n"
                        )
                        for _ in range(100):
                            tls.sendall(b"x")
                            time.sleep(0.5)
                except (OSError, ssl.SSLError):
                    pass

        with tempfile.TemporaryDirectory() as tmp:
            cert, key = Path(tmp) / "cert.pem", Path(tmp) / "key.pem"
            subprocess.run([
                "openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048",
                "-days", "1", "-keyout", str(key), "-out", str(cert),
                "-subj", "/CN=approved.fixture.test",
                "-addext", "subjectAltName=DNS:approved.fixture.test",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(str(cert), str(key))
            with socketserver.ThreadingTCPServer(("127.0.0.1", 0), DripHandler) as server:
                server.daemon_threads = True
                server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                server_thread.start()
                deadline = 1.6
                result = queue.Queue()
                def run_fetch():
                    try:
                        fetch_single_https_response(
                            "approved.fixture.test", server.server_address[1],
                            "127.0.0.1", "/slow-drip")
                    except BaseException as exc:
                        result.put(exc)
                    else:
                        result.put(None)
                with patch.dict(os.environ, {"SSL_CERT_FILE": str(cert)}), patch(
                    "workflow_browser_external_fetch.FETCH_DEADLINE_SECONDS", deadline
                ):
                    worker = threading.Thread(target=run_fetch, daemon=True)
                    start = time.monotonic()
                    worker.start()
                    worker.join(deadline + 2)
                    elapsed = time.monotonic() - start
                    self.assertFalse(worker.is_alive(), "TLS slow-drip leaked a worker thread")
                    self.assertLess(elapsed, deadline + 2)
                    failure = result.get_nowait()
                    self.assertIsInstance(failure, TimeoutError)
                    self.assertIn("fetch_deadline_exceeded", str(failure))
                server.shutdown()
                server_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
