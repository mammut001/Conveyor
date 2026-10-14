"""G4 offline policy and required local real-Chromium cookie isolation tests."""
from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from browser_auth_policy import check_request, enabled, origins
from browser_auth_sessions import AuthSession, AuthSessionManager
from workflow_browser_external_identity import ExternalPolicyDenied
from human_takeover import HumanTakeoverStore

RUN_REAL = os.getenv("CONVEYOR_REQUIRE_REAL_BROWSER") == "1"


class G4OfflineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            "CONVEYOR_REAL_BROWSER": "1",
            "CONVEYOR_BROWSER_EXTERNAL_READONLY": "1",
            "CONVEYOR_BROWSER_EXTERNAL_INTERACT": "1",
            "CONVEYOR_BROWSER_EXTERNAL_SUBMIT": "1",
            "CONVEYOR_TAKEOVER_ENABLED": "1",
            "CONVEYOR_BROWSER_AUTH_SESSIONS": "1",
            "CONVEYOR_BROWSER_AUTH_DOMAINS": "http://127.0.0.1:8765",
            "CONVEYOR_BROWSER_AUTH_TEST_LOOPBACK": "1",
            "CONVEYOR_REQUIRE_REAL_BROWSER": "1",
            "CONVEYOR_BROWSER_AUTH_TTL_SEC": "900",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.settings = SimpleNamespace(codex_memory_root=Path(self.tmp.name))
        self.manager = AuthSessionManager(self.settings)

    def test_origin_and_request_policy(self):
        good = "http://127.0.0.1:8765"
        self.assertIn(good, origins())
        check_request(good + "/login", "POST", good, True)
        for url, method, lease in (
            (good + "/submit", "POST", True),
            (good + "/login", "POST", False),
            ("http://127.0.0.1:8766/steal", "GET", True),
            ("https://evil.example/x", "GET", False),
            (good.replace("127.0.0.1", "localhost") + "/", "GET", True),
        ):
            with self.subTest(url=url), self.assertRaises(ExternalPolicyDenied):
                check_request(url, method, good, lease)

    async def test_g4_requires_all_prior_browser_gates(self):
        for name in (
            "CONVEYOR_REAL_BROWSER", "CONVEYOR_BROWSER_EXTERNAL_READONLY",
            "CONVEYOR_BROWSER_EXTERNAL_INTERACT", "CONVEYOR_BROWSER_EXTERNAL_SUBMIT",
            "CONVEYOR_TAKEOVER_ENABLED",
        ):
            with self.subTest(name=name):
                os.environ[name] = "0"
                self.assertFalse(enabled())
                with self.assertRaises(ExternalPolicyDenied):
                    await self.manager.open("operator", "task1", "http://127.0.0.1:8765")
                os.environ[name] = "1"

    def test_bare_domain_normalization(self):
        from browser_auth_policy import _origin
        self.assertEqual(_origin("approved.example.com"), "https://approved.example.com")
        self.assertEqual(_origin("https://approved.example.com:443"),
                         "https://approved.example.com")
        with self.assertRaises(ExternalPolicyDenied):
            _origin("*.example.com")

    def test_loopback_fixture_refuses_display_and_non_test(self):
        from browser_auth_policy import _origin, fixture_loopback_enabled
        origin = "http://127.0.0.1:8765"
        self.assertTrue(fixture_loopback_enabled())
        with patch.dict(os.environ, {"CONVEYOR_REQUIRE_REAL_BROWSER": "0"}):
            self.assertFalse(fixture_loopback_enabled())
            with self.assertRaises(ExternalPolicyDenied):
                _origin(origin)
        with patch.dict(os.environ, {"DISPLAY": ":99"}):
            self.assertFalse(fixture_loopback_enabled())
            with self.assertRaises(ExternalPolicyDenied):
                _origin(origin)

    async def test_flag_off_does_not_launch(self):
        os.environ["CONVEYOR_BROWSER_AUTH_SESSIONS"] = "0"
        with self.assertRaises(ExternalPolicyDenied):
            await self.manager.open("operator", "task1", "http://127.0.0.1:8765")
        self.assertEqual(list(self.manager.root.glob("session-*")), [])

    async def _open_fake(self, operator="operator", task="task"):
        with patch.object(AuthSession, "start", new_callable=AsyncMock):
            return await self.manager.open(operator, task, "http://127.0.0.1:8765")

    async def test_session_binding_revoke_and_logout_delete_dir(self):
        a = await self._open_fake("alice", "task1")
        b = await self._open_fake("bob", "task2")
        self.assertNotEqual(a.directory, b.directory)
        for s in (a, b):
            (s.directory / "profile").mkdir()
            (s.directory / "profile" / "Cookie").write_bytes(b"secret")
        self.assertEqual(len(await self.manager.list_public(operator="alice")), 1)
        self.assertEqual(len(await self.manager.list_public(operator="bob")), 1)
        self.assertNotIn(a.id, [row["id"] for row in
                               await self.manager.list_public(operator="bob")])
        self.assertFalse(await self.manager.revoke(a.id, operator="bob"))
        self.assertTrue(a.directory.exists())
        self.assertTrue(await self.manager.revoke(a.id, operator="alice"))
        self.assertFalse(a.directory.exists())
        self.assertTrue(b.directory.exists())
        self.assertTrue(await self.manager.logout(b.id, operator="bob"))
        self.assertFalse(b.directory.exists())

    async def test_ttl_and_kill_delete_storage(self):
        a = await self._open_fake()
        cookie = a.directory / "cookie"
        cookie.write_bytes(b"secret")
        a.expires_at = time.time() - 1
        self.assertEqual(await self.manager.list_public(operator="operator"), [])
        self.assertFalse(cookie.exists())
        b = await self._open_fake()
        cookie2 = b.directory / "cookie"
        cookie2.write_bytes(b"secret")
        from browser_g3_kill import path as kill_path
        kill = kill_path(self.settings)
        kill.touch(mode=0o600)
        self.assertEqual(await self.manager.list_public(operator="operator"), [])
        self.assertFalse(cookie2.exists())

    async def test_takeover_denies_every_agent_entrypoint(self):
        s = await self._open_fake()
        store = HumanTakeoverStore(self.settings)
        store.start(reason="login", task_id="task")
        for action in ("observe", "click", "type", "screenshot", "clipboard", "submit"):
            with self.subTest(action=action), self.assertRaisesRegex(
                    ExternalPolicyDenied, "g4_human_takeover_active"):
                await getattr(s, action)()
        await self.manager.revoke(s.id, operator="operator")

    def test_startup_orphan_cleanup(self):
        orphan = self.manager.root / "session-abandoned"
        orphan.mkdir(mode=0o700)
        (orphan / "Cookies").write_bytes(b"token")
        new_manager = AuthSessionManager(self.settings)
        self.assertFalse(orphan.exists())
        self.assertEqual(new_manager.sessions, {})


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/login":
            body = b'<html><body><form method="post" action="/login"><input type="password" name="password"><button>Login</button></form></body></html>'
        elif self.path == "/who":
            cookie = self.headers.get("Cookie", "")
            body = (b"<html><body>logged-in</body></html>" if "session=alice" in cookie
                    else b"<html><body>guest</body></html>")
        elif self.path == "/off-domain":
            body = b'<html><body><a href="http://127.0.0.1:8999/steal">go</a></body></html>'
        else:
            body = b"<html><body>fixture</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/login":
            self.server.bad_posts += 1
            self.send_response(403)
            self.end_headers()
            return
        self.server.login_posts += 1
        body = b"<html><body>logged-in</body></html>"
        self.send_response(200)
        self.send_header("Set-Cookie", "session=alice; HttpOnly; SameSite=Lax; Path=/")
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


@unittest.skipUnless(RUN_REAL, "Chromium fixture required only in real-browser CI")
class G4RealBrowserTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        cls.http.login_posts = cls.http.bad_posts = 0
        cls.worker = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.worker.start()
        cls.other = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        cls.other.login_posts = cls.other.bad_posts = 0
        cls.other_worker = threading.Thread(target=cls.other.serve_forever, daemon=True)
        cls.other_worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.worker.join(timeout=3)
        cls.other.shutdown()
        cls.other.server_close()
        cls.other_worker.join(timeout=3)
        super().tearDownClass()

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.domain = f"http://127.0.0.1:{self.http.server_port}"
        self.env = patch.dict(os.environ, {
            "CONVEYOR_REAL_BROWSER": "1",
            "CONVEYOR_BROWSER_EXTERNAL_READONLY": "1",
            "CONVEYOR_BROWSER_EXTERNAL_INTERACT": "1",
            "CONVEYOR_BROWSER_EXTERNAL_SUBMIT": "1",
            "CONVEYOR_TAKEOVER_ENABLED": "1",
            "CONVEYOR_BROWSER_AUTH_SESSIONS": "1",
            "CONVEYOR_REAL_BROWSER": "1",
            "CONVEYOR_BROWSER_AUTH_DOMAINS": self.domain,
            "CONVEYOR_BROWSER_AUTH_TEST_LOOPBACK": "1",
            "CONVEYOR_REQUIRE_REAL_BROWSER": "1",
            "CONVEYOR_BROWSER_AUTH_TTL_SEC": "900",
        })
        self.env.start()
        self.settings = SimpleNamespace(codex_memory_root=Path(self.tmp.name))
        self.mgr = AuthSessionManager(self.settings)
        self.store = HumanTakeoverStore(self.settings)

    async def asyncTearDown(self):
        await self.mgr.revoke_all()
        self.env.stop()
        self.tmp.cleanup()

    async def _session(self, operator, task):
        self.store.start(reason="login", task_id=task, requested_by=operator)
        current = self.store.current()
        self.store.activate(current["id"])
        s = await self.mgr.open(operator, task, self.domain)
        return s, current["id"]

    async def test_isolation_login_takeover_and_sensitive_field(self):
        s, lease = await self._session("alice", "task1")
        await s.navigate_for_human(self.domain + "/login")
        with self.assertRaisesRegex(ExternalPolicyDenied, "takeover"):
            await s.observe()
        # Fixture simulates manual remote-desktop actions; no agent API types
        # the password or sees it through page text.
        await s.page.locator("input[type=password]").fill("secret")
        await s.page.locator("button").click()
        await s.page.wait_for_load_state()
        self.assertEqual(self.http.login_posts >= 1, True)
        self.store.complete(lease)
        await s.page.goto(self.domain + "/who")
        self.assertEqual((await s.observe())["text"], "logged-in")
        with self.assertRaisesRegex(ExternalPolicyDenied, "approval_required"):
            await s.submit()
        cookie_dir = s.directory
        self.assertTrue(await self.mgr.revoke(s.id, operator="alice"))
        self.assertFalse(cookie_dir.exists())
        second, lease2 = await self._session("bob", "task2")
        await second.navigate_for_human(self.domain + "/who")
        self.store.complete(lease2)
        self.assertEqual((await second.observe())["text"], "guest")
        # Password fields remain inaccessible to agent readback.
        await second.page.goto(self.domain + "/login")
        with self.assertRaisesRegex(ExternalPolicyDenied, "sensitive_dom"):
            await second.observe()

    async def test_cross_domain_cookie_isolation(self):
        first, lease = await self._session("alice", "task-domain-a")
        await first.navigate_for_human(self.domain + "/login")
        await first.page.locator("input[type=password]").fill("private")
        await first.page.locator("button").click()
        self.store.complete(lease)

        second_domain = f"http://127.0.0.1:{self.other.server_port}"
        os.environ["CONVEYOR_BROWSER_AUTH_DOMAINS"] = self.domain + "," + second_domain
        self.store.start(reason="login", task_id="task-domain-b", requested_by="bob")
        next_lease = self.store.current()
        self.store.activate(next_lease["id"])
        second = await self.mgr.open("bob", "task-domain-b", second_domain)
        await second.navigate_for_human(second_domain + "/who")
        self.store.complete(next_lease["id"])
        self.assertEqual((await second.observe())["text"], "guest")
        # These two origins share the same IP/cookie-host, so a reused cookie
        # jar would leak even though their TCP ports differ.
        await first.page.goto(self.domain + "/who")
        self.assertEqual((await first.observe())["text"], "logged-in")

    async def test_cross_domain_redirect_and_unapproved_submit(self):
        s, lease = await self._session("alice", "task3")
        await s.navigate_for_human(self.domain + "/off-domain")
        try:
            await s.page.locator("a").click(timeout=1500)
        except Exception:
            pass  # Chromium may report aborted navigation to click()
        await asyncio.sleep(0.1)
        self.assertNotEqual(s.page.url, "http://127.0.0.1:8999/steal")
        with self.assertRaises(ExternalPolicyDenied):
            check_request(self.domain + "/purchase", "POST", self.domain, True)
        self.assertEqual(self.http.bad_posts, 0)
        self.store.complete(lease)

    async def test_ttl_and_kill_clear_real_cookie_directory(self):
        s, lease = await self._session("alice", "task4")
        await s.navigate_for_human(self.domain + "/login")
        await s.page.locator("input[type=password]").fill("private")
        await s.page.locator("button").click()
        self.store.complete(lease)
        directory = s.directory
        s.expires_at = time.time() - 1
        self.assertEqual(await self.mgr.list_public(operator="alice"), [])
        self.assertFalse(directory.exists())
        t, lease2 = await self._session("bob", "task5")
        self.store.complete(lease2)
        from browser_g3_kill import path as kill_path
        kill = kill_path(self.settings)
        kill.touch()
        self.assertEqual(await self.mgr.list_public(operator="bob"), [])
        self.assertFalse(t.directory.exists())


if __name__ == "__main__":
    unittest.main()
