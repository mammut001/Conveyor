"""Opt-in, no-credentials Playwright port for the bundled static localhost fixture.

Not a general browser provider; every network response is attested against the
checked-in fixture bytes. Never expose this port to arbitrary workflow input.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit

from workflow_browser_contract import BrowserAction

FIXTURE_ROOT = Path(__file__).resolve().parent / "tests" / "fixtures" / "browser_portal"
ALLOWED_FILES = frozenset({"welcome.html", "details.html", "review.html"})


class BrowserPolicyError(Exception):
    """Fail-closed, fixed-code browser policy violation."""


def real_browser_enabled() -> bool:
    return os.environ.get("CONVEYOR_REAL_BROWSER") == "1"


def validate_fixture_origin(origin: str) -> str:
    """Explicit loopback HTTP origin with port; never infer a remote origin."""
    if not isinstance(origin, str):
        raise BrowserPolicyError("invalid_fixture_origin")
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError as exc:
        raise BrowserPolicyError("invalid_fixture_origin") from exc
    if (parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost")
            or parts.username is not None or parts.password is not None
            or parts.path not in ("", "/") or parts.query or parts.fragment
            or port is None or not (1 <= port <= 65535)
            or parts.netloc != f"{parts.hostname}:{port}"):
        raise BrowserPolicyError("invalid_fixture_origin")
    return f"http://{parts.hostname}:{port}"


class LocalPlaywrightBrowser:
    """Same apply/readback/page interface as FixtureBrowser; no generic selectors.

    Create only when CONVEYOR_REAL_BROWSER=1; run in a private Playwright
    browser context. Browser/Chromium are lazily imported in __enter__.
    """

    def __init__(self, *, origin: str) -> None:
        if not real_browser_enabled():
            raise BrowserPolicyError("real_browser_disabled")
        self.origin = validate_fixture_origin(origin)
        self._pw = None
        self._browser = None
        self._context = None
        self._tab = None
        self._violated = False
        self.invocations = 0

    def __enter__(self) -> "LocalPlaywrightBrowser":
        if not real_browser_enabled():
            raise BrowserPolicyError("real_browser_disabled")
        # Import lazily: base installation and all mock tests need no Playwright.
        from playwright.sync_api import sync_playwright
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._context = self._browser.new_context(
                accept_downloads=False, service_workers="block",
                permissions=[], java_script_enabled=True,
            )
            self._context.route("**/*", self._route)
            self._tab = self._context.new_page()
            self._tab.on("popup", self._reject_popup)
            self._tab.on("download", self._reject_download)
            self._tab.on("dialog", lambda dialog: dialog.dismiss())
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            try:
                if self._browser is not None:
                    self._browser.close()
            finally:
                if self._pw is not None:
                    self._pw.stop()
        self._context = self._browser = self._pw = self._tab = None

    def _reject_popup(self, page) -> None:
        self._violated = True
        page.close()

    def _reject_download(self, download) -> None:
        self._violated = True
        download.cancel()

    def _file_for_url(self, url: str) -> Path:
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise BrowserPolicyError("origin_denied") from exc
        if (not url.startswith(self.origin + "/") or
                f"{parts.scheme}://{parts.netloc}" != self.origin or
                parts.query or parts.fragment or parts.path.count("/") != 1 or
                parts.path[1:] not in ALLOWED_FILES):
            raise BrowserPolicyError("origin_denied")
        return FIXTURE_ROOT / parts.path[1:]

    def _route(self, route) -> None:
        # Enforce on *every* request, including redirects and subresources.
        try:
            expected = self._file_for_url(route.request.url).read_bytes()
            if route.request.method != "GET":
                raise BrowserPolicyError("unsafe_http_method")
            # Fetch only from the vetted local origin, with redirects disabled.
            response = route.fetch(max_redirects=0, timeout=5000)
            if response.status != 200 or hashlib.sha256(response.body()).digest() != hashlib.sha256(expected).digest():
                raise BrowserPolicyError("fixture_content_mismatch")
            route.fulfill(status=200, body=expected, content_type="text/html")
        except Exception:
            self._violated = True
            route.abort()

    def _check(self) -> None:
        if not real_browser_enabled():
            raise BrowserPolicyError("real_browser_disabled")
        if self._violated or self._tab is None or len(self._tab.frames) != 1:
            raise BrowserPolicyError("browser_policy_violation")
        if self._tab.url != "about:blank":
            self._file_for_url(self._tab.url)
            # Defence in depth: DOM changes must never expose credentials,
            # upload controls or a final-submit surface.
            forbidden = self._tab.locator(
                'input[type="password"], input[type="file"], '
                'input[type="submit"], button[type="submit"], '
                '[contenteditable="true"], form[action]'
            )
            if forbidden.count():
                raise BrowserPolicyError("sensitive_or_submit_dom_denied")

    @property
    def page(self) -> str:
        self._check()
        if self._tab.url == "about:blank":
            return "idle"
        path = urlsplit(self._tab.url).path[1:]
        name = path.removesuffix(".html")
        if name not in ("welcome", "details", "review"):
            raise BrowserPolicyError("page_not_allowlisted")
        marker = self._tab.locator("[data-fixture-page]")
        if marker.count() != 1 or marker.get_attribute("data-fixture-page") != name:
            raise BrowserPolicyError("fixture_marker_mismatch")
        return name

    def _field(self, name: str):
        if name not in ("purpose", "funding"):
            raise BrowserPolicyError("sensitive_field_denied")
        target = self._tab.locator(f'input[name="{name}"]')
        if (target.count() != 1 or
                target.get_attribute("type") != "text" or
                not target.is_visible() or not target.is_enabled()):
            raise BrowserPolicyError("unsafe_field")
        return target

    def field_matches(self, name: str, value: str) -> bool:
        current = self.page
        if current == "details":
            return self._field(name).input_value() == value
        if current == "review":
            target = self._tab.locator(f'[data-result-field="{name}"]')
            return target.count() == 1 and target.inner_text() == value
        return False

    def apply(self, op: BrowserAction) -> None:
        self._check()
        if self.page != op.on_page:
            raise BrowserPolicyError("page_precondition_failed")
        if op.kind == "open":
            if op.target != "welcome":
                raise BrowserPolicyError("target_denied")
            self._tab.goto(self.origin + "/welcome.html", wait_until="load", timeout=5000)
        elif op.kind == "click":
            if op.target != "next" or op.on_page not in ("welcome", "details"):
                raise BrowserPolicyError("target_denied")
            next_page = {"welcome": "details", "details": "review"}[op.on_page]
            target = self._tab.locator('a[data-action="next"]')
            if (target.count() != 1 or not target.is_visible() or
                    target.get_attribute("href") != f"/{next_page}.html"):
                raise BrowserPolicyError("unsafe_navigation_control")
            target.click(timeout=3000)
        elif op.kind == "fill":
            self._field(op.target).fill(op.value, timeout=3000)
        elif op.kind == "assert_field":
            if not self.field_matches(op.target, op.value):
                raise BrowserPolicyError("field_assertion_failed")
        elif op.kind == "assert_text":
            if op.target not in ("mock_welcome", "mock_review"):
                raise BrowserPolicyError("marker_denied")
        else:
            raise BrowserPolicyError("action_denied")
        self.invocations += 1
        self._check()

    def readback(self, op: BrowserAction) -> bool:
        try:
            if self.page != op.expect_page:
                return False
            if op.kind in ("fill", "assert_field"):
                return self.field_matches(op.target, op.value)
            if op.kind == "assert_text":
                target = self._tab.locator(f'[data-marker="{op.target}"]')
                return target.count() == 1 and target.is_visible()
            return True
        except BrowserPolicyError:
            return False
