"""Offline pinned-HTTPS response framing and deadline tests (no Playwright)."""
from __future__ import annotations

import io
import time
import unittest
from unittest.mock import patch

from workflow_browser_external_fetch import fetch_single_https_response


class FakeSocket:
    def settimeout(self, _timeout):
        pass


class FakeResponse:
    status = 200

    def __init__(self, headers, body, *, pause=0):
        self.headers = headers
        self.buf = io.BytesIO(body)
        self.pause = pause

    def getheaders(self):
        return self.headers

    def read(self, count):
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

    def test_head_is_safe_and_never_reads_body(self):
        result = self.fetch([("Content-Length", "42")], b"ignored", method="HEAD")
        self.assertEqual(result.body, b"")


if __name__ == "__main__":
    unittest.main()
