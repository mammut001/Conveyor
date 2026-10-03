"""approval_inbox.py — Unified approval inbox with editable drafts (roadmap P2-2).

Aggregates pending tool confirmation actions and job apply/discard approvals,
providing draft parsing, validation, editing, and execution gating.
"""
from __future__ import annotations

import email.utils
import logging
import re
from datetime import datetime, timezone
from typing import Any

from handlers.tools.audit import audit_tool_event
from handlers.tools.confirm import (
    PendingToolAction,
    get_pending,
    list_pending,
    replace_pending_arg,
)
from redaction import redact_text

logger = logging.getLogger(__name__)

EDITABLE_TOOLS: frozenset[str] = frozenset({
    "email.send",
    "github.comment",
    "github.create_issue",
    "notes.add",
    "memory.remember",
    "routine.create",
    "skill.create",
})

_SINGLE_LINE_FIELDS = frozenset({"to", "subject", "number", "title", "cron", "name", "description"})
_LENGTH_CAPS = {
    "to": 320,
    "subject": 200,
    "title": 200,
    "name": 80,
    "description": 300,
    "body": 20000,
    "text": 4000,
    "prompt": 4000,
}

_EMAIL_PATTERN = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


def _is_valid_email_token(addr: str) -> bool:
    addr = addr.strip()
    if not addr:
        return False
    _, parsed = email.utils.parseaddr(addr)
    target = parsed if parsed else addr
    return bool(_EMAIL_PATTERN.match(target))


def _validate_common_rules(draft: dict[str, Any], allowed_fields: set[str]) -> None:
    if not isinstance(draft, dict):
        raise ValueError("Draft must be a JSON object")

    for field in allowed_fields:
        if field not in draft:
            raise ValueError(f"Missing required field: {field}")

    for k, v in draft.items():
        if k not in allowed_fields:
            raise ValueError(f"Unknown field in draft: {k}")
        if not isinstance(v, str):
            raise ValueError(f"Field '{k}' must be a string")
        if redact_text(v) != v:
            raise ValueError("drafts cannot contain secrets or tokens")
        cap = _LENGTH_CAPS.get(k)
        if cap is not None and len(v) > cap:
            raise ValueError(f"Field '{k}' exceeds length limit of {cap} characters")
        if k in _SINGLE_LINE_FIELDS:
            if "|" in v:
                raise ValueError(f"Field '{k}' cannot contain pipes (|)")
            if "\n" in v or "\r" in v:
                raise ValueError(f"Field '{k}' cannot contain newlines")


def parse_draft(tool_name: str, arg: str) -> dict[str, str] | None:
    """Parse a tool argument string into a structured draft dict.

    Returns None if the tool is not editable or if arg cannot be parsed.
    """
    if tool_name not in EDITABLE_TOOLS:
        return None

    raw = arg or ""

    if tool_name == "email.send":
        parts = raw.split("|", 2)
        if len(parts) < 3:
            return None
        to = parts[0].strip()
        subject = parts[1].strip()
        body = parts[2].strip()
        if not to or not subject:
            return None
        return {"to": to, "subject": subject, "body": body}

    if tool_name == "github.comment":
        parts = raw.split("|", 1)
        if len(parts) < 2:
            return None
        number = parts[0].strip()
        body = parts[1].strip()
        if not number.isdigit():
            return None
        return {"number": number, "body": body}

    if tool_name == "github.create_issue":
        parts = raw.split("|", 1)
        title = parts[0].strip()
        body = parts[1].strip() if len(parts) > 1 else ""
        if not title:
            return None
        return {"title": title, "body": body}

    if tool_name in ("notes.add", "memory.remember"):
        text = raw.strip()
        if not text:
            return None
        return {"text": text}

    if tool_name == "routine.create":
        parts = [p.strip() for p in raw.split("|", 2)]
        if len(parts) < 2:
            return None
        cron = parts[0]
        prompt = parts[1]
        name = parts[2] if len(parts) >= 3 else ""
        if not cron or not prompt:
            return None
        return {"cron": cron, "prompt": prompt, "name": name}

    if tool_name == "skill.create":
        parts = raw.split("|", 2)
        if len(parts) < 3:
            return None
        name = parts[0].strip()
        description = parts[1].strip()
        body = parts[2].strip()
        if not name or not description or not body:
            return None
        return {"name": name, "description": description, "body": body}

    return None


def build_arg(tool_name: str, draft: dict[str, Any]) -> str:
    """Validate a draft dict and construct the command argument string.

    Raises ValueError with an operator-facing message on any validation error.
    Guarantees build_arg(tool_name, parse_draft(tool_name, arg)) == arg
    for well-formed arguments.
    """
    if tool_name not in EDITABLE_TOOLS:
        raise ValueError(f"Tool {tool_name} does not support editable drafts")

    if tool_name == "email.send":
        _validate_common_rules(draft, {"to", "subject", "body"})
        to = draft["to"].strip()
        subject = draft["subject"].strip()
        body = draft["body"]
        if not to:
            raise ValueError("Field 'to' cannot be empty")
        if not subject:
            raise ValueError("Field 'subject' cannot be empty")

        # Comma-separated email addresses validation
        addrs = [a.strip() for a in to.split(",")]
        if not addrs or any(not a for a in addrs):
            raise ValueError("Field 'to' must contain valid comma-separated email addresses")
        for addr in addrs:
            if not _is_valid_email_token(addr):
                raise ValueError(f"Invalid email address in 'to': {addr}")

        return f"{to} | {subject} | {body}"

    if tool_name == "github.comment":
        _validate_common_rules(draft, {"number", "body"})
        number = draft["number"].strip()
        body = draft["body"]
        if not number or not number.isdigit():
            raise ValueError("Field 'number' must contain only digits")
        return f"{number} | {body}"

    if tool_name == "github.create_issue":
        _validate_common_rules(draft, {"title", "body"})
        title = draft["title"].strip()
        body = draft["body"]
        if not title:
            raise ValueError("Field 'title' cannot be empty")
        return f"{title} | {body}"

    if tool_name == "notes.add":
        _validate_common_rules(draft, {"text"})
        text = draft["text"].strip()
        if not text:
            raise ValueError("Field 'text' cannot be empty")
        return text

    if tool_name == "memory.remember":
        _validate_common_rules(draft, {"text"})
        text = draft["text"].strip()
        if not text:
            raise ValueError("Field 'text' cannot be empty")
        from personal_tools.long_term_memory import screen_write_arg
        screened = screen_write_arg("memory.remember", text)
        if screened.error:
            raise ValueError(screened.error)
        return screened.arg

    if tool_name == "routine.create":
        _validate_common_rules(draft, {"cron", "prompt", "name"})
        cron = draft["cron"].strip()
        prompt = draft["prompt"]
        name = draft["name"].strip()
        if not cron:
            raise ValueError("Field 'cron' cannot be empty")
        if not prompt.strip():
            raise ValueError("Field 'prompt' cannot be empty")
        if "|" in prompt:
            raise ValueError("Field 'prompt' cannot contain pipes (|)")

        import routines
        try:
            routines.validate_cron(cron)
        except Exception as exc:
            raise ValueError(f"Invalid cron expression: {exc}") from exc

        return f"{cron} | {prompt} | {name}"

    if tool_name == "skill.create":
        _validate_common_rules(draft, {"name", "description", "body"})
        name = draft["name"].strip()
        description = draft["description"].strip()
        body = draft["body"].strip()
        if not name:
            raise ValueError("Field 'name' cannot be empty")
        if not description:
            raise ValueError("Field 'description' cannot be empty")
        if not body:
            raise ValueError("Field 'body' cannot be empty")
        from personal_tools.skills import validate_skill_fields
        validate_skill_fields(name=name, description=description, body=body)
        return f"{name} | {description} | {body}"

    raise ValueError(f"Unsupported tool: {tool_name}")


def edit_pending(settings: Any, token: str, draft: dict[str, Any]) -> PendingToolAction:
    """Atomically replace the argument of a still-pending editable action.

    Raises KeyError if not found or expired.
    Raises PermissionError if not editable or if original arg was redacted.
    Raises ValueError on validation failure.
    """
    action = get_pending(token)
    if action is None:
        raise KeyError(f"Pending tool action {token} not found or expired")
    if action.channel != "web":
        raise KeyError(f"Pending tool action {token} is not on web channel")

    if action.tool_name not in EDITABLE_TOOLS:
        raise PermissionError(f"Tool {action.tool_name} does not support editable drafts")

    # If the original arg was redacted, editing is disabled to prevent writing
    # a redacted placeholder back into the command.
    if redact_text(action.arg) != action.arg:
        raise PermissionError("Cannot edit a pending action with redacted content")

    new_arg = build_arg(action.tool_name, draft)

    old_arg = action.arg
    updated = replace_pending_arg(token, new_arg)
    if updated is None:
        raise KeyError(f"Pending tool action {token} expired during edit")

    # If persisted in routine_approvals, update that row's arg too
    try:
        import routines
        routines.update_routine_approval_arg(settings, token, new_arg)
    except Exception:
        logger.debug("Failed to update routine_approvals DB row for %s", token, exc_info=True)

    # Audit event with tool name and redacted previews of old and new arg
    try:
        from handlers.tools.registry import get_tool
        from personal_tools.registry import personal_tool_danger
        spec = get_tool(updated.tool_name)
        danger = spec.danger.value if spec else personal_tool_danger(updated.tool_name)
        audit_tool_event(
            settings,
            operator_id=updated.operator_id,
            chat_id=updated.chat_id,
            channel=updated.channel,
            tool_name=updated.tool_name,
            arg=new_arg,
            old_arg=old_arg,
            danger=danger,
            action="edited",
        )
    except Exception:
        logger.exception("Failed to write audit event for edited approval %s", token)

    return updated


def list_items(settings: Any, control: Any) -> list[dict[str, Any]]:
    """Return all pending approvals (tools and jobs) newest first.

    - Pending tool actions on channel 'web'
    - Pending job apply/discard approvals from control.list_approvals()
    """
    try:
        import handlers.tools.executors  # register builtin tools
        from personal_tools.registry import register_personal_tools
        register_personal_tools()
    except Exception:
        pass

    from handlers.tools.registry import get_tool
    from personal_tools.registry import get_personal_tool, personal_tool_danger
    from transcript_store import session_identity

    items: list[dict[str, Any]] = []

    # 1. Pending tool actions on web channel
    for action in list_pending(channel="web"):
        spec = get_tool(action.tool_name)
        if spec is not None:
            summary = spec.summary
            danger = spec.danger.value
        elif action.tool_name.startswith("mcp."):
            from mcp_client import get_mcp_tool_spec
            mspec = get_mcp_tool_spec(settings, action.tool_name)
            summary = mspec.summary if mspec else action.tool_name
            danger = mspec.danger.value if mspec else "write"
        else:
            pspec = get_personal_tool(action.tool_name)
            summary = pspec.summary if pspec else action.tool_name
            danger = personal_tool_danger(action.tool_name)

        source = "chat"
        routine_id: int | None = None
        routine_name: str | None = None

        if action.chat_id.startswith("routine-"):
            source = "routine"
            raw_id = action.chat_id[len("routine-"):]
            if raw_id.isdigit():
                routine_id = int(raw_id)
                try:
                    import routines
                    r_obj = routines.get_routine(settings, routine_id)
                    if r_obj:
                        routine_name = r_obj.get("name")
                    conn = routines._connect(settings)
                    try:
                        row = conn.execute(
                            "SELECT trigger FROM routine_runs WHERE approval_id = ? ORDER BY id DESC LIMIT 1",
                            (action.token,),
                        ).fetchone()
                        if row and row["trigger"] == "webhook":
                            source = "webhook"
                    finally:
                        conn.close()
                except Exception:
                    pass

        redacted_arg = redact_text(action.arg)
        is_redacted = (redacted_arg != action.arg)

        draft = parse_draft(action.tool_name, action.arg)
        if is_redacted:
            editable = False
            draft = None
        elif draft is not None:
            editable = True
            draft = {k: redact_text(v) for k, v in draft.items()}
        else:
            editable = False
            draft = None

        item: dict[str, Any] = {
            "id": action.token,
            "kind": "tool",
            "source": source,
            "tool_name": action.tool_name,
            "summary": summary,
            "danger": danger,
            "arg": redacted_arg,
            "draft": draft,
            "editable": editable,
            "created_at": action.created_at,
            "expires_at": action.expires_at,
            "session_id": session_identity(action.channel, action.chat_id, action.operator_id),
            "routine_id": routine_id,
            "routine_name": routine_name,
        }
        if is_redacted:
            item["redacted"] = True

        items.append(item)

    # 2. Pending job approvals from control.list_approvals()
    try:
        raw_approvals = control.list_approvals()
        for raw in raw_approvals:
            if raw.get("kind") == "job":
                items.append({
                    "id": raw["id"],
                    "kind": "job",
                    "source": "job",
                    "action": raw.get("action"),
                    "job_id": raw.get("job_id"),
                    "created_at": raw.get("created_at"),
                    "expires_at": raw.get("expires_at"),
                    "editable": False,
                    "draft": None,
                })
    except Exception:
        logger.exception("Failed to fetch job approvals in list_items")

    # 3. Sort newest first
    def _sort_ts(it: dict[str, Any]) -> float:
        ca = it.get("created_at")
        if isinstance(ca, (int, float)):
            return float(ca)
        if isinstance(ca, str):
            try:
                return datetime.fromisoformat(ca.replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
        return 0.0

    items.sort(key=_sort_ts, reverse=True)
    return items
