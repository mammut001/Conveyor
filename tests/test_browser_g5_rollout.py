"""G5 manual rollout: no promotion by CI, independent signature and revocation."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import browser_g5_rollout as g5
import browser_g5_api
from workflow_browser_external_identity import ExternalPolicyDenied
from workflow_browser_external import ExternalReadOnlyBrowser
from workflow_browser_external_approvals import ApprovalLedger
from workflow_browser_egress import EgressAudit


ORIGIN = "https://approved.example.org"
REVIEW_KEY = "independent-review-private-key-not-a-production-secret"
EVIDENCE = ("g1", "g2", "g3", "g4", "os_capture", "dns", "injection",
            "dom_race", "session_bleed", "takeover_chaos", "egress_alerts",
            "rollback", "redaction")


class G5Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = self.root / "manifest.json"
        self.env = patch.dict(os.environ, {
            "CONVEYOR_BROWSER_G5": "1",
            "CONVEYOR_BROWSER_G5_REVIEW_KEY": REVIEW_KEY,
            "CONVEYOR_BROWSER_G5_MANIFEST": str(self.manifest),
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("CONVEYOR_BROWSER_G5_LIVE_ACK", None)

    def sign(self, **change):
        fields = {
            "version": 1, "origin": ORIGIN,
            "actions": ["read", "interact", "auth"],
            "reviewer": "independent-security-reviewer",
            "expires_at": time.time() + 3600,
            "builds": {"playwright": "1.58.0", "chromium": "pinned-build-123"},
            "evidence": {key: "a" * 64 for key in EVIDENCE},
            "retention_days": 14,
        }
        fields.update(change)
        signature = hmac.new(REVIEW_KEY.encode(), json.dumps(
            fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode(), hashlib.sha256).hexdigest()
        self.manifest.write_text(json.dumps(dict(fields, signature=signature)))
        self.manifest.chmod(0o600)

    def test_g5_flag_off_does_not_create_state_and_endpoints_404(self):
        with patch.dict(os.environ, {"CONVEYOR_BROWSER_G5": "0"}):
            g5.check(self.root, ORIGIN, "read")
            self.assertFalse((self.root / "g5-rollout.sqlite3").exists())
            self.assertFalse(browser_g5_api.get(object(), "/api/browser/g5/status"))
            self.assertFalse(browser_g5_api.post(
                object(), "/api/browser/g5/revoke", {"origin": ORIGIN}))

    def test_flag_alone_never_promotes_and_signed_manifest_required(self):
        g5.check(self.root, ORIGIN, "read")
        self.assertFalse(g5.status(self.root)["auto_promotion"])
        with patch.dict(os.environ, {"CONVEYOR_BROWSER_G5_LIVE_ACK": "1"}):
            with self.assertRaisesRegex(ExternalPolicyDenied, "g5_manifest_unavailable"):
                g5.check(self.root, ORIGIN, "read")
            self.sign()
            with patch("importlib.metadata.version", return_value="1.58.0"), \
                 patch("browser_g5_rollout._chromium_revision", return_value="pinned-build-123"):
                g5.check(self.root, ORIGIN, "read")
                g5.check(self.root, ORIGIN, "auth")
                with self.assertRaisesRegex(ExternalPolicyDenied, "g5_action_not_certified"):
                    g5.check(self.root, ORIGIN, "submit")
                with self.assertRaisesRegex(ExternalPolicyDenied, "g5_domain_not_certified"):
                    g5.check(self.root, "https://other.example.org", "read")
            doc = json.loads(self.manifest.read_text())
            doc["actions"].append("submit")
            self.manifest.write_text(json.dumps(doc))
            with self.assertRaisesRegex(ExternalPolicyDenied, "g5_review_signature_denied"):
                g5.check(self.root, ORIGIN, "read")

    def test_manifest_expiry_permissions_evidence_and_build_pin(self):
        with patch.dict(os.environ, {"CONVEYOR_BROWSER_G5_LIVE_ACK": "1"}):
            self.sign()
            self.manifest.chmod(0o644)
            with self.assertRaisesRegex(ExternalPolicyDenied, "g5_manifest_unsafe"):
                g5.check(self.root, ORIGIN, "read")
            self.manifest.chmod(0o600)
            with patch("importlib.metadata.version", return_value="different-build"):
                with self.assertRaisesRegex(ExternalPolicyDenied, "g5_playwright_version_changed"):
                    g5.check(self.root, ORIGIN, "read")
            self.sign(expires_at=time.time() - 1)
            with self.assertRaisesRegex(ExternalPolicyDenied, "g5_review_expired"):
                g5.check(self.root, ORIGIN, "read")
            self.sign(evidence={"g1": "a" * 64})
            with self.assertRaisesRegex(ExternalPolicyDenied, "g5_evidence_incomplete"):
                g5.check(self.root, ORIGIN, "read")
            self.sign(reviewer="ci")
            with self.assertRaisesRegex(ExternalPolicyDenied, "g5_independent_review_missing"):
                g5.check(self.root, ORIGIN, "read")

    def test_sticky_revocation_blocks_active_g1_before_network(self):
        db = self.root / "g1.sqlite3"
        ledger = ApprovalLedger(db)
        ledger.approve(task="case123", host="approved.example.org",
                       expires_at=time.time() + 180, approver="operator",
                       safe_paths=("/safe",))
        with patch.dict(os.environ, {"CONVEYOR_REAL_BROWSER": "1",
                                     "CONVEYOR_BROWSER_EXTERNAL_READONLY": "1"}):
            browser = ExternalReadOnlyBrowser(task="case123", ledger=ledger,
                                              audit=EgressAudit(db))
            browser._ensure_live()
            g5.revoke(self.root, ORIGIN)
            with self.assertRaisesRegex(ExternalPolicyDenied, "g5_domain_revoked"):
                browser._ensure_live()
            with self.assertRaisesRegex(ExternalPolicyDenied, "g5_domain_revoked"):
                g5.check(self.root, ORIGIN, "auth")
            self.assertIn(ORIGIN, [x["origin"] for x in g5.status(self.root)["revoked"]])

    def test_strict_origin_and_failure_closed(self):
        for url in ("https://APPROVED.example.org", "http://approved.example.org",
                    "https://approved.example.org/path",
                    "https://approved.example.org@evil.org",
                    "https://approved.example.org:443",
                    "https://127.0.0.1", "https://approved.example.org?x=1"):
            with self.subTest(url=url), self.assertRaises(ExternalPolicyDenied):
                g5.revoke(self.root, url)
        g5.revoke(self.root, ORIGIN)
        with patch("browser_g5_rollout.sqlite3.connect", side_effect=OSError("storage down")):
            with self.assertRaisesRegex(ExternalPolicyDenied, "g5_state_unavailable"):
                g5.check(self.root, ORIGIN, "read")

    def test_metrics_are_redacted_and_alerts_are_visible(self):
        from types import SimpleNamespace
        from personal_tools.browser_read import _db
        replies = []
        settings = SimpleNamespace(codex_memory_root=self.root)
        handler = SimpleNamespace(
            server=SimpleNamespace(control=SimpleNamespace(settings=settings)),
            _json=lambda code, body: replies.append((code, body)),
        )
        # Audit -> SQLite -> Web API: no mocked aggregation or page content.
        audit = EgressAudit(_db(settings))
        for _ in range(5):
            audit.record("task", "approved.example.org", "GET",
                         "denied", "request_filter_error")
        self.assertTrue(browser_g5_api.get(handler, "/api/browser/g5/metrics"))
        code, response = replies.pop()
        self.assertEqual(code, 200)
        self.assertEqual(response["recent_egress_count"], 5)
        self.assertEqual(response["denied"], 5)
        self.assertTrue(response["alert"])
        self.assertNotIn("query", response)

    def test_domain_revoke_api_is_durable_before_bounded_session_close(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        replies = []
        settings = SimpleNamespace(codex_memory_root=self.root)
        session = SimpleNamespace(id="session-id", operator="web-console", domain=ORIGIN)
        manager = SimpleNamespace(sessions={session.id: session},
                                  revoke=AsyncMock(return_value=True))
        handler = SimpleNamespace(
            server=SimpleNamespace(control=SimpleNamespace(settings=settings)),
            _await=lambda coro, timeout: asyncio.run(coro),
            _json=lambda code, body: replies.append((code, body)),
        )
        with patch("browser_auth_sessions.manager_for", return_value=manager):
            self.assertTrue(browser_g5_api.post(
                handler, "/api/browser/g5/revoke", {"origin": ORIGIN}))
        self.assertEqual(replies[-1][0], 200)
        self.assertFalse(replies[-1][1]["pending_cleanup"])
        manager.revoke.assert_awaited_once_with("session-id", operator="web-console")
        self.assertTrue(g5.revoked(self.root / "browser_external", ORIGIN))
        with self.assertRaisesRegex(ExternalPolicyDenied, "g5_domain_revoked"):
            g5.check(self.root / "browser_external", ORIGIN, "auth")
