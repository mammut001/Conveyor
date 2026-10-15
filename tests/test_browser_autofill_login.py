"""Autofill-login regression: no secret-bearing evidence or off-domain POST."""
from __future__ import annotations
import asyncio
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from urllib.parse import quote

from browser_auth_policy import _origin
from browser_login_credentials import put, read_for_executor, public_list, remove, enabled
from browser_login_audit import reserve, finish, public_audit, LoginQuotaDenied
from workflow_browser_external_identity import ExternalPolicyDenied
from console_secrets import path_for
from browser_auth_orphans import cleanup
from personal_tools.browser_login import login, login_adapter
from browser_auth_sessions import manager_for
from browser_auth_timeout import wait_for_step

REAL = os.getenv("CONVEYOR_REQUIRE_REAL_BROWSER") == "1"


def flags(domain):
    return {
        "CONVEYOR_REAL_BROWSER": "1",
        "CONVEYOR_BROWSER_EXTERNAL_READONLY": "1",
        "CONVEYOR_BROWSER_EXTERNAL_INTERACT": "1",
        "CONVEYOR_BROWSER_EXTERNAL_SUBMIT": "1",
        "CONVEYOR_TAKEOVER_ENABLED": "1",
        "CONVEYOR_BROWSER_AUTH_SESSIONS": "1",
        "CONVEYOR_BROWSER_AUTOFILL_LOGIN": "1",
        "CONVEYOR_BROWSER_AUTH_DOMAINS": domain,
        "CONVEYOR_BROWSER_AUTH_TEST_LOOPBACK": "1",
        "CONVEYOR_REQUIRE_REAL_BROWSER": "1",
    }


class StoreAndAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.origin = "http://127.0.0.1:18098"
        self.env = patch.dict(os.environ, flags(self.origin))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.settings = SimpleNamespace(codex_memory_root=Path(self.tmp.name),
                                        conveyor_takeover_enabled=True)

    def test_write_only_secret_and_exact_domain(self):
        record = put(self.settings, self.origin, "person@example.org", "TOP_SECRET_TEST")
        self.assertEqual(record, {"domain": self.origin, "username": "person@example.org",
                                  "masked": True})
        self.assertNotIn("TOP_SECRET_TEST", str(public_list(self.settings)))
        self.assertNotIn("TOP_SECRET_TEST", str(record))
        self.assertEqual(read_for_executor(self.settings, self.origin)["password"],
                         "TOP_SECRET_TEST")
        self.assertEqual(stat.S_IMODE(path_for(self.settings).stat().st_mode), 0o600)
        with self.assertRaises(ExternalPolicyDenied):
            put(self.settings, "https://other.example.org", "person", "TOP_SECRET_TEST")
        self.assertTrue(remove(self.settings, self.origin))
        self.assertEqual(public_list(self.settings), [])

    def test_rate_limit_and_lockout_with_secret_free_audit(self):
        put(self.settings, self.origin, "person", "TOP_SECRET_TEST")
        for _ in range(3):
            ident = reserve(self.settings, self.origin, "person")
            finish(self.settings, ident, "failed", reason="credentials_rejected",
                   submitted=True)
        with self.assertRaises(LoginQuotaDenied) as cm:
            reserve(self.settings, self.origin, "person")
        self.assertEqual(cm.exception.reason, "locked")
        audit = public_audit(self.settings, self.origin)
        self.assertEqual(len(audit), 3)
        self.assertTrue(all(set(x) == {"domain", "username", "result", "time",
                                      "reason", "submitted"} for x in audit))
        self.assertTrue(all(x["reason"] == "credentials_rejected"
                            and x["submitted"] for x in audit))
        self.assertNotIn("TOP_SECRET_TEST", str(audit))

    def test_infrastructure_failures_have_separate_bounded_quota(self):
        put(self.settings, self.origin, "person", "TOP_SECRET_TEST")
        for _ in range(5):
            ident = reserve(self.settings, self.origin, "person")
            finish(self.settings, ident, "failed", reason="login_open_timeout")
        with self.assertRaises(LoginQuotaDenied) as cm:
            reserve(self.settings, self.origin, "person")
        self.assertEqual(cm.exception.reason, "infra_rate_limited")
        audit = public_audit(self.settings, self.origin)
        self.assertEqual(len(audit), 5)
        self.assertTrue(all(x["result"] == "failed" and
                            x["reason"] == "login_open_timeout" and
                            not x["submitted"] for x in audit))

    def test_rejection_requires_positive_submission_evidence(self):
        attempt = reserve(self.settings, self.origin, "person")
        with self.assertRaisesRegex(ValueError, "unverified rejection"):
            finish(self.settings, attempt, "failed", reason="credentials_rejected")
        finish(self.settings, attempt, "failed", reason="login_unavailable")
        # A generic failure, even with a server-visible POST, is not verified
        # as an incorrect password and does not consume the credential quota.
        attempt = reserve(self.settings, self.origin, "person")
        finish(self.settings, attempt, "failed", reason="login_not_confirmed",
               submitted=True)
        self.assertEqual(len(public_audit(self.settings, self.origin)), 2)
        attempt = reserve(self.settings, self.origin, "person")
        finish(self.settings, attempt, "logged_in", reason="success", submitted=True)

    async def test_flag_off_no_model_tool_or_secret_store(self):
        from handlers.chat_tools import build_tool_schemas
        os.environ["CONVEYOR_BROWSER_AUTOFILL_LOGIN"] = "0"
        self.assertFalse(enabled())
        self.assertFalse(any(x["function"]["name"] == "browser__login"
                             for x in build_tool_schemas(self.settings)))
        result = await login_adapter(self.settings, self.origin, operator_id="operator")
        self.assertIn("failed", result.text)
        self.assertFalse(path_for(self.settings).exists())

    async def test_tools_help_flag_off_matches_main_inventory(self):
        from handlers.commands import _tools
        from personal_tools.registry import PERSONAL_TOOL_REGISTRY, register_personal_tools
        register_personal_tools()

        class Replies:
            def __init__(self):
                self.messages = []

            async def reply(self, _msg, body):
                self.messages.append(body)

        actual, baseline = Replies(), Replies()
        with patch.dict(os.environ, {"CONVEYOR_BROWSER_AUTOFILL_LOGIN": "0"}):
            await _tools(None, actual, None, None, "")
            login_spec = PERSONAL_TOOL_REGISTRY.pop("browser.login")
            try:
                # Without the only new G4 entry this is main's /tools inventory.
                await _tools(None, baseline, None, None, "")
            finally:
                PERSONAL_TOOL_REGISTRY["browser.login"] = login_spec
        self.assertEqual(actual.messages, baseline.messages)
        self.assertNotIn("browser.login", actual.messages[0])

    def test_loopback_requires_ci_without_display(self):
        with patch.dict(os.environ, {"DISPLAY": ":100"}):
            with self.assertRaises(ExternalPolicyDenied):
                _origin(self.origin)
        with patch.dict(os.environ, {"CONVEYOR_REQUIRE_REAL_BROWSER": "0"}):
            with self.assertRaises(ExternalPolicyDenied):
                _origin(self.origin)

    def test_orphan_cleanup_exact_profile_and_retry(self):
        root = Path(self.tmp.name)/"state"/"browser-auth-sessions"
        root.mkdir(parents=True)
        orphan = root/"session-leftover"
        (orphan/"profile").mkdir(parents=True)
        (orphan/"profile"/"Cookies").write_bytes(b"token")
        import browser_auth_orphans as m
        actual_rm = m.shutil.rmtree
        calls = []
        def slow_remove(path):
            calls.append(str(path))
            if len(calls) == 1:
                raise OSError("busy")
            return actual_rm(path)
        with patch.object(m, "_matching_pids", return_value=[12345]), \
             patch.object(m, "_signal_verified") as kill, \
             patch.object(m.shutil, "rmtree", side_effect=slow_remove):
            cleanup(root)
        self.assertFalse(orphan.exists())
        self.assertEqual(len(calls), 2)
        kill.assert_called_once()


class LoginFixture(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/login", "/cross", "/iframe", "/failed", "/mfa"):
            action = (f"http://127.0.0.1:{self.server.other_port}/login"
                      if self.path == "/cross" else "/login")
            iframe = '<iframe src="https://other.example.org/steal"></iframe>' if self.path == "/iframe" else ""
            body = (f'<html><body>{iframe}<form method="post" action="{action}">'
                    '<input name="username" type="text"><input name="password" type="password">'
                    '<button type="submit">Sign in</button></form></body></html>').encode()
        else:
            body = b"<html><body>welcome</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.server.posts += 1
        length = int(self.headers.get("Content-Length", "0"))
        content = self.rfile.read(min(length, 8192))
        # Fixture checks a password was transmitted only to the intended site;
        # neither it nor any response/log retains or echoes the password.
        self.server.password_seen = b"TOP_SECRET_TEST" in content
        if self.path == "/mfa":
            body = b"<html><body>authenticated</body></html>"
        elif self.server.challenge:
            body = (b'<html><body><form method="post" action="/mfa">'
                    b'<input autocomplete="one-time-code" name="otp">'
                    b'<button type="submit">Verify</button></form></body></html>')
        elif self.server.reject:
            body = b'<html><body>try again<input type="password"></body></html>'
        else:
            body = b"<html><body>welcome</body></html>"
        self.send_response(200)
        if self.path == "/mfa" or (not self.server.challenge and not self.server.reject):
            self.send_header("Set-Cookie", "session=ok; HttpOnly; SameSite=Lax; Path=/")
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(REAL, "real Chromium integration required only in fixture CI")
class BrowserLoginRealTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), LoginFixture)
        cls.http.other_port = 65533
        cls.http.posts = 0
        cls.http.password_seen = cls.http.challenge = cls.http.reject = False
        cls.worker = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.worker.join(timeout=3)

    async def asyncSetUp(self):
        loop = asyncio.get_running_loop()
        loop.set_debug(True)
        loop.slow_callback_duration = 1
        self.tmp = tempfile.TemporaryDirectory()
        self.origin = f"http://127.0.0.1:{self.http.server_port}"
        self.env = patch.dict(os.environ, flags(self.origin))
        self.env.start()
        self.settings = SimpleNamespace(codex_memory_root=Path(self.tmp.name),
                                        conveyor_takeover_enabled=True)
        self.http.posts = 0
        self.http.password_seen = self.http.challenge = self.http.reject = False

    async def asyncTearDown(self):
        mgr = manager_for(self.settings)
        watches = [s._watch for s in mgr.sessions.values() if s._watch]
        await mgr.revoke_all()
        await asyncio.gather(*watches, return_exceptions=True)
        self.env.stop()
        self.tmp.cleanup()

    async def test_success_cookie_no_secret_in_results_and_revoke(self):
        put(self.settings, self.origin, "alice", "TOP_SECRET_TEST")
        from personal_tools.browser_login import login_adapter
        with self.assertNoLogs(level="WARNING"):
            result = await login_adapter(self.settings, self.origin, operator_id="owner")
        self.assertEqual(result.text, "logged_in")
        self.assertTrue(self.http.password_seen)
        self.assertEqual(self.http.posts, 1)
        self.assertNotIn("TOP_SECRET_TEST", str(result))
        self.assertNotIn("TOP_SECRET_TEST", str(public_audit(self.settings, self.origin)))
        mgr = manager_for(self.settings)
        self.assertEqual(len(mgr.sessions), 1)
        # A second browser.login from another operator is BUSY and must not
        # launch a browser, send a second POST, or burn a password attempt.
        second = await asyncio.wait_for(
            login_adapter(self.settings, self.origin, operator_id="other"), 3)
        self.assertEqual(second.text, "failed (busy)")
        self.assertEqual(self.http.posts, 1)
        self.assertEqual(len(mgr.sessions), 1)
        self.assertEqual(len(public_audit(self.settings, self.origin)), 1)
        s = next(iter(mgr.sessions.values()))
        self.assertGreater(len(await wait_for_step(
            s.context.cookies([self.origin]), 3, "test_success.cookies")), 0)
        path = s.directory
        await mgr.revoke(s.id, operator=s.operator)
        self.assertFalse(path.exists())

    async def test_mismatched_origin_lookalike_iframe_action_never_fills(self):
        for path in ("/cross", "/iframe"):
            put(self.settings, self.origin, "alice", "TOP_SECRET_TEST",
                login_url=self.origin + path)
            result, reason = await login(self.settings, self.origin, "owner")
            self.assertEqual(result, "failed")
            self.assertEqual(self.http.posts, 0)
            self.assertFalse(self.http.password_seen)
        result, reason = await login(self.settings, "https://localhost.evil.example", "owner")
        self.assertEqual((result, reason), ("failed", "domain_denied"))
        self.assertEqual(self.http.posts, 0)

    async def test_mfa_needs_human_keeps_same_session_and_completes_otp(self):
        from human_takeover import HumanTakeoverStore
        put(self.settings, self.origin, "alice", "TOP_SECRET_TEST")
        self.http.challenge = True
        # Real WebTakeover.start records a real protected lease; only the
        # external desktop sidecar and unrelated computer steps are mocked.
        with patch("web_takeover.read_transport_gate", return_value=None), \
             patch("web_takeover.allow_transport"), \
             patch("web_takeover.cancel_pending_computer_steps"), \
             patch("web_takeover.cancel_pending_observe_requests"), \
             patch("web_takeover.has_claimed_computer_steps", return_value=False), \
             patch("web_takeover.has_claimed_observe_requests", return_value=False):
            result, reason = await login(self.settings, self.origin, "owner")
        self.assertEqual((result, reason), ("needs_human", "authentication_challenge"))
        manager = manager_for(self.settings)
        self.assertEqual(len(manager.sessions), 1)
        session = next(iter(manager.sessions.values()))
        lease_store = HumanTakeoverStore(self.settings)
        lease = lease_store.current()
        self.assertIsNotNone(lease)
        self.assertEqual(lease["reason"], "login")
        self.assertEqual(lease["task_id"], session.task)
        self.assertFalse(session._automated_login)
        self.assertIsNone(session._autofill_target)
        with self.assertRaises(ExternalPolicyDenied):
            session.ensure(agent=True)
        # The pending lease cannot authorize OTP submit until activated.
        self.assertFalse(session._human_active())
        lease_store.activate(lease["id"])
        self.assertTrue(session._human_active())
        with self.assertRaises(ExternalPolicyDenied):
            await session.type("otp", "123456")
        # Simulate the protected human desktop completing the OTP in the
        # *same* Chromium page, not a newly launched or cookie-less browser.
        await session.page.locator('input[autocomplete="one-time-code"]').fill("123456")
        await session.page.locator('button[type=submit]').click()
        await session.page.wait_for_load_state("domcontentloaded")
        cookies = await session.context.cookies([self.origin])
        self.assertTrue(any(c["name"] == "session" for c in cookies))
        self.assertEqual(self.http.posts, 2)
        path = session.directory
        self.assertTrue(await manager.revoke(session.id, operator=session.operator))
        self.assertFalse(path.exists())
        lease_store.complete(lease["id"])

    async def test_failed_login_locks_after_three_attempts(self):
        put(self.settings, self.origin, "alice", "TOP_SECRET_TEST")
        self.http.reject = True
        rejections = 0
        # A transient pre-submit browser failure cannot use up a password
        # attempt. Retry it, while preserving the exact HTTP POST assertion.
        for _ in range(5):
            result, reason = await wait_for_step(
                login(self.settings, self.origin, "owner"), 19, "test.lockout")
            self.assertEqual(result, "failed")
            if reason == "credentials_rejected":
                rejections += 1
                self.assertEqual(self.http.posts, rejections)
            else:
                self.assertIn(reason, ("login_open_timeout", "login_form_timeout",
                                       "login_unavailable", "login_not_confirmed"))
            if rejections == 3:
                break
        self.assertEqual(rejections, 3, "three verified rejections required")
        self.assertEqual(self.http.posts, 3)
        result, reason = await wait_for_step(
            login(self.settings, self.origin, "owner"), 3, "test.lockout_denial")
        self.assertEqual((result, reason), ("failed", "locked"))
        self.assertEqual(self.http.posts, 3)
        audit = public_audit(self.settings, self.origin)
        self.assertEqual(sum(x["reason"] == "credentials_rejected" and
                             x["submitted"] for x in audit), 3)

    async def test_forced_pre_submit_timeout_does_not_consume_password_quota(self):
        from browser_auth_sessions import AuthSession
        put(self.settings, self.origin, "alice", "TOP_SECRET_TEST")
        self.http.reject = True
        # Do not even start Chromium: this is an injected browser-open failure.
        async def timeout_before_post(_session):
            raise asyncio.TimeoutError("fixture forced pre-submit timeout")
        with patch.object(AuthSession, "start", timeout_before_post):
            for _ in range(3):
                result, reason = await wait_for_step(
                    login(self.settings, self.origin, "owner"), 3,
                    "test.forced_open_timeout")
                self.assertEqual((result, reason), ("failed", "login_open_timeout"))
        self.assertEqual(self.http.posts, 0)
        audit = public_audit(self.settings, self.origin)
        self.assertEqual(len(audit), 3)
        self.assertTrue(all(x["reason"] == "login_open_timeout" and
                            not x["submitted"] for x in audit))
        # A real rejected password remains permitted after those timeouts.
        result, reason = await wait_for_step(
            login(self.settings, self.origin, "owner"), 19,
            "test.after_open_timeout")
        self.assertEqual((result, reason), ("failed", "credentials_rejected"))
        self.assertEqual(self.http.posts, 1)
        self.assertEqual(len(public_audit(self.settings, self.origin)), 4)
