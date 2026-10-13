"""Durable, one-shot human-controlled outbound drafts (default off).

Only the web API or a human confirmation callback can claim a draft.  A claim
is committed before the network call: failures/timeouts are NEVER retried.
"""
from __future__ import annotations

import asyncio
import email.utils
from email.message import EmailMessage
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import smtplib
import sqlite3
import time
import unicodedata
from typing import Any

from redaction import redact_text

ADDRESS = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_BODY = 20000
MAX_JSON = 64000


def enabled(settings: Any) -> bool:
    return bool(getattr(settings, "draft_cards_enabled", False))


def expiry_seconds(settings: Any) -> int:
    return max(300, min(7 * 86400, int(getattr(settings, "draft_cards_ttl_seconds", 86400))))


def _path(settings: Any) -> Path:
    return Path(settings.codex_memory_root) / "outbound_drafts.db"


def _connection(settings: Any) -> sqlite3.Connection:
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
    if path.exists():
        os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    # No persistent WAL with plaintext outbound content.
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("""CREATE TABLE IF NOT EXISTS outbound_drafts (
        token TEXT PRIMARY KEY, tool TEXT NOT NULL, draft TEXT NOT NULL,
        original_hash TEXT NOT NULL, sent_hash TEXT, status TEXT NOT NULL,
        created REAL NOT NULL, expires REAL NOT NULL, result TEXT
    )""")
    return db


def is_outbound(settings: Any, tool: str) -> bool:
    if tool in ("email.send", "github.comment", "github.create_issue"):
        return True
    if tool.startswith("mcp."):
        try:
            from mcp_client import get_mcp_tool_spec
            spec = get_mcp_tool_spec(settings, tool)
            tags = spec.annotations.get("tags", []) if spec else []
            configured = getattr(settings, "mcp_outbound_tools", ()) or ()
            return bool(spec and spec.danger.value != "read" and (
                spec.annotations.get("outbound") is True or
                (isinstance(tags, list) and "outbound" in tags) or
                tool.replace(".", "__", 2) in configured
            ))
        except Exception:
            return False
    # Telegram/Feishu transport replies are NOT registered agent tools; they
    # continue to serve confirmations, not as an outbound-send bypass.
    return False


def _recipients(raw: str, *, required: bool = False) -> list[str]:
    if not isinstance(raw, str) or len(raw) > 2000 or "\r" in raw or "\n" in raw:
        raise ValueError("Invalid recipient header")
    if not raw.strip():
        if required:
            raise ValueError("To is required")
        return []
    values = [s.strip() for s in raw.split(",")]
    if len(values) > 50 or any(not ADDRESS.fullmatch(v) for v in values):
        raise ValueError("Invalid email address (use comma-separated addresses)")
    return values


def _allowlist(settings: Any, recipients: list[str]) -> None:
    raw = str(getattr(settings, "outbound_allowlist", "") or "").strip()
    if not raw:
        return
    rules = [r.strip().lower().lstrip("@") for r in raw.split(",") if r.strip()]
    if not rules or not recipients:
        raise ValueError("Outbound allowlist requires known recipients")
    for address in recipients:
        local, domain = address.lower().rsplit("@", 1)
        if not any(("@" in r and address.lower() == r) or
                   ("@" not in r and domain == r) for r in rules):
            raise ValueError("Recipient not in CONVEYOR_OUTBOUND_ALLOWLIST")


def _repo_allowed(settings: Any, repo: str) -> bool:
    """Only an exact owner/repo allowlist entry authorizes a GitHub destination."""
    raw = str(getattr(settings, "outbound_allowlist", "") or "").strip()
    return not raw or repo.lower() in (entry.strip().lower() for entry in raw.split(","))


def _repo_editable(settings: Any, proposed: str) -> bool:
    """Allow repo editing only if there is an explicit alternative destination."""
    raw = str(getattr(settings, "outbound_allowlist", "") or "")
    return any(REPO.fullmatch(e.strip()) and ".." not in e and
               e.strip().lower() != proposed.lower() for e in raw.split(","))


def _json(payload: dict[str, str]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(payload: dict[str, str]) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _parse(settings: Any, tool: str, arg: str) -> dict[str, str]:
    if tool == "email.send":
        if arg.lstrip().startswith("{"):
            try:
                payload = json.loads(arg)
            except (ValueError, TypeError) as exc:
                raise ValueError("Invalid email JSON") from exc
        else:
            parts = arg.split("|", 2)
            if len(parts) != 3:
                raise ValueError("Email format: to | subject | body")
            payload = dict(to=parts[0].strip(), cc="", bcc="", subject=parts[1].strip(), body=parts[2].strip())
        if isinstance(payload, dict):
            payload = {**{"cc": "", "bcc": ""}, **payload}
        return validate(settings, tool, payload)
    if tool == "github.create_issue":
        parts = arg.split("|", 1)
        return validate(settings, tool, dict(
            repo=str(settings.github_default_repo), title=parts[0].strip(),
            body=parts[1].strip() if len(parts) > 1 else "",
        ))
    if tool == "github.comment":
        parts = arg.split("|", 1)
        if len(parts) != 2:
            raise ValueError("Comment format: number | body")
        return validate(settings, tool, dict(repo=str(settings.github_default_repo),
                                              number=parts[0].strip(), body=parts[1].strip()))
    if tool.startswith("mcp.") and is_outbound(settings, tool):
        try:
            obj = json.loads(arg)
        except ValueError as exc:
            raise ValueError("Outbound MCP input must be JSON") from exc
        if not isinstance(obj, dict):
            raise ValueError("Outbound MCP input must be a JSON object")
        return validate(settings, tool, {"payload": json.dumps(obj, ensure_ascii=False)})
    raise ValueError("Tool is not an outbound draft tool")


def validate(settings: Any, tool: str, payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict) or any(not isinstance(v, str) for v in payload.values()):
        raise ValueError("Draft fields must be strings")
    keys = {
        "email.send": {"to", "cc", "bcc", "subject", "body"},
        "github.comment": {"repo", "number", "body"},
        "github.create_issue": {"repo", "title", "body"},
    }.get(tool, {"payload"} if tool.startswith("mcp.") and is_outbound(settings, tool) else set())
    if set(payload) != keys:
        raise ValueError("Draft fields changed; reload")
    if any(len(v) > MAX_JSON or "\x00" in v for v in payload.values()):
        raise ValueError("Draft field too large or contains NUL")
    if redact_text(_json(payload)) != _json(payload):
        raise ValueError("Secret detected in draft")
    if tool == "email.send":
        to = _recipients(payload["to"], required=True)
        cc = _recipients(payload["cc"])
        bcc = _recipients(payload["bcc"])
        subject = payload["subject"]
        if (not subject.strip() or len(subject) > 200 or
                any(c in subject for c in "\r\n") or
                any(ord(c) < 32 for c in subject)):
            raise ValueError("Invalid subject header")
        if len(payload["body"]) > MAX_BODY:
            raise ValueError("Email body exceeds 20000 characters")
        _allowlist(settings, to + cc + bcc)
    elif tool in ("github.comment", "github.create_issue"):
        if not REPO.fullmatch(payload["repo"]) or ".." in payload["repo"]:
            raise ValueError("Invalid repo")
        if not _repo_allowed(settings, payload["repo"]):
            raise ValueError("GitHub repo not in CONVEYOR_OUTBOUND_ALLOWLIST")
        if len(payload["body"]) > MAX_BODY:
            raise ValueError("GitHub body exceeds 20000 characters")
        if tool == "github.comment":
            if not payload["number"].isdigit() or int(payload["number"]) <= 0:
                raise ValueError("Invalid issue/PR number")
            if not payload["body"].strip():
                raise ValueError("Comment body cannot be empty")
        else:
            title = payload["title"]
            if (not title.strip() or len(title) > 200 or
                    any(unicodedata.category(char) == "Cc" for char in title)):
                raise ValueError("Issue title must be 1-200 chars without control characters")
    elif tool.startswith("mcp."):
        try:
            obj = json.loads(payload["payload"])
        except ValueError as exc:
            raise ValueError("Invalid MCP JSON") from exc
        if not isinstance(obj, dict) or len(payload["payload"]) > MAX_JSON:
            raise ValueError("Invalid MCP JSON object")
        # A defined allowlist must fail closed for unknown outbound destinations.
        addresses: list[str] = []
        for k in ("to", "cc", "bcc"):
            if k in obj:
                addresses.extend(_recipients(obj[k], required=k == "to"))
        if getattr(settings, "outbound_allowlist", ""):
            _allowlist(settings, addresses)
    else:
        raise ValueError("Unsupported outbound tool")
    return dict(payload)


def register(settings: Any, pending: Any) -> bool:
    if not (enabled(settings) and is_outbound(settings, pending.tool_name)):
        return False
    draft = _parse(settings, pending.tool_name, pending.arg)
    with _connection(settings) as conn:
        conn.execute("INSERT INTO outbound_drafts VALUES(?,?,?,?,?,?,?,?,?)",
                     (pending.token, pending.tool_name, _json(draft), fingerprint(draft),
                      None, "pending", pending.created_at, pending.expires_at, None))
    return True


def is_draft_token(settings: Any, token: str) -> bool:
    if not _path(settings).exists():
        return False
    with _connection(settings) as conn:
        return conn.execute("SELECT 1 FROM outbound_drafts WHERE token=?", (token,)).fetchone() is not None


def list_items(settings: Any) -> list[dict[str, Any]]:
    if not enabled(settings):
        return []
    with _connection(settings) as conn:
        conn.execute("UPDATE outbound_drafts SET status='expired', draft='{}' WHERE status='pending' AND expires<=?",
                     (time.time(),))
        rows = conn.execute("SELECT * FROM outbound_drafts WHERE status='pending' ORDER BY created DESC").fetchall()
        incidents = conn.execute(
            "SELECT * FROM outbound_drafts WHERE status IN ('sending','needs_reconciliation') ORDER BY created DESC LIMIT 30"
        ).fetchall()
    pending_items = [{
        "id": r["token"], "kind": "tool", "source": "chat",
        "tool_name": r["tool"], "summary": "Outbound draft · human Send required",
        "danger": "write", "draft": json.loads(r["draft"]), "draft_hash": r["original_hash"],
        "draft_card": True, "editable": True, "arg": "[outbound draft]",
        "created_at": r["created"], "expires_at": r["expires"],
        "save_only": False,
        "repo_editable": (r["tool"] in ("github.comment", "github.create_issue") and
                          _repo_editable(settings, json.loads(r["draft"])["repo"])),
    } for r in rows]
    pending_items.extend({
        "id": r["token"], "kind": "reconciliation", "source": "chat",
        "tool_name": r["tool"], "summary": "Outbound status uncertain — inspect destination; do not retry",
        "danger": "write", "editable": False, "draft": None,
        "arg": "Sending or outcome uncertain; no automatic retry",
        "created_at": r["created"], "expires_at": None,
    } for r in incidents)
    return pending_items


def _claim(settings: Any, token: str, expected_hash: str, edited: Any, decision: str) -> tuple[str, dict[str, str]] | None:
    conn = _connection(settings)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM outbound_drafts WHERE token=?", (token,)).fetchone()
        if row is None:
            raise KeyError("Draft not found")
        if row["status"] != "pending" or row["expires"] <= time.time():
            raise ValueError("Draft already decided or expired")
        if not isinstance(expected_hash, str) or not hmac.compare_digest(row["original_hash"], expected_hash):
            raise ValueError("Draft hash mismatch; reload")
        tool = row["tool"]
        if decision == "deny":
            conn.execute("UPDATE outbound_drafts SET status='denied', draft='{}' WHERE token=?", (token,))
            conn.execute("COMMIT")
            return None
        if decision != "send":
            raise ValueError("Save only is not supported for this transport")
        payload = validate(settings, tool, edited)
        if tool in ("github.comment", "github.create_issue"):
            original_repo = json.loads(row["draft"])["repo"]
            if (payload["repo"].lower() != original_repo.lower() and
                    not (getattr(settings, "outbound_allowlist", "") and
                         _repo_allowed(settings, payload["repo"]))):
                raise ValueError("GitHub repo is fixed to the original draft unless explicitly allowlisted")
        sent_hash = fingerprint(payload)
        conn.execute("UPDATE outbound_drafts SET status='sending', draft=?, sent_hash=? WHERE token=?",
                     (_json(payload), sent_hash, token))
        conn.execute("COMMIT")
        return tool, payload
    except Exception:
        conn.execute("ROLLBACK") if conn.in_transaction else None
        raise
    finally:
        conn.close()


def _finish(settings: Any, token: str, status: str, result: str) -> None:
    with _connection(settings) as conn:
        conn.execute("UPDATE outbound_drafts SET status=?, draft='{}', result=? WHERE token=? AND status='sending'",
                     (status, redact_text(result)[:1500], token))
    try:
        import approval_relay
        approval_relay.mark_local(settings, token, "done" if status == "sent" else "failed", result_preview=result)
    except Exception:
        pass


def _send_email(settings: Any, draft: dict[str, str]) -> str:
    from personal_tools.email_smtp import _check_config
    error = _check_config(settings)
    if error:
        raise ValueError(error)
    msg = EmailMessage()
    msg["From"] = settings.gmail_address
    msg["To"] = draft["to"]
    if draft["cc"]:
        msg["Cc"] = draft["cc"]
    msg["Subject"] = draft["subject"]
    msg["Date"] = email.utils.formatdate(localtime=False)
    msg["Message-ID"] = email.utils.make_msgid()
    msg.set_content(draft["body"])
    recipients = sum((_recipients(draft[k]) for k in ("to", "cc", "bcc")), [])
    with smtplib.SMTP(settings.gmail_smtp_host, settings.gmail_smtp_port, timeout=30) as client:
        client.ehlo()
        client.starttls()
        client.ehlo()
        client.login(settings.gmail_address, settings.gmail_app_password)
        client.send_message(msg, to_addrs=recipients)
    return "Email sent"


def _send_issue(settings: Any, draft: dict[str, str]) -> str:
    from personal_tools.github_tools import _check_config, _github_request
    err = _check_config(settings)
    if err:
        raise ValueError(err)
    owner, repo = draft["repo"].split("/")
    ok, data = _github_request(settings, "POST",
        f"/repos/{owner}/{repo}/issues",
        json_body={"title": draft["title"], "body": draft["body"]})
    if not ok:
        raise RuntimeError("GitHub issue outcome uncertain: " + redact_text(str(data))[:160])
    return "GitHub issue created"


def _send_comment(settings: Any, draft: dict[str, str]) -> str:
    from personal_tools.github_tools import _check_config, _github_request
    err = _check_config(settings)
    if err:
        raise ValueError(err)
    owner, repo = draft["repo"].split("/")
    ok, data = _github_request(settings, "POST",
        f"/repos/{owner}/{repo}/issues/{int(draft['number'])}/comments",
        json_body={"body": draft["body"]})
    if not ok:
        raise RuntimeError("GitHub comment outcome uncertain: " + redact_text(str(data))[:160])
    return "GitHub comment sent"


async def submit(settings: Any, token: str, *, expected_hash: str,
                 draft: Any, decision: str = "send") -> dict[str, str]:
    claimed = _claim(settings, token, expected_hash, draft, decision)
    if claimed is None:
        try:
            import approval_relay
            approval_relay.mark_local(settings, token, "cancelled")
        except Exception:
            pass
        return {"status": "denied", "result": "Draft denied"}
    tool, edited = claimed
    try:
        if tool == "email.send":
            result = await asyncio.to_thread(_send_email, settings, edited)
        elif tool == "github.comment":
            result = await asyncio.to_thread(_send_comment, settings, edited)
        elif tool == "github.create_issue":
            result = await asyncio.to_thread(_send_issue, settings, edited)
        else:
            from mcp_client import run_mcp_tool
            result = await run_mcp_tool(settings, tool, edited["payload"])
        _finish(settings, token, "sent", result)
        return {"status": "sent", "result": redact_text(str(result))[:1500]}
    except Exception:
        # Even transport exceptions can follow a successful remote write.
        _finish(settings, token, "needs_reconciliation", "Outcome uncertain; do not retry")
        return {"status": "needs_reconciliation", "result": "Outcome uncertain; do not retry"}


async def approve_original(settings: Any, token: str) -> dict[str, str]:
    """Telegram/Feishu human Approve: original hash only; never editable there."""
    with _connection(settings) as conn:
        row = conn.execute("SELECT original_hash, draft FROM outbound_drafts WHERE token=?", (token,)).fetchone()
        if row is None:
            return {"status": "missing", "result": "Use Web Approval Inbox"}
        payload = json.loads(row["draft"])
    try:
        return await submit(settings, token, expected_hash=row["original_hash"], draft=payload)
    except (KeyError, ValueError) as exc:
        return {"status": "conflict", "result": str(exc)}


def deny(settings: Any, token: str) -> None:
    with _connection(settings) as conn:
        conn.execute("UPDATE outbound_drafts SET status='denied', draft='{}' WHERE token=? AND status='pending'", (token,))
