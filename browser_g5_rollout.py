"""G5 manual release gate: signed domain manifest, deny-first revoke, no auto promotion.

G5 cannot supply missing OS egress isolation or independent human review. The
separate LIVE_ACK is deliberate: enabling monitoring/CI alone grants nothing.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from workflow_browser_external_identity import ExternalPolicyDenied, canonical_url
from workflow_browser_egress import private_database_path

_HEX = re.compile(r"^[0-9a-f]{64}$")
_ACTIONS = frozenset({"read", "interact", "auth"})
_EVIDENCE = frozenset({"g1", "g2", "g3", "g4", "os_capture", "dns",
                       "injection", "dom_race", "session_bleed", "takeover_chaos",
                       "egress_alerts", "rollback", "redaction"})
_DB = "g5-rollout.sqlite3"


def enabled():
    return os.getenv("CONVEYOR_BROWSER_G5") == "1"


def live_requested():
    # Flag-only CI cannot activate live-site promotion.
    return enabled() and os.getenv("CONVEYOR_BROWSER_G5_LIVE_ACK") == "1"


def _origin(value):
    host, port, path = canonical_url(value)
    if path != "/" or not value.endswith("/") and value != (
            f"https://{host}" + (f":{port}" if port != 443 else "")):
        raise ExternalPolicyDenied("g5_origin_denied")
    return f"https://{host}" + (f":{port}" if port != 443 else "")


def _db(root):
    root = Path(root)
    path = private_database_path(root / _DB)
    try:
        with sqlite3.connect(path, timeout=2) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS revoked (
                origin TEXT PRIMARY KEY, revoked_at REAL NOT NULL
            )""")
    except sqlite3.Error as exc:
        raise ExternalPolicyDenied("g5_state_unavailable") from exc
    return path


def revoke(root, origin):
    """Sticky domain kill. Clearing it requires out-of-band security review."""
    origin = _origin(origin)
    try:
        with sqlite3.connect(_db(root), timeout=2) as conn:
            conn.execute("INSERT OR REPLACE INTO revoked VALUES (?,?)",
                         (origin, time.time()))
    except (OSError, sqlite3.Error) as exc:
        raise ExternalPolicyDenied("g5_state_unavailable") from exc
    return origin


def revoked(root, origin):
    try:
        with sqlite3.connect(_db(root), timeout=2) as conn:
            return conn.execute("SELECT 1 FROM revoked WHERE origin=?",
                                (origin,)).fetchone() is not None
    except (OSError, sqlite3.Error) as exc:
        raise ExternalPolicyDenied("g5_state_unavailable") from exc


def _manifest():
    path_str = os.getenv("CONVEYOR_BROWSER_G5_MANIFEST", "")
    if not path_str:
        raise ExternalPolicyDenied("g5_manifest_missing")
    path = Path(path_str)
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 16384):
            raise ExternalPolicyDenied("g5_manifest_unsafe")
        raw = path.read_bytes()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (OSError, ValueError, TypeError) as exc:
        raise ExternalPolicyDenied("g5_manifest_unavailable") from exc


def _chromium_revision():
    """Browser revision pinned by installed Playwright distribution, offline."""
    try:
        from importlib.resources import files
        info = json.loads(files("playwright").joinpath(
            "driver/package/browsers.json").read_text(encoding="utf-8"))
        for row in info["browsers"]:
            if row.get("name") == "chromium":
                return str(row["revision"])
    except (OSError, ValueError, KeyError, ImportError, TypeError):
        pass
    raise ExternalPolicyDenied("g5_chromium_revision_unavailable")


def _certified(origin, action):
    doc = _manifest()
    signature = doc.pop("signature", None)
    key = os.getenv("CONVEYOR_BROWSER_G5_REVIEW_KEY", "")
    if not isinstance(signature, str) or not _HEX.fullmatch(signature):
        raise ExternalPolicyDenied("g5_review_missing")
    if len(key) < 32:
        raise ExternalPolicyDenied("g5_review_key_missing")
    data = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    expected = hmac.new(key.encode(), data, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ExternalPolicyDenied("g5_review_signature_denied")
    if set(doc) != {"version", "origin", "actions", "reviewer",
                    "expires_at", "builds", "evidence", "retention_days"}:
        raise ExternalPolicyDenied("g5_manifest_schema_denied")
    if doc["version"] != 1 or doc["origin"] != origin:
        raise ExternalPolicyDenied("g5_domain_not_certified")
    if (not isinstance(doc["actions"], list) or not doc["actions"]
            or set(doc["actions"]) - _ACTIONS or action not in doc["actions"]):
        raise ExternalPolicyDenied("g5_action_not_certified")
    if (not isinstance(doc["reviewer"], str) or not 4 <= len(doc["reviewer"]) <= 100
            or doc["reviewer"] in ("web-console", "operator", "ci", "test")):
        raise ExternalPolicyDenied("g5_independent_review_missing")
    expiry = doc["expires_at"]
    if type(expiry) not in (float, int) or not time.time() < expiry <= time.time() + 30 * 86400:
        raise ExternalPolicyDenied("g5_review_expired")
    if type(doc["retention_days"]) is not int or not 1 <= doc["retention_days"] <= 30:
        raise ExternalPolicyDenied("g5_retention_denied")
    builds = doc["builds"]
    evidence = doc["evidence"]
    if (not isinstance(builds, dict) or set(builds) != {"playwright", "chromium"}
            or any(not isinstance(v, str) or not v for v in builds.values())
            or not isinstance(evidence, dict) or set(evidence) != _EVIDENCE
            or any(not isinstance(v, str) or not _HEX.fullmatch(v) for v in evidence.values())):
        raise ExternalPolicyDenied("g5_evidence_incomplete")
    # Metadata from the signed reviewer is checked, not merely stored.
    try:
        from importlib.metadata import version
        if builds["playwright"] != version("playwright"):
            raise ExternalPolicyDenied("g5_playwright_version_changed")
        if builds["chromium"] != _chromium_revision():
            raise ExternalPolicyDenied("g5_chromium_version_changed")
    except ImportError as exc:
        raise ExternalPolicyDenied("g5_build_unavailable") from exc
    # Actual Chromium version / OS capture must be attested externally. This
    # gate does NOT attempt a privileged process or an unsafe unbounded launch.
    return True


def check(root, origin, action):
    """Checked before EVERY request/action and at session creation."""
    if not enabled():
        return
    origin = _origin(origin)
    if revoked(root, origin):
        raise ExternalPolicyDenied("g5_domain_revoked")
    if live_requested():
        _certified(origin, action)


def status(root):
    if not enabled():
        raise ExternalPolicyDenied("g5_disabled")
    try:
        with sqlite3.connect(_db(root), timeout=2) as conn:
            entries = conn.execute(
                "SELECT origin, revoked_at FROM revoked ORDER BY revoked_at DESC LIMIT 100"
            ).fetchall()
        return {"enabled": True, "live_requested": live_requested(),
                "auto_promotion": False, "revoked": [
                    {"origin": name, "at": ts} for name, ts in entries]}
    except (OSError, sqlite3.Error) as exc:
        raise ExternalPolicyDenied("g5_state_unavailable") from exc
