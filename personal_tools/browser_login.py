"""Server-only narrow G4 login operation. Secrets never enter model arguments.

No screenshot, DOM content, field-value readback or password-bearing error is
ever persisted. The only automatic POST is the one verified form action.
"""
from __future__ import annotations
import asyncio
import hashlib
import re
import secrets
from urllib.parse import urlsplit

from browser_login_credentials import enabled, canonical_domain, read_for_executor
from browser_login_audit import reserve, finish, LoginQuotaDenied
from workflow_browser_external_identity import ExternalPolicyDenied
from personal_tools.base import ToolResult


def _same_origin(url, domain):
    try:
        u = urlsplit(url)
        return (u.scheme == urlsplit(domain).scheme
                and u.netloc == urlsplit(domain).netloc
                and u.username is None and u.password is None
                and not u.fragment)
    except (TypeError, ValueError):
        return False


async def _challenge(page):
    """Metadata only; do not read input values or page text."""
    if await page.locator(
        'input[autocomplete="one-time-code"], input[name*="otp" i],'
        'input[name*="verification" i], input[name*="2fa" i],'
        'iframe[src*="captcha" i], [data-sitekey],'
        'input[autocomplete="webauthn"], button[aria-label*="passkey" i]'
    ).count():
        return True
    return bool(re.search(r"/(?:mfa|2fa|otp|captcha|passkey|verify)(?:/|$|[?])",
                          urlsplit(page.url).path, re.I))


async def _static_form(page, domain):
    if not _same_origin(page.url, domain):
        raise ExternalPolicyDenied("login_origin_mismatch")
    # No iframe insertion, even for ostensibly trusted frames.
    if len(page.frames) != 1 or await page.locator("iframe,frame,object,embed").count():
        raise ExternalPolicyDenied("login_iframe_denied")
    forms = page.locator("form")
    if await forms.count() != 1:
        raise ExternalPolicyDenied("login_form_ambiguous")
    form = forms.first
    # Constant JS returns only attributes, never a control's value.
    meta = await form.evaluate("""f => ({
        method:f.method.toUpperCase(), action:f.action,
        override:!!f.querySelector('[formaction],[formmethod],[formenctype],input[type=file]'),
        username:[...f.querySelectorAll('input')].filter(i=>{
          const t=(i.getAttribute('type')||'text').toLowerCase();
          const a=(i.getAttribute('autocomplete')||'').toLowerCase();
          const n=(i.getAttribute('name')||'').toLowerCase();
          return t==='email'||a==='username'||n==='username'||
                 n==='email'||t==='text';
        }).length,
        passwords:f.querySelectorAll('input[type=password]').length
    })""")
    if (meta["method"] != "POST" or meta["override"] or
            meta["username"] != 1 or meta["passwords"] != 1 or
            not _same_origin(meta["action"], domain)):
        raise ExternalPolicyDenied("login_form_denied")
    user = form.locator(
        'input[type=email],input[autocomplete=username],'
        'input[name=username],input[name=email],input[type=text]'
    )
    password = form.locator('input[type=password]')
    buttons = form.locator("button:not([type=button]):not([type=reset]),input[type=submit]")
    if (await user.count() != 1 or await password.count() != 1 or
            await buttons.count() != 1 or
            not await user.is_visible() or not await password.is_visible()):
        raise ExternalPolicyDenied("login_controls_denied")
    return form, user, password, buttons, meta["action"]


async def _login(session, record):
    domain = record["domain"]
    page = session.page
    if not _same_origin(record["login_url"], domain):
        return "failed", "origin_mismatch"
    response = await page.goto(record["login_url"], wait_until="domcontentloaded",
                               timeout=12000)
    if response is None or not _same_origin(page.url, domain):
        return "failed", "origin_mismatch"
    if await _challenge(page):
        return "needs_human", "authentication_challenge"
    _, user, password, submit, action = await _static_form(page, domain)
    if await _challenge(page):
        return "needs_human", "authentication_challenge"
    # Never return or log either filled value. No screenshots at any point.
    await user.fill(record["username"], timeout=1500)
    await password.fill(record["password"], timeout=1500)
    if not _same_origin(page.url, domain) or len(page.frames) != 1:
        return "failed", "origin_changed"
    # The next verified top-level, exact-action POST alone can cross G4's
    # submission barrier. The route consumes this token BEFORE fetch.
    session._autofill_target = action
    try:
        await submit.click(timeout=4000)
        await page.wait_for_load_state("domcontentloaded", timeout=6000)
    finally:
        session._autofill_target = None
    if not _same_origin(page.url, domain):
        return "failed", "origin_changed"
    if await _challenge(page):
        return "needs_human", "authentication_challenge"
    # A cookie is necessary evidence, never return its actual value.
    cookies = await session.context.cookies([domain])
    if cookies and await page.locator('input[type=password]').count() == 0:
        return "logged_in", None
    return "failed", "login_not_confirmed"


async def login(settings, domain, operator_id):
    """Result is a fixed safe status/reason and never the credential."""
    if not enabled():
        return "failed", "disabled"
    try:
        origin = canonical_domain(domain)
    except ExternalPolicyDenied:
        return "failed", "domain_denied"
    record = read_for_executor(settings, origin)
    if record is None:
        return "failed", "credential_missing"
    try:
        attempt = reserve(settings, origin, record["username"])
    except LoginQuotaDenied as exc:
        return "failed", exc.reason
    except Exception:
        return "failed", "audit_unavailable"
    session = None
    result, reason = "failed", "login_unavailable"
    try:
        operator = "login_" + hashlib.sha256(operator_id.encode()).hexdigest()[:24]
        task = "login_" + secrets.token_hex(12)
        from browser_auth_sessions import manager_for  # lazy: avoid G4/personal_tools import cycle
        session = await manager_for(settings).open(operator, task, origin, automated=True)
        result, reason = await asyncio.wait_for(_login(session, record), timeout=25)
    except ExternalPolicyDenied:
        result, reason = "failed", "policy_denied"
    except Exception:
        result, reason = "failed", "login_unavailable"
    finally:
        if session is not None and result not in ("logged_in", "needs_human"):
            await manager_for(settings).revoke(session.id, operator=session.operator)
        # Only fixed status metadata is recorded.
        finish(settings, attempt, result)
    if result == "needs_human":
        # Preserve the SAME Chromium context and task at the challenge screen.
        # Drop the automatic one-POST capability before issuing the human lease;
        # the G4 takeover guards still deny every agent click/type/readback.
        session._autofill_target = None
        session._automated_login = False
        try:
            from web_takeover import WebTakeover
            await asyncio.to_thread(WebTakeover(settings).start, {
                "reason": "login", "task_id": session.task, "ttl_seconds": 300
            })
        except Exception:
            await manager_for(settings).revoke(session.id, operator=session.operator)
            return "failed", "takeover_unavailable"
    return result, reason


async def login_adapter(settings, arg, *, operator_id, **_kwargs):
    """Only domain is accepted from model, never credentials."""
    if not isinstance(arg, str) or len(arg) > 280:
        return ToolResult(False, "failed (invalid_domain)")
    result, reason = await login(settings, arg, operator_id)
    return ToolResult(result == "logged_in",
                      result if result == "logged_in" else f"{result} ({reason})")
