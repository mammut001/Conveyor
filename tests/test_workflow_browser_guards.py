"""Unit and opt-in Chromium regressions for independent browser guards."""
from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import os
import socketserver
import threading
import unittest

from workflow_browser_guards import (
    EXTRA_INIT_SCRIPT, inspect_cdp_dom, inspect_page_via_cdp,
)
from workflow_browser_contract import parse_plan
from workflow_browser_journal import DENIAL_CODES
from workflow_browser_playwright import (
    BrowserPolicyError, FIXTURE_ROOT, LocalPlaywrightBrowser,
)


class CdpGuardUnitTests(unittest.TestCase):
    def test_init_script_exposes_sticky_denials_for_all_api_families(self):
        for api in ("Worker", "SharedWorker", "ServiceWorkerContainer",
                    "open", "write", "writeln"):
            with self.subTest(api=api):
                self.assertIn(api, EXTRA_INIT_SCRIPT)
        self.assertIn("__conveyorUnsafeDOMReason", EXTRA_INIT_SCRIPT)
        self.assertIn("worker_denied", EXTRA_INIT_SCRIPT)
        self.assertIn("document_write_denied", EXTRA_INIT_SCRIPT)

    def test_new_denial_codes_are_journal_allowlisted(self):
        self.assertTrue({"worker_denied", "document_write_denied",
                         "cdp_dom_unavailable"}.issubset(DENIAL_CODES))

    def test_closed_shadow_root_sensitive_input_is_seen(self):
        root = {
            "nodeName": "#document",
            "shadowRoots": [{
                "nodeName": "#document-fragment",
                "shadowRootType": "closed",
                "children": [{
                    "nodeName": "INPUT",
                    "attributes": ["type", "password"],
                }],
            }],
        }
        self.assertEqual(inspect_cdp_dom(root), "sensitive_or_submit_dom_denied")

    def test_nested_template_content_and_sensitive_autocomplete(self):
        root = {
            "nodeName": "#document",
            "children": [{
                "nodeName": "DIV",
                "templateContent": {
                    "nodeName": "#document-fragment",
                    "children": [{
                        "localName": "input",
                        "attributes": ["autocomplete", "shipping cc-number"],
                    }],
                },
            }],
        }
        self.assertEqual(inspect_cdp_dom(root), "sensitive_autocomplete_denied")

    def test_submit_contenteditable_declarative_template_and_otp(self):
        cases = [
            ({"nodeName": "BUTTON", "attributes": []},
             "sensitive_or_submit_dom_denied"),
            ({"nodeName": "DIV", "attributes": ["contenteditable", "false"]},
             "sensitive_or_submit_dom_denied"),
            ({"nodeName": "TEMPLATE", "attributes": ["shadowrootmode", "closed"]},
             "declarative_shadow_denied"),
            ({"nodeName": "INPUT", "attributes": ["autocomplete", "one-time-code"]},
             "sensitive_autocomplete_denied"),
            ({"nodeName": "FORM", "attributes": ["action", "/unsafe"]},
             "sensitive_or_submit_dom_denied"),
        ]
        for node, reason in cases:
            with self.subTest(node=node):
                self.assertEqual(inspect_cdp_dom(node), reason)

    def test_safe_tree_and_malformed_tree(self):
        self.assertIsNone(inspect_cdp_dom({
            "nodeName": "#document", "children": [{
                "nodeName": "INPUT",
                "attributes": ["type", "text", "name", "purpose"],
            }],
        }))
        for node in (None, {"attributes": ["type"]},
                     {"children": "not-a-list"}, {"shadowRoots": [None]}):
            with self.subTest(node=node):
                self.assertEqual(inspect_cdp_dom(node), "cdp_dom_unavailable")

    def test_cdp_failure_is_fail_closed(self):
        class FailingContext:
            def new_cdp_session(self, _page):
                raise RuntimeError("CDP blocked")

        class FailingPage:
            context = FailingContext()

        self.assertEqual(inspect_page_via_cdp(FailingPage()), "cdp_dom_unavailable")


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class _SocketCounter(socketserver.BaseRequestHandler):
    def handle(self):
        with self.server.counter_lock:
            self.server.accepted += 1


class ChromiumGuardTests(unittest.TestCase):
    def setUp(self):
        required = os.environ.get("CONVEYOR_REQUIRE_REAL_BROWSER") == "1"
        if os.environ.get("CONVEYOR_REAL_BROWSER") != "1":
            if required:
                self.fail("CONVEYOR_REQUIRE_REAL_BROWSER needs CONVEYOR_REAL_BROWSER=1")
            self.skipTest("real-browser opt-in disabled")
        if importlib.util.find_spec("playwright") is None:
            if required:
                self.fail("Playwright missing in required-real-browser CI")
            self.skipTest("Playwright not installed")

        self.http_server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            partial(_QuietHandler, directory=str(FIXTURE_ROOT)),
        )
        self.http_thread = threading.Thread(
            target=self.http_server.serve_forever, daemon=True,
        )
        self.http_thread.start()
        self.addCleanup(self.http_thread.join, 2)
        self.addCleanup(self.http_server.server_close)
        self.addCleanup(self.http_server.shutdown)

        self.origin = f"http://127.0.0.1:{self.http_server.server_port}"
        self.browser = LocalPlaywrightBrowser(origin=self.origin)
        try:
            self.browser.__enter__()
        except Exception as exc:
            if "Executable doesn't exist" in str(exc):
                if required:
                    self.fail("Chromium executable missing in required-real-browser CI")
                self.skipTest("Chromium executable not installed")
            raise
        self.addCleanup(self.browser.close)
        self.browser.apply(parse_plan({
            "plan_id": "browser_guards_001",
            "fixture": "visitor600_mock",
            "actions": [{
                "kind": "open", "on_page": "idle", "target": "welcome",
                "expect_page": "welcome",
            }],
        }).actions[0])

    def _expect_sticky_denial(self, reason):
        with self.assertRaisesRegex(BrowserPolicyError, reason):
            self.browser._check()
        self.assertTrue(self.browser._violated)
        self.assertEqual(self.browser._denial_reason, reason)

    def test_worker_websocket_never_reaches_real_loopback_listener(self):
        listener = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SocketCounter)
        listener.daemon_threads = True
        listener.accepted = 0
        listener.counter_lock = threading.Lock()
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)

        self.browser._tab.evaluate("""port => {
            const uri = 'ws://127.0.0.1:' + port + '/socket';
            const code = 'new WebSocket(' + JSON.stringify(uri) + ');';
            const url = URL.createObjectURL(new Blob([code], {
              type: 'text/javascript'
            }));
            try { new Worker(url); } catch (error) {}
            URL.revokeObjectURL(url);
        }""", listener.server_address[1])
        self.browser._tab.wait_for_timeout(250)
        with listener.counter_lock:
            self.assertEqual(listener.accepted, 0, "Worker reached real TCP listener")
        self._expect_sticky_denial("worker_denied")

    def test_shared_worker_constructor_denied(self):
        self.browser._tab.evaluate("""() => {
            try { new SharedWorker('data:text/javascript,postMessage(1)'); }
            catch (error) {}
        }""")
        self._expect_sticky_denial("worker_denied")

    def test_service_worker_registration_denied_when_api_available(self):
        # Playwright's service_workers="block" may install an own-property
        # no-op register on navigator.serviceWorker, hiding the prototype
        # override in EXTRA_INIT_SCRIPT. Assert the security invariant:
        # no successful registration and no service worker in this context.
        state = self.browser._tab.evaluate("""async () => {
            if (!navigator.serviceWorker) return {available: false, count: 0};
            try { await navigator.serviceWorker.register('/sw.js'); }
            catch (error) {}
            const registrations = await navigator.serviceWorker.getRegistrations();
            return {available: true, count: registrations.length};
        }""")
        self.assertEqual(state["count"], 0, "Service worker registered despite policy")
        self.assertEqual(self.browser._context.service_workers, [],
                         "Browser context started a service worker")
        # If our script intercepted the call, its sticky worker denial must
        # still be enforced. With Playwright's no-op override, no registration
        # is the required outcome, without depending on the marker.
        reason = self.browser._tab.evaluate(
            "() => window.__conveyorUnsafeDOMReason || null"
        )
        if reason is not None:
            self.assertEqual(reason, "worker_denied")
            self._expect_sticky_denial("worker_denied")

    def test_document_write_cannot_create_declarative_closed_password_root(self):
        self.browser._tab.evaluate("""() => {
            try {
                document.write('<div><template shadowrootmode="closed">' +
                    '<input type="password"></template></div>');
            } catch (error) {}
        }""")
        self._expect_sticky_denial("document_write_denied")

    def test_document_open_and_writeln_denied(self):
        for method in ("open", "writeln"):
            with self.subTest(method=method):
                # Use an unpoisoned page for the second attempt by checking the
                # sticky marker only after both methods have been exercised.
                if method == "open":
                    self.browser._tab.evaluate("""() => {
                        try { document.open(); } catch (error) {}
                    }""")
                else:
                    self.browser._tab.evaluate("""() => {
                        try { document.writeln('unsafe'); } catch (error) {}
                    }""")
        self._expect_sticky_denial("document_write_denied")

    def test_live_cdp_detects_password_control(self):
        self.browser._tab.evaluate("""() => {
            const el = document.createElement('input');
            el.type = 'password';
            document.body.append(el);
        }""")
        self.assertEqual(inspect_page_via_cdp(self.browser._tab),
                         "sensitive_or_submit_dom_denied")
        self._expect_sticky_denial("sensitive_or_submit_dom_denied")


if __name__ == "__main__":
    unittest.main()
