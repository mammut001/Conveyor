"""Tests for gzip response compression in the Web Console HTTP server."""
from __future__ import annotations

import asyncio
import gzip
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from conveyor.entrypoints import web_console

TOKEN = "t" * 40


def _settings(root: Path, **overrides):
    from conveyor.core.config import Settings

    base = Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_id=42,
        codex_workspace_root=root,
        codex_bin="codex",
        codex_task_root=root / "tasks",
        codex_model=None,
        codex_timeout_seconds=30,
        codex_retry_429_delays_seconds=(),
        telegram_progress_seconds=1,
        codex_memory_root=root,
        user_timezone="UTC",
        routines_enabled=True,
    )
    return replace(base, **overrides)


class AcceptsGzipUnitTests(unittest.TestCase):
    def _accepts(self, headers: dict[str, str] | None) -> bool:
        handler = SimpleNamespace(headers=headers)
        return web_console.WebConsoleHandler._accepts_gzip(handler)  # type: ignore[arg-type]

    def test_accepts_gzip_parsing(self) -> None:
        self.assertTrue(self._accepts({"Accept-Encoding": "gzip"}))
        self.assertTrue(self._accepts({"Accept-Encoding": "deflate, gzip, br"}))
        self.assertTrue(self._accepts({"Accept-Encoding": "br, gzip;q=0.5"}))
        self.assertTrue(self._accepts({"accept-encoding": "GZIP"}))
        self.assertFalse(self._accepts({"Accept-Encoding": "gzip;q=0"}))
        self.assertFalse(self._accepts({"Accept-Encoding": "deflate, gzip; q=0.000"}))
        self.assertFalse(self._accepts({"Accept-Encoding": "*"}))
        self.assertFalse(self._accepts({"Accept-Encoding": "br, deflate"}))
        self.assertFalse(self._accepts({"Accept-Encoding": "gzip;q=invalid"}))
        self.assertFalse(self._accepts({}))
        self.assertFalse(self._accepts(None))


class WebCompressionTests(unittest.TestCase):
    def setUp(self) -> None:
        from conveyor.entrypoints.web_console import WebConsoleHandler, WebConsoleServer
        from conveyor.web.web_control import WebControl

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = _settings(self.root)
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.root / "state" / "job_queue.sqlite3"))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS queued_jobs "
            "(id TEXT, channel TEXT, operator_id TEXT, chat_id TEXT)"
        )
        conn.commit()
        conn.close()
        self.queue = mock.MagicMock()
        self.queue.list_jobs.side_effect = lambda *_a, **_kw: [
            {"id": f"job-{i}", "state": "completed", "prompt": "x" * 200}
            for i in range(20)
        ]
        self.queue._db_path.return_value = self.root / "state" / "job_queue.sqlite3"
        control = WebControl(self.settings, runner=mock.MagicMock(), queue=self.queue)
        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
        loop_thread.start()
        self.server = WebConsoleServer(
            ("127.0.0.1", 0), WebConsoleHandler, control=control, loop=loop, token=TOKEN,
        )
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            self.server.shutdown()
            self.server.server_close()
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=2)
            thread.join(timeout=2)

        self.addCleanup(stop)

    def _get(
        self,
        path: str,
        *,
        accept_encoding: str | None = None,
        authorized: bool = True,
    ) -> tuple[int, http.client.HTTPMessage, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        headers: dict[str, str] = {}
        if authorized:
            headers["Authorization"] = f"Bearer {TOKEN}"
        if accept_encoding is not None:
            headers["Accept-Encoding"] = accept_encoding
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        body = response.read()
        resp_headers = response.headers
        conn.close()
        return response.status, resp_headers, body

    def test_large_json_compressed_when_accepted(self) -> None:
        status_plain, headers_plain, body_plain = self._get("/api/jobs")
        self.assertEqual(status_plain, 200)
        self.assertIsNone(headers_plain.get("Content-Encoding"))
        self.assertEqual(headers_plain.get("Cache-Control"), "no-store")
        plain_payload = json.loads(body_plain)
        self.assertGreater(len(body_plain), 1024)

        status_gz, headers_gz, body_gz = self._get(
            "/api/jobs", accept_encoding="gzip, deflate",
        )
        self.assertEqual(status_gz, 200)
        self.assertEqual(headers_gz.get("Content-Encoding"), "gzip")
        self.assertEqual(headers_gz.get("Vary"), "Accept-Encoding")
        self.assertEqual(headers_gz.get("Cache-Control"), "no-store")
        self.assertEqual(int(headers_gz["Content-Length"]), len(body_gz))
        self.assertLess(len(body_gz), len(body_plain))
        self.assertEqual(json.loads(gzip.decompress(body_gz)), plain_payload)

    def test_json_not_compressed_when_q_zero_or_wildcard_only(self) -> None:
        for header in ("gzip;q=0", "*"):
            with self.subTest(accept_encoding=header):
                status, headers, body = self._get("/api/jobs", accept_encoding=header)
                self.assertEqual(status, 200)
                self.assertIsNone(headers.get("Content-Encoding"))
                self.assertEqual(headers.get("Cache-Control"), "no-store")
                payload = json.loads(body)
                self.assertEqual(len(payload["jobs"]), 20)

    def test_small_json_not_compressed(self) -> None:
        status, headers, body = self._get(
            "/api/does-not-exist", accept_encoding="gzip",
        )
        self.assertEqual(status, 404)
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(json.loads(body), {"error": "not found"})

    def test_static_files_compression_and_cache(self) -> None:
        dist = self.root / "dist"
        (dist / "assets").mkdir(parents=True, exist_ok=True)
        html_bytes = b"<!doctype html><html><body>" + (b"x" * 2048) + b"</body></html>"
        js_bytes = b"export const msg = '" + (b"y" * 4096) + b"';\n"
        png_bytes = b"\x89PNG\r\n\x1a\n" + (b"\xab" * 2048)
        (dist / "index.html").write_bytes(html_bytes)
        (dist / "assets" / "app.js").write_bytes(js_bytes)
        (dist / "logo.png").write_bytes(png_bytes)

        with mock.patch.object(web_console, "STATIC_ROOT", dist), \
                mock.patch.dict(web_console._STATIC_GZIP_CACHE, {}, clear=True):
            status, headers, body = self._get(
                "/", accept_encoding="gzip", authorized=False,
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Content-Encoding"), "gzip")
            self.assertEqual(headers.get("Vary"), "Accept-Encoding")
            self.assertEqual(headers.get("Cache-Control"), "no-cache")
            self.assertEqual(gzip.decompress(body), html_bytes)

            status, headers, body = self._get(
                "/assets/app.js", accept_encoding="gzip", authorized=False,
            )
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Content-Encoding"), "gzip")
            self.assertEqual(headers.get("Vary"), "Accept-Encoding")
            self.assertEqual(gzip.decompress(body), js_bytes)
            self.assertGreaterEqual(len(web_console._STATIC_GZIP_CACHE), 2)

            status, headers, body = self._get(
                "/assets/app.js", accept_encoding=None, authorized=False,
            )
            self.assertEqual(status, 200)
            self.assertIsNone(headers.get("Content-Encoding"))
            self.assertEqual(body, js_bytes)

            status, headers, body = self._get(
                "/logo.png", accept_encoding="gzip", authorized=False,
            )
            self.assertEqual(status, 200)
            self.assertIsNone(headers.get("Content-Encoding"))
            self.assertEqual(body, png_bytes)


if __name__ == "__main__":
    unittest.main()
