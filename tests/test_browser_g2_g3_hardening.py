"""Unit and rendered-DOM regressions for G2/G3 browser hardening."""
from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from browser_g3_form_policy import sensitive_form
from browser_g3_kill import active, refusal
from handlers.tools.confirm import clear_all_pending, create_pending
from workflow_browser_g3_store import save, review, advance, state, claim, list_reconciliation, _conn
from workflow_browser_external_identity import ExternalPolicyDenied


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = SimpleNamespace(
            codex_memory_root=Path(self.temp.name), approval_inbox_enabled=True,
            approval_relay_enabled=False, telegram_bot_token="redacted-test-bot",
        )
        clear_all_pending()
        self.addCleanup(clear_all_pending)

    def sample(self):
        preview = {"url":"https://demo.example/form", "target":"https://demo.example/send",
                   "method":"POST", "fields":[["topic","hello"]]}
        return save(self.settings, operator_id="operator", url=preview["url"],
                    form_ref=0, fields={"topic":"hello"},
                    preview=preview, screenshot=b"\x89PNG small test")

    def test_missing_and_expired_preview_never_show_bare_tool(self):
        import approval_inbox
        for expired in (False, True):
            token, digest = self.sample()
            if expired:
                with _conn(self.settings) as conn:
                    conn.execute("UPDATE browser_g3_submits SET expires_at=0 WHERE token=?", (token,))
            pending = create_pending("browser.g3_dispatch", json.dumps({"token":token,"hash":digest}),
                operator_id="operator", chat_id="test", channel="web")
            item = next(x for x in approval_inbox.list_items(self.settings, SimpleNamespace(
                list_approvals=lambda: [])) if x["id"] == pending.token)
            if expired:
                self.assertIn("preview unavailable", item["summary"].lower())
                self.assertIn("do not approve", item["summary"].lower())
            else:
                self.assertIn("G3 fresh", item["summary"])
            clear_all_pending()
        pending = create_pending("browser.g3_dispatch", json.dumps({"token":"a"*32,"hash":"b"*64}),
            operator_id="operator", chat_id="test", channel="web")
        item = next(x for x in approval_inbox.list_items(self.settings, SimpleNamespace(
            list_approvals=lambda: [])) if x["id"] == pending.token)
        self.assertIn("do not approve", item["summary"].lower())

    def test_crash_recovery_is_durable_and_never_replayable(self):
        token, hash_value = self.sample()
        advance(self.settings, token, "pending", "verifying")
        advance(self.settings, token, "verifying", "dispatched")
        self.assertEqual(state(self.settings, token), "dispatched")  # same process may complete
        with _conn(self.settings) as conn:
            conn.execute("UPDATE browser_g3_submits SET dispatch_owner='killed-process' WHERE token=?", (token,))
        self.assertEqual(state(self.settings, token), "needs_reconciliation")
        self.assertIsNone(review(self.settings, token))
        with self.assertRaises(ExternalPolicyDenied):
            claim(self.settings, token, "operator", hash_value)
        self.assertEqual(len(list_reconciliation(self.settings)), 1)
        self.assertIn(token, list_reconciliation(self.settings)[0]["token"])
        from browser_g3_reconciliation import inbox_items
        items = inbox_items(self.settings)
        self.assertEqual(items[0]["kind"], "reconciliation")
        self.assertIn("NEVER retry", items[0]["summary"])
        self.assertEqual(state(self.settings, token), "needs_reconciliation")

    def test_legacy_dispatched_without_owner_reconciles(self):
        token, _ = self.sample()
        with _conn(self.settings) as conn:
            conn.execute("UPDATE browser_g3_submits SET state='dispatched',dispatch_owner=NULL WHERE token=?", (token,))
        self.assertEqual(state(self.settings, token), "needs_reconciliation")

    def test_live_sibling_process_is_not_falsely_reconciled(self):
        token, _ = self.sample()
        with _conn(self.settings) as conn:
            conn.execute("""UPDATE browser_g3_submits
                SET state='dispatched',dispatch_owner='123456789-other'
                WHERE token=?""", (token,))
        with patch("workflow_browser_g3_recovery.os.kill", return_value=None) as probe:
            self.assertEqual(state(self.settings, token), "dispatched")
            probe.assert_called()
        with patch("workflow_browser_g3_recovery.os.kill", side_effect=ProcessLookupError):
            self.assertEqual(state(self.settings, token), "needs_reconciliation")

    def test_form_classifier_ignores_unrelated_footer(self):
        safe = {"target":"https://demo.example/feedback", "heading":"Public feedback",
                "submit_label":"Send"}
        self.assertFalse(sensitive_form("https://demo.example/form",safe))
        for form in (
            dict(safe, heading="Sign in to your account"),
            dict(safe, submit_label="Delete my account"),
            dict(safe, target="https://demo.example/checkout"),
            dict(safe, submit_label="Pay now"),
            dict(safe, submit_label="Pay"),
            dict(safe, target="https://demo.example/delete"),
        ):
            self.assertTrue(sensitive_form("https://demo.example/form",form))
        self.assertTrue(sensitive_form("https://demo.example/login",safe))

    def test_unicode_normalization_and_sensitive_control_labels(self):
        base = {"target":"https://demo.example/send","heading":"Public feedback",
                "submit_label":"Send","controls":[]}
        self.assertFalse(sensitive_form("https://demo.example/form", base))
        for label in ("Sign\u200b in", "Log\u00adin", "ｐａｙ now",
                      "One-time code", "card number", "security code", "Email"):
            with self.subTest(label=label):
                self.assertTrue(sensitive_form("https://demo.example/form",
                    {**base,"controls":[{"labels":[label]}]}))
        for label in ("Sign\u200b in","Log\u00adin","ｐａｙ now","Close your account"):
            with self.subTest(submit=label):
                self.assertTrue(sensitive_form("https://demo.example/form",
                    {**base,"submit_label":label}))
        self.assertFalse(sensitive_form("https://demo.example/form",
            {**base,"controls":[{"labels":["Topic"],"placeholder":"Message"}]}))

    def test_visible_context_and_card_shaped_field_data(self):
        safe={"target":"https://demo.example/send","heading":"Public feedback",
              "preceding_text":"Public feedback","submit_label":"Send","controls":[]}
        self.assertFalse(sensitive_form("https://demo.example/form", safe))
        for context in ("Checkout Your details","Payment","Sign in","ｐａｙ"):
            with self.subTest(context=context):
                self.assertTrue(sensitive_form("https://demo.example/form",
                    {**safe,"preceding_text":context}))
        for key in ("placeholder","value"):
            for number in ("4111111111111111","4111 1111 1111 1111",
                           "4111-1111-1111-1111","378282246310005"):
                with self.subTest(key=key,number=number):
                    self.assertTrue(sensitive_form("https://demo.example/form",
                        {**safe,"controls":[{key:number}]}))
        for number in ("123456789012", "12345678901234567890", "2026-10-12"):
            with self.subTest(non_card=number):
                self.assertFalse(sensitive_form("https://demo.example/form",
                    {**safe,"controls":[{"value":number}]}))

    def test_kill_refuses_approvals_and_custom_file_read(self):
        custom=Path(self.temp.name)/"external.kill"
        with patch.dict(os.environ, {"CONVEYOR_BROWSER_EXTERNAL_KILL_FILE":str(custom)}):
            self.assertFalse(active(self.settings))
            self.assertIsNone(refusal(self.settings,"browser.g3_dispatch"))
            custom.touch()
            self.assertTrue(active(self.settings))
            self.assertIn("kill switch is ON",refusal(self.settings,"browser.g3_dispatch"))
            self.assertIn("kill switch is ON",refusal(self.settings,"browser.browse_site"))
            self.assertIsNone(refusal(self.settings,"notes.write"))
            custom.unlink()
            custom.symlink_to(Path(self.temp.name)/"target")
            self.assertIn("unsafe",refusal(self.settings,"browser.read_webpage"))

    def test_telegram_screenshot_requires_explicit_opt_in(self):
        from browser_g3_telegram import send_preview
        token, digest = self.sample()
        arg=json.dumps({"token":token,"hash":digest})
        with patch.dict(os.environ, {"CONVEYOR_BROWSER_G3_TELEGRAM_SCREENSHOT":"0"}), \
             patch("urllib.request.urlopen") as urlopen:
            self.assertFalse(send_preview(self.settings,"123",arg))
            urlopen.assert_not_called()
        with patch.dict(os.environ, {"CONVEYOR_BROWSER_G3_TELEGRAM_SCREENSHOT":"1"}), \
             patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"ok":true}')) as urlopen:
            self.assertTrue(send_preview(self.settings,"123",arg))
            self.assertIn(b"\x89PNG", urlopen.call_args.args[0].data)


class RenderedLinkTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_excludes_hidden_and_zero_size_descendants(self):
        required=os.getenv("CONVEYOR_REQUIRE_REAL_BROWSER")=="1"
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            if required: self.fail("Chromium required")
            self.skipTest("Playwright not installed")
        from workflow_browser_g2 import LINKS_JS
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page(viewport={"width":800,"height":600})
                await page.set_content("""<a href="/next" style="display:block;width:300px;height:50px">
                    Next <span style="display:none">DECOY</span>
                    <span style="visibility:hidden">HIDDEN</span>
                    <span style="opacity:0">TRANSPARENT</span>
                    <span style="font-size:0">ZERO</span>Page</a>""")
                links=await page.locator("a").evaluate_all(LINKS_JS)
                self.assertEqual(len(links),1)
                self.assertEqual(links[0]["text"],"Next Page")
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
