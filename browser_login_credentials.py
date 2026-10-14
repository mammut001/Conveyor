"""Browser login entries backed by the existing 0600 console Secrets store.

Public code can enumerate domain/user/masked only.  The only password-read
call site is the non-model browser-login executor.
"""
from __future__ import annotations
import hashlib
import json
import re
from urllib.parse import urlsplit

from browser_auth_policy import _origin, origins
from console_secrets import (
    SecretStoreError, _LOCK, delete_secret, list_secrets, read_secret, set_secret,
)
from workflow_browser_external_identity import ExternalPolicyDenied

PREFIX = "BROWSER_LOGIN_"


def enabled():
    import os
    from browser_auth_policy import enabled as g4_enabled
    return g4_enabled() and os.getenv("CONVEYOR_BROWSER_AUTOFILL_LOGIN") == "1"


def canonical_domain(domain):
    if not isinstance(domain, str):
        raise ExternalPolicyDenied("login_domain_denied")
    origin = _origin(domain)
    if origin not in origins():
        raise ExternalPolicyDenied("login_domain_not_allowlisted")
    if urlsplit(origin).scheme != "https":
        # HTTP is only an ephemeral, local, required-Chromium fixture.
        from browser_auth_policy import fixture_loopback_enabled
        if not fixture_loopback_enabled():
            raise ExternalPolicyDenied("login_https_required")
    return origin


def _name(origin):
    return PREFIX + hashlib.sha256(origin.encode("ascii")).hexdigest()[:48].upper()


def _metadata(value):
    return {"domain": value["domain"], "username": value["username"],
            "masked": True}


def _validate(domain, username, password, login_url):
    origin = canonical_domain(domain)
    if (not isinstance(username, str) or not 0 < len(username) <= 256
            or any(c in username for c in "\x00\r\n")):
        raise ExternalPolicyDenied("login_username_denied")
    if (not isinstance(password, str) or not 0 < len(password) <= 4096
            or "\x00" in password):
        raise ExternalPolicyDenied("login_password_denied")
    if login_url is not None:
        if (not isinstance(login_url, str) or not login_url.startswith(origin + "/")
                or len(login_url) > 2048 or
                urlsplit(login_url).netloc != urlsplit(origin).netloc or
                urlsplit(login_url).scheme != urlsplit(origin).scheme or
                urlsplit(login_url).fragment or urlsplit(login_url).username or
                urlsplit(login_url).password or "\\" in login_url):
            raise ExternalPolicyDenied("login_url_denied")
    return {"domain": origin, "username": username,
            "password": password, "login_url": login_url or origin + "/login"}


def put(settings, domain, username, password, login_url=None):
    if not enabled():
        raise ExternalPolicyDenied("login_disabled")
    value = _validate(domain, username, password, login_url)
    with _LOCK:
        set_secret(settings, _name(value["domain"]), json.dumps(value, separators=(",", ":")))
    return _metadata(value)


def read_for_executor(settings, domain):
    """Private. Never call from a model, Web response, log or journal."""
    if not enabled():
        raise ExternalPolicyDenied("login_disabled")
    origin = canonical_domain(domain)
    with _LOCK:
        raw = read_secret(settings, _name(origin))
    if raw is None:
        return None
    try:
        record = json.loads(raw)
        if record.get("domain") != origin:
            return None
        return _validate(**record)
    except (ValueError, TypeError, KeyError, ExternalPolicyDenied):
        return None


def public_list(settings):
    if not enabled():
        raise ExternalPolicyDenied("login_disabled")
    out = []
    with _LOCK:
        for item in list_secrets(settings):
            if not item["name"].startswith(PREFIX) or not item["set"]:
                continue
            raw = read_secret(settings, item["name"])
            try:
                record = json.loads(raw)
                if (record["domain"] in origins() and
                        _name(record["domain"]) == item["name"]):
                    out.append(_metadata(record))
            except (ValueError, KeyError, TypeError):
                continue
    return sorted(out, key=lambda x: x["domain"])


def remove(settings, domain):
    if not enabled():
        raise ExternalPolicyDenied("login_disabled")
    origin = canonical_domain(domain)
    with _LOCK:
        return delete_secret(settings, _name(origin))
