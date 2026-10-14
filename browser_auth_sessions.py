"""G4 browser sessions: ephemeral identity, takeover-only human login, safe cleanup.

This is an opt-in, in-process context manager, NOT an agent tool. G3 remains
the only human-approval route for submits; G4 itself never submits forms.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
import time
from urllib.parse import urlsplit

from browser_auth_policy import enabled, ttl, origins, _origin, check_request, browser_pins
from browser_g3_kill import active as kill_active
from human_takeover import HumanTakeoverStore
from workflow_browser_external_identity import ExternalPolicyDenied
from workflow_browser_guards import EXTRA_INIT_SCRIPT, inspect_cdp_dom
from redaction import redact_text


def _private_root(settings):
    root = Path(settings.codex_memory_root) / "state" / "browser-auth-sessions"
    if root.is_symlink():
        raise ExternalPolicyDenied("g4_unsafe_root")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise ExternalPolicyDenied("g4_unsafe_root")
    return root


class AuthSession:
    def __init__(self, owner, operator, task, domain, directory):
        self.owner, self.operator, self.task, self.domain = owner, operator, task, domain
        self.id = secrets.token_hex(16)
        self.directory = directory
        self.expires_at = time.time() + ttl()
        self.context = None
        self.page = None
        self._closed = False
        self._watch = None
        self._violation = None
        self._in_action = asyncio.Lock()

    def _lease(self):
        return HumanTakeoverStore(self.owner.settings).current()

    def _human_active(self):
        lease = self._lease()
        if not (lease and lease["state"] == "human_active" and
                lease.get("reason") == "login" and lease.get("task_id") == self.task):
            return False
        if os.getenv("CONVEYOR_BROWSER_AUTH_TEST_LOOPBACK") == "1":
            return True  # local fixture only; production requires sidecar gate
        from web_takeover import read_transport_gate
        gate = read_transport_gate(self.owner.settings)
        return bool(gate and gate.get("session_id") == lease["id"])

    def ensure(self, *, agent=False):
        if self._closed or self._violation:
            raise ExternalPolicyDenied(self._violation or "g4_revoked")
        if not enabled() or self.domain not in origins():
            raise ExternalPolicyDenied("g4_disabled_or_domain_revoked")
        if time.time() >= self.expires_at or kill_active(self.owner.settings):
            raise ExternalPolicyDenied("g4_expired_or_killed")
        if agent and self._lease() is not None:
            raise ExternalPolicyDenied("g4_human_takeover_active")

    async def start(self):
        self.ensure()
        if not self._human_active():
            raise ExternalPolicyDenied("g4_login_takeover_required")
        # No existing browser state is imported. Chromium sees only this private
        # HOME; OS keychain, clipboard APIs and password-store integrations off.
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        self.owner._drivers[self.id] = pw
        headless = os.getenv("CONVEYOR_BROWSER_AUTH_TEST_LOOPBACK") == "1"
        if not headless and not os.getenv("DISPLAY"):
            raise ExternalPolicyDenied("g4_protected_display_missing")
        child_env = {
            "HOME": str(self.directory),
            "XDG_CONFIG_HOME": str(self.directory),
            "XDG_CACHE_HOME": str(self.directory),
            "LANG": "C.UTF-8",
            "PATH": os.getenv("PATH", "/usr/bin:/bin"),
        }
        if not headless:
            child_env["DISPLAY"] = os.environ["DISPLAY"]
            if os.environ.get("XAUTHORITY"):
                child_env["XAUTHORITY"] = os.environ["XAUTHORITY"]
        self.context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(self.directory / "profile"),
            headless=headless, env=child_env, accept_downloads=False,
            service_workers="block", permissions=[], ignore_https_errors=False,
            args=[
                "--disable-background-networking", "--no-first-run",
                "--disable-default-apps", "--disable-extensions",
                "--disable-sync", "--disable-quic", "--no-proxy-server",
                "--disable-features=DnsOverHttps,AutofillServerCommunication,PasswordManagerOnboarding,PasswordManager",
                "--password-store=basic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                *browser_pins(self.domain),
            ],
        )
        if not hasattr(self.context, "route_web_socket"):
            raise ExternalPolicyDenied("g4_websocket_guard_missing")
        await self.context.route("**/*", self._route)
        await self.context.route_web_socket("**/*", self._websocket)
        await self.context.add_init_script(EXTRA_INIT_SCRIPT + """
          Object.defineProperty(navigator,'clipboard',{configurable:false,
              get(){throw Error('clipboard_denied')}});
        """)
        self.context.on("page", self._new_page)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self._new_page(self.page)
        self._watch = asyncio.create_task(self._sweep())
        return self

    def _new_page(self, page):
        page.on("popup", lambda _: self._deny("g4_popup_denied"))
        page.on("download", lambda _: self._deny("g4_download_denied"))

    def _deny(self, reason):
        self._violation = reason

    async def _websocket(self, _socket):
        self._deny("g4_websocket_denied")
        # Never call connect_to_server: no handshake.

    async def _route(self, route):
        try:
            self.ensure()
            request = route.request
            lease = self._human_active()
            check_request(request.url, request.method, self.domain, lease)
            if request.is_navigation_request() and (
                    urlsplit(request.url).netloc != urlsplit(self.domain).netloc or
                    request.frame != self.page.main_frame):
                raise ExternalPolicyDenied("g4_navigation_denied")
            if request.resource_type in ("websocket", "eventsource"):
                raise ExternalPolicyDenied("g4_background_request_denied")
            # Prevent all session cookies, authorization and referrers from
            # reaching explicitly allowed static-asset hosts.
            headers = await request.all_headers()
            if urlsplit(request.url).netloc != urlsplit(self.domain).netloc:
                headers = {k: v for k, v in headers.items()
                           if k.lower() not in ("cookie", "authorization", "referer",
                                               "proxy-authorization")}
            # fetch(max_redirects=0) MUST NOT follow an unapproved redirect.
            self.ensure()
            response = await route.fetch(max_redirects=0, headers=headers, timeout=10000)
            self.ensure()
            if 300 <= response.status < 400:
                raise ExternalPolicyDenied("g4_redirect_denied")
            await route.fulfill(response=response)
        except Exception:
            self._deny("g4_request_denied")
            try:
                await route.abort()
            except Exception:
                pass

    async def navigate_for_human(self, url):
        self.ensure()
        if not self._human_active():
            raise ExternalPolicyDenied("g4_login_takeover_required")
        check_request(url, "GET", self.domain, True)
        if urlsplit(url).netloc != urlsplit(self.domain).netloc:
            raise ExternalPolicyDenied("g4_navigation_denied")
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
            self.ensure()
        except Exception:
            raise ExternalPolicyDenied("g4_navigation_denied") from None

    async def observe(self):
        """Read-only, after takeover. No value attributes or private screenshots."""
        async with self._in_action:
            self.ensure(agent=True)
            if not self.page or self.page.is_closed():
                raise ExternalPolicyDenied("g4_page_closed")
            cdp = None
            try:
                cdp = await self.context.new_cdp_session(self.page)
                tree = await cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
                if inspect_cdp_dom(tree.get("root")):
                    raise ExternalPolicyDenied("g4_sensitive_dom_denied")
                self.ensure(agent=True)
                # This text is untrusted and scrubbed, never a credential value.
                text = await self.page.locator("body").inner_text(timeout=2000)
                self.ensure(agent=True)
                return {"origin": self.domain, "text": redact_text(text[:12000]),
                        "trust_level": "untrusted_page_data"}
            finally:
                if cdp:
                    await cdp.detach()

    async def click(self, *_):
        self.ensure(agent=True)
        raise ExternalPolicyDenied("g4_g3_approval_required")

    async def type(self, *_):
        self.ensure(agent=True)
        raise ExternalPolicyDenied("g4_sensitive_input_denied")

    async def screenshot(self, *_):
        self.ensure(agent=True)
        raise ExternalPolicyDenied("g4_screenshot_denied")

    async def clipboard(self, *_):
        self.ensure(agent=True)
        raise ExternalPolicyDenied("g4_clipboard_denied")

    async def submit(self, *_):
        self.ensure(agent=True)
        raise ExternalPolicyDenied("g4_g3_approval_required")

    async def _sweep(self):
        while not self._closed:
            await asyncio.sleep(0.25)
            try:
                self.ensure()
            except Exception:
                await self.owner.revoke(self.id)
                return

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self._watch and self._watch is not asyncio.current_task():
            self._watch.cancel()
        try:
            if self.context:
                try:
                    for page in self.context.pages:
                        await page.evaluate("""async () => {
                            localStorage.clear(); sessionStorage.clear();
                            for(const k of await caches.keys()) await caches.delete(k);
                            if (indexedDB.databases) for(const db of await indexedDB.databases())
                              if(db.name) indexedDB.deleteDatabase(db.name);
                        }""")
                except Exception:
                    pass
                try:
                    await self.context.clear_cookies()
                finally:
                    await self.context.close()
        finally:
            pw = self.owner._drivers.pop(self.id, None)
            if pw:
                try:
                    await pw.stop()
                except Exception:
                    pass
            shutil.rmtree(self.directory, ignore_errors=True)


class AuthSessionManager:
    """Single-process registry; no persistent session IDs/cookies survive restart."""

    def __init__(self, settings):
        self.settings = settings
        self.root = _private_root(settings)
        self.sessions = {}
        self._drivers = {}
        # Orphaned browser profiles are never reopened after a crash.
        for p in self.root.glob("session-*"):
            if p.is_symlink():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)

    async def open(self, operator, task, domain):
        if not enabled():
            raise ExternalPolicyDenied("g4_disabled_or_domain_denied")
        domain = _origin(domain)
        if domain not in origins():
            raise ExternalPolicyDenied("g4_disabled_or_domain_denied")
        if not isinstance(operator, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", operator):
            raise ExternalPolicyDenied("g4_operator_denied")
        if not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", task):
            raise ExternalPolicyDenied("g4_task_denied")
        if kill_active(self.settings):
            raise ExternalPolicyDenied("g4_killed")
        if any(not s._closed and s.operator == operator and s.task == task and
               s.domain == domain for s in self.sessions.values()):
            raise ExternalPolicyDenied("g4_duplicate_session")
        folder = Path(tempfile.mkdtemp(prefix="session-", dir=self.root))
        session = AuthSession(self, operator, task, domain, folder)
        self.sessions[session.id] = session
        try:
            await session.start()
        except BaseException:
            await self.revoke(session.id)
            raise
        return session

    async def revoke(self, session_id):
        session = self.sessions.pop(session_id, None)
        if session is None:
            return False
        await session.close()
        return True

    async def revoke_all(self):
        for session_id in list(self.sessions):
            await self.revoke(session_id)

    async def logout(self, session_id):
        return await self.revoke(session_id)

    async def list_public(self):
        for session_id, session in list(self.sessions.items()):
            try:
                session.ensure()
            except Exception:
                await self.revoke(session_id)
        return [{
            "id": s.id, "task_id": s.task, "domain": s.domain,
            "operator_fingerprint": hashlib.sha256(s.operator.encode()).hexdigest()[:12],
            "expires_at": s.expires_at,
            "takeover_active": bool(s._lease()),
        } for s in self.sessions.values()]


_MANAGERS = {}


def manager_for(settings):
    key = str(Path(settings.codex_memory_root).resolve())
    if key not in _MANAGERS:
        _MANAGERS[key] = AuthSessionManager(settings)
    return _MANAGERS[key]
