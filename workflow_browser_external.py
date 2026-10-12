"""G1 only: explicitly approved read-only external Chromium session.

NOT a workflow executor. No selectors, clicks, forms, evaluate, credentials, or
automatic tool invocation. Text returned from sites is adversarial DATA.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path

from workflow_browser_external_approvals import ApprovalLedger
from workflow_browser_external_identity import (
    ExternalPolicyDenied, canonical_url, resolve_public_ipv4,
)
from workflow_browser_egress import EgressAudit, EgressAuditUnavailable
from workflow_browser_guards import EXTRA_INIT_SCRIPT


@dataclass(frozen=True)
class UntrustedPageText:
    source_origin: str
    text: str
    trust_level: str = "untrusted_page_data"


class ExternalReadOnlyBrowser:
    """Use 'async with' per task; never reuse Chromium, context or profile."""

    def __init__(self, *, task: str, ledger: ApprovalLedger,
                 audit: EgressAudit, kill_file: str | Path | None = None):
        if os.getenv("CONVEYOR_REAL_BROWSER") != "1" or \
                os.getenv("CONVEYOR_BROWSER_EXTERNAL_READONLY") != "1":
            raise ExternalPolicyDenied("external_browser_disabled")
        self.task, self.ledger, self.audit = task, ledger, audit
        self.site = ledger.require(task)
        self.kill_file = Path(kill_file or os.getenv(
            "CONVEYOR_BROWSER_EXTERNAL_KILL_FILE",
            "/tmp/conveyor-browser-external.kill"))
        self._pw = self._browser = self._context = self._page = None
        self._watcher = None
        self._violation = None
        self._active_url = None
        self._requests = 0
        self._closed = False

    def _ensure_live(self):
        if self._violation:
            raise ExternalPolicyDenied(self._violation)
        if os.getenv("CONVEYOR_REAL_BROWSER") != "1" or \
                os.getenv("CONVEYOR_BROWSER_EXTERNAL_READONLY") != "1":
            raise ExternalPolicyDenied("external_browser_disabled")
        if self.kill_file.exists():
            raise ExternalPolicyDenied("external_kill_switch")
        current = self.ledger.require(self.task)
        if current != self.site:
            raise ExternalPolicyDenied("approval_changed")

    @staticmethod
    def _context_options():
        return dict(accept_downloads=False, service_workers="block",
                    permissions=[], java_script_enabled=False,
                    ignore_https_errors=False,
                    storage_state={"cookies": [], "origins": []})

    async def __aenter__(self):
        self._ensure_live()
        # Blocking DNS lookup only once, before Chromium is started.
        pinned_ip = await asyncio.to_thread(
            resolve_public_ipv4, self.site.host, self.site.port)
        self._ensure_live()
        from playwright.async_api import async_playwright
        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True, args=[
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-extensions",
                    "--no-proxy-server",
                    "--disable-features=DnsOverHttps",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    f"--host-resolver-rules=MAP {self.site.host} {pinned_ip}, MAP * ~NOTFOUND",
                ])
            self._context = await self._browser.new_context(**self._context_options())
            if not hasattr(self._context, "route_web_socket"):
                raise ExternalPolicyDenied("websocket_routing_unavailable")
            await self._context.route("**/*", self._route)
            await self._context.route_web_socket("**/*", self._websocket)
            await self._context.add_init_script(EXTRA_INIT_SCRIPT)
            self._page = await self._context.new_page()
            self._page.on("popup", lambda page: self._poison("popup_denied"))
            self._page.on("download", lambda dl: self._poison("download_denied"))
            self._page.on("response", self._response)
            self._watcher = asyncio.create_task(self._watch_revocation())
            self._ensure_live()
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *_):
        await self.close()

    def _poison(self, reason: str):
        if self._violation is None:
            self._violation = reason

    def _audit(self, method: str, outcome: str, reason: str):
        self.audit.record(
            self.task, self.site.host,
            method if method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
            else "OTHER",
            outcome, reason,
        )

    async def _route(self, route):
        request = route.request
        method = request.method
        try:
            self._ensure_live()
            self.site.check_url(request.url, method)
            if (request.redirected_from is not None or
                    self._active_url != request.url):
                raise ExternalPolicyDenied("redirect_denied")
            if (request.resource_type != "document"
                    or not request.is_navigation_request()
                    or request.frame != self._page.main_frame):
                raise ExternalPolicyDenied("subresource_denied")
            headers = await request.all_headers()
            if (request.post_data is not None or any(
                    header in headers for header in (
                        "cookie", "authorization", "proxy-authorization",
                        "x-api-key", "referer"))):
                raise ExternalPolicyDenied("credentials_denied")
            self._requests += 1
            if self._requests > 4:
                raise ExternalPolicyDenied("request_budget_denied")
            # Must commit the audit row BEFORE allowing any network operation.
            self._audit(method, "allowed", "approved_readonly")
            self._ensure_live()
            await route.continue_()
            return
        except ExternalPolicyDenied as exc:
            reason = str(exc)
        except EgressAuditUnavailable:
            reason = "audit_storage_unavailable"
        except Exception:
            reason = "request_filter_error"
        self._poison(reason)
        try:
            self._audit(method, "denied", reason)
        except Exception:
            self._poison("audit_storage_unavailable")
        try:
            await route.abort()
        except Exception:
            pass

    async def _websocket(self, _ws):
        self._poison("websocket_denied")
        try:
            self._audit("OTHER", "denied", "websocket_denied")
        except Exception:
            self._poison("audit_storage_unavailable")
        # No connect_to_server() means no WebSocket handshake.

    async def _response(self, response):
        headers = response.headers
        if 300 <= response.status < 400:
            reason = "redirect_denied"
        elif "set-cookie" in headers:
            reason = "cookies_denied"
        elif "attachment" in headers.get("content-disposition", "").lower():
            reason = "download_denied"
        elif response.status == 401 or response.status == 407:
            reason = "auth_denied"
        elif not headers.get("content-type", "").lower().startswith(
                ("text/html", "text/plain")):
            reason = "nontext_response_denied"
        else:
            return
        self._poison(reason)
        try:
            self._audit(response.request.method, "denied", reason)
        except Exception:
            self._poison("audit_storage_unavailable")

    async def _watch_revocation(self):
        while not self._closed:
            await asyncio.sleep(0.05)
            try:
                self._ensure_live()
            except Exception as exc:
                self._poison(str(exc) if isinstance(exc, ExternalPolicyDenied)
                             else "approval_store_unavailable")
            if self._violation:
                if self._context:
                    try:
                        await self._context.close()
                    except Exception:
                        pass
                return

    async def read(self, url: str) -> UntrustedPageText:
        try:
            self._ensure_live()
            self.site.check_url(url, "GET")
            # Require exactly the canonical requested URL, including paths.
            host, port, path = canonical_url(url)
            if url != self.site.origin + path:
                raise ExternalPolicyDenied("url_denied")
        except ExternalPolicyDenied as exc:
            self._audit("GET", "denied", str(exc))
            raise
        if self._page is None or self._active_url is not None:
            raise ExternalPolicyDenied("session_already_used")
        self._active_url = url
        try:
            response = await self._page.goto(url, wait_until="domcontentloaded",
                                             timeout=10000)
            self._ensure_live()
            if response is None or response.status != 200 or self._page.url != url:
                raise ExternalPolicyDenied("navigation_denied")
            text = await self._page.locator("body").inner_text(timeout=2000)
            self._ensure_live()
            return UntrustedPageText(self.site.origin, text[:65536])
        except ExternalPolicyDenied:
            raise
        except Exception as exc:
            raise ExternalPolicyDenied(
                self._violation or "navigation_denied") from exc

    async def close(self):
        self._closed = True
        if self._watcher and self._watcher is not asyncio.current_task():
            self._watcher.cancel()
            try:
                await self._watcher
            except asyncio.CancelledError:
                pass
        for target in (self._context, self._browser, self._pw):
            if target:
                try:
                    await (target.stop() if target is self._pw else target.close())
                except Exception:
                    pass
        self._context = self._browser = self._pw = self._page = None
