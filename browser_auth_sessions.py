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

from browser_auth_timeout import wait_for_step
from browser_auth_policy import (
    enabled, ttl, origins, _origin, check_request, browser_pins,
    fixture_loopback_enabled,
)
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
        self._closing = None
        self._owner_lock = None
        self._violation = None
        self._in_action = asyncio.Lock()
        self._automated_login = False
        self._autofill_target = None
        # Positive evidence: the approved login POST received a response.
        self._autofill_post_completed = False

    def _lease(self):
        return HumanTakeoverStore(self.owner.settings).current()

    def _human_active(self):
        lease = self._lease()
        if not (lease and lease["state"] == "human_active" and
                lease.get("reason") == "login" and lease.get("task_id") == self.task):
            return False
        if fixture_loopback_enabled():
            return True  # required Chromium fixture only
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
        # Only G4's explicit real-browser loopback test harness is exempt
        # from G5's HTTPS-only certification namespace.
        if not (fixture_loopback_enabled() and self.domain.startswith("http://127.0.0.1:")):
            from browser_g5_rollout import check as g5_check
            g5_check(Path(self.owner.settings.codex_memory_root) / "browser_external",
                     self.domain, "auth")
        if (agent or self._automated_login) and self._lease() is not None:
            raise ExternalPolicyDenied("g4_human_takeover_active")

    async def start(self):
        self.ensure()
        if not self._automated_login and not self._human_active():
            raise ExternalPolicyDenied("g4_login_takeover_required")
        # No existing browser state is imported. Chromium sees only this private
        # HOME; OS keychain, clipboard APIs and password-store integrations off.
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        self.owner._drivers[self.id] = pw
        # On a protected desktop keep the login browser visible for OTP
        # handoff; only the isolated CI fixture may complete it headlessly.
        headless = fixture_loopback_enabled() or (self._automated_login and not os.getenv("DISPLAY"))
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
            java_script_enabled=not self._automated_login,
            timeout=10000,
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
        await wait_for_step(self.context.route("**/*", self._route), 5, "context.route")
        await wait_for_step(self.context.route_web_socket("**/*", self._websocket),
                            5, "context.route_web_socket")
        await wait_for_step(self.context.add_init_script(EXTRA_INIT_SCRIPT + """
          Object.defineProperty(navigator,'clipboard',{configurable:false,
              get(){throw Error('clipboard_denied')}});
        """), 5, "context.add_init_script")
        self.context.on("page", self._new_page)
        self.page = (self.context.pages[0] if self.context.pages else
                     await wait_for_step(self.context.new_page(), 5, "context.new_page"))
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
            login_post = (
                self._automated_login and
                request.method == "POST" and
                request.url == self._autofill_target and
                request.is_navigation_request() and
                request.frame == self.page.main_frame and not lease
            )
            if login_post:
                # One exact POST; no other agent-origin submit is authorized.
                self._autofill_target = None
            else:
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
            if login_post and response.status < 500:
                self._autofill_post_completed = True
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
        try:
            while not self._closed:
                await asyncio.sleep(0.25)
                try:
                    self.ensure()
                except Exception:
                    await self.owner.revoke(self.id, operator=self.operator)
                    return
        except asyncio.CancelledError:
            return

    async def _close_impl(self):
        from browser_auth_close import bounded_close
        await bounded_close(self)

    async def close(self):
        # Exactly one shutdown Task; caller cancellation must not stop teardown.
        if self._closing is None:
            self._closed = True
            self._closing = asyncio.ensure_future(self._close_impl())
            task = self._closing
            self.owner._closing[self.id] = (self.operator, task)
            self.owner._closing_domains[self.id] = self.domain
            def forget(done):
                if self.owner._closing.get(self.id, (None, None))[1] is done:
                    self.owner._closing.pop(self.id, None)
                    self.owner._closing_domains.pop(self.id, None)
            task.add_done_callback(forget)
        return await asyncio.shield(self._closing)


class AuthSessionManager:
    """Single-process registry; no persistent session IDs/cookies survive restart."""

    def __init__(self, settings):
        self.settings = settings
        self.root = _private_root(settings)
        self.sessions = {}
        self._drivers = {}
        self._closing = {}
        self._closing_domains = {}
        # Orphaned browser profiles are never reopened after a crash.
        from browser_auth_orphans import cleanup
        cleanup(self.root)

    async def open(self, operator, task, domain, *, automated=False):
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
        if automated:
            from browser_login_credentials import enabled as login_enabled
            if not login_enabled() or HumanTakeoverStore(self.settings).current():
                raise ExternalPolicyDenied("login_automation_denied")
        if any(not s._closed and s.operator == operator and s.task == task and
               s.domain == domain for s in self.sessions.values()):
            raise ExternalPolicyDenied("g4_duplicate_session")
        from contextlib import nullcontext
        from browser_login_domain_gate import reserve_login_domain
        with reserve_login_domain(self, domain) if automated else nullcontext():
            folder = Path(tempfile.mkdtemp(prefix="session-", dir=self.root))
            from browser_auth_owner import acquire
            try:
                ownership = acquire(folder)
            except BaseException:
                shutil.rmtree(folder, ignore_errors=True)
                raise
            session = AuthSession(self, operator, task, domain, folder)
            session._owner_lock = ownership
            session._automated_login = automated
            self.sessions[session.id] = session
            try:
                await session.start()
            except BaseException:
                await self.revoke(session.id, operator=operator)
                raise
            return session

    async def revoke(self, session_id, *, operator):
        session = self.sessions.get(session_id)
        if session is None:
            closing = self._closing.get(session_id)
            if closing is None or closing[0] != operator:
                return False
            await asyncio.shield(closing[1])
            return True
        if session.operator != operator:
            return False
        self.sessions.pop(session_id)
        await session.close()
        return True

    async def revoke_all(self):
        # Include closes whose sessions were already popped by their sweep task.
        await asyncio.gather(*(self.revoke(s.id, operator=s.operator)
                               for s in list(self.sessions.values())))
        await asyncio.gather(*(asyncio.shield(task)
                               for _, task in list(self._closing.values())))

    async def logout(self, session_id, *, operator):
        return await self.revoke(session_id, operator=operator)

    async def list_public(self, *, operator):
        for session_id, session in list(self.sessions.items()):
            try:
                session.ensure()
            except Exception:
                await self.revoke(session_id, operator=session.operator)
        return [{
            "id": s.id, "task_id": s.task, "domain": s.domain,
            "operator_fingerprint": hashlib.sha256(s.operator.encode()).hexdigest()[:12],
            "expires_at": s.expires_at,
            "takeover_active": bool(s._lease()),
        } for s in self.sessions.values() if s.operator == operator]


_MANAGERS = {}


def manager_for(settings):
    key = str(Path(settings.codex_memory_root).resolve())
    if key not in _MANAGERS:
        _MANAGERS[key] = AuthSessionManager(settings)
    return _MANAGERS[key]
