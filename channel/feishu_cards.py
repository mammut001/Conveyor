"""channel/feishu_cards.py — Feishu interactive message card builders + action mapping.

Pure functions: no network, no I/O, no SDK imports. The Feishu adapter
(channel/feishu.py) sends the dicts returned here; the dispatch path
handles them when the user clicks a button.

Card schema reference: Feishu/Lark Open Platform
  https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-json-v2-structure
  https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-callback-communication

Action callback contract
------------------------
Button ``value`` payloads are small JSON objects, e.g.::

    {"action": "diff", "job_id": "..."}
    {"action": "apply", "job_id": "..."}
    {"action": "discard", "job_id": "..."}
    {"action": "cancel", "job_id": "..."}
    {"action": "status"}
    {"action": "confirm", "token": "..."}
    {"action": "cancel_confirm", "token": "..."}

The action key is the only thing the callback handler trusts from
client input. Anything not in ``ALLOWED_ACTIONS`` is rejected as
unknown. Confirmation tokens go through the existing
``handlers.tools.confirm`` binding checks (operator + chat + channel).
"""
from __future__ import annotations

import json
from typing import Any

# ---- Action allowlist -------------------------------------------------------

#: Hardcoded set of action names the callback handler will accept.
#: Anything else is rejected with a short safe error. Do NOT trust
#: client-provided strings outside this list.
ALLOWED_ACTIONS: frozenset[str] = frozenset({
    "status",
    "diff",
    "apply",
    "discard",
    "cancel",
    "confirm",
    "cancel_confirm",
    # P5.0: Execution-node layer actions. Both are read-only
    # refreshes — no token, no command argument, no chat-changing
    # behavior. They are mapped to the existing /nodes and
    # /computer_status commands in :func:`action_to_command`.
    "nodes_status",
    "computer_status",
    "desktop_screenshot_status",
    "desktop_observe_status",
    "desktop_observe_cancel",
})

#: Maximum number of action buttons rendered in a single card row.
#: Feishu cards cap interactive components per row; 5 keeps the
#: confirm/cancel pattern + a small action set in one row.
_MAX_BUTTONS_PER_ROW = 5

#: Tone templates supported by ``status_card``. Feishu accepts these
#: directly under ``header.template``.
_TONE_TEMPLATES: dict[str, str] = {
    "blue": "blue",
    "wathet": "wathet",
    "turquoise": "turquoise",
    "green": "green",
    "yellow": "yellow",
    "orange": "orange",
    "red": "red",
    "carmine": "carmine",
    "violet": "violet",
    "purple": "purple",
    "indigo": "indigo",
    "grey": "grey",
}


# ---- Action payload parsing ------------------------------------------------


def parse_action(value: Any) -> dict[str, Any] | None:
    """Parse and validate a card button ``value`` payload.

    Accepts either a dict (already parsed by the SDK) or a JSON string
    (some lark-oapi versions pass the raw JSON). Returns the validated
    payload or ``None`` if the payload is missing the required ``action``
    key, the action is outside the allowlist, or any non-empty extra
    field is the wrong type.

    Strict mode: if a payload contains an unknown field or a known
    field with a wrong type, the payload is rejected entirely. This
    matches the security guidance "do not trust client-provided
    command strings" — better to drop the press than to act on a
    malformed value.

    The function never raises: any parse / shape error returns ``None``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict):
        return None
    action = value.get("action")
    if not isinstance(action, str):
        return None
    if action not in ALLOWED_ACTIONS:
        return None
    # Reject unknown keys (typos, future fields the client tries to
    # sneak in). Acceptable fields depend on the action.
    expected_keys = {"action", "job_id", "token"}
    extra = set(value.keys()) - expected_keys
    if extra:
        return None
    # Coerce known fields to the expected types. Numbers, booleans,
    # lists, and other non-string types are rejected.
    payload: dict[str, Any] = {"action": action}
    job_id = value.get("job_id")
    if job_id is not None:
        if not isinstance(job_id, str) or not job_id:
            return None
        payload["job_id"] = job_id
    token = value.get("token")
    if token is not None:
        if not isinstance(token, str) or not token:
            return None
        payload["token"] = token
    # Action-specific shape check: confirm/cancel_confirm must carry
    # a token, slash-style actions may carry a job_id. Don't enforce
    # job_id presence (a `status` action has no job_id) but reject
    # token on slash actions since it's meaningless.
    if action in ("confirm", "cancel_confirm") and "token" not in payload:
        return None
    slash_actions = (
        "status", "diff", "apply", "discard", "cancel",
        # P5.0: execution-node refreshes are also slash-style
        # (no token, no job_id, no extra state).
        "nodes_status", "computer_status", "desktop_screenshot_status",
        "desktop_observe_status", "desktop_observe_cancel",
    )
    if action in slash_actions and "token" in payload:
        return None
    return payload


# ---- Internal helpers ------------------------------------------------------


def _header(title: str, tone: str = "blue") -> dict[str, Any]:
    template = _TONE_TEMPLATES.get(tone, "blue")
    return {
        "title": {
            "tag": "plain_text",
            "content": title,
        },
        "template": template,
    }


def _markdown(content: str) -> dict[str, Any]:
    return {
        "tag": "markdown",
        "content": content,
    }


def _actions_row(buttons: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "tag": "action",
        "actions": buttons,
    }


def _button(label: str, value: dict[str, Any], tone: str = "default") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tag": "button",
        "text": {
            "tag": "plain_text",
            "content": label,
        },
        "type": "primary" if tone == "primary" else "default",
        "value": value,
    }
    return payload


def _truncate(text: str, limit: int = 1500) -> str:
    """Card content has a hard length cap. Truncate with a marker.

    Keeps the tail of the original text so the most recent context is
    preserved (matches the existing ``redaction.truncate`` behavior).
    """
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    suffix = "\n…(truncated)"
    return text[: max(0, limit - len(suffix))] + suffix


def _format_file_list(files: list[str] | None, limit: int = 8) -> str:
    if not files:
        return ""
    head = files[:limit]
    lines = [f"- `{f}`" for f in head]
    extra = len(files) - len(head)
    if extra > 0:
        lines.append(f"- … +{extra} more")
    return "\n".join(lines)


# ---- Public card builders --------------------------------------------------


def job_started_card(
    job_id: str,
    prompt: str,
    worktree: str | None = None,
) -> dict[str, Any]:
    """Card sent right after a Codex job has been accepted.

    ``job_id`` and ``worktree`` (if known) are rendered as a status
    field. Action buttons give the operator quick access to common
    follow-ups without retyping slash commands.
    """
    body_lines = [
        f"**Job**: `{job_id}`",
    ]
    if worktree:
        body_lines.append(f"**Worktree**: `{worktree}`")
    body_lines.append("")
    body_lines.append("**Prompt**")
    body_lines.append(_truncate(prompt, 600))
    buttons: list[dict[str, Any]] = [
        _button("Status", {"action": "status"}),
        _button("Diff", {"action": "diff", "job_id": job_id}),
        _button("Cancel", {"action": "cancel", "job_id": job_id}, tone="primary"),
    ]
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header("Codex job started", "blue"),
        "elements": [
            _markdown("\n".join(body_lines)),
            _actions_row(buttons),
        ],
    }


def job_finished_card(
    job_id: str,
    summary: str,
    changed_files: list[str] | None = None,
) -> dict[str, Any]:
    """Card sent when a Codex job completes successfully."""
    body_lines = [
        f"**Job**: `{job_id}`",
        "",
        "**Summary**",
        _truncate(summary, 1500),
    ]
    files = _format_file_list(changed_files)
    if files:
        body_lines.append("")
        body_lines.append("**Changed files**")
        body_lines.append(files)
    buttons: list[dict[str, Any]] = [
        _button("Status", {"action": "status"}),
        _button("Diff", {"action": "diff", "job_id": job_id}),
        _button("Apply", {"action": "apply", "job_id": job_id}, tone="primary"),
        _button("Discard", {"action": "discard", "job_id": job_id}),
    ]
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header("Codex job finished", "green"),
        "elements": [
            _markdown("\n".join(body_lines)),
            _actions_row(buttons),
        ],
    }


def job_failed_card(
    job_id: str,
    error: str,
) -> dict[str, Any]:
    """Card sent when a Codex job errors out.

    The error is sanitized (no stack traces, no secrets) by the
    caller. The builder itself only truncates and prefixes.
    """
    body_lines = [
        f"**Job**: `{job_id}`",
        "",
        "**Error**",
        _truncate(error, 1500),
    ]
    buttons: list[dict[str, Any]] = [
        _button("Status", {"action": "status"}),
        _button("Cancel", {"action": "cancel", "job_id": job_id}),
    ]
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header("Codex job failed", "red"),
        "elements": [
            _markdown("\n".join(body_lines)),
            _actions_row(buttons),
        ],
    }


def diff_preview_card(
    job_id: str,
    diff_summary: str,
    changed_files: list[str] | None = None,
) -> dict[str, Any]:
    """Card sent in response to ``/diff`` on Feishu.

    The full diff text is intentionally not embedded: this card is
    a navigation surface (Apply / Discard / Status) plus a short
    summary. The complete diff is still available via the existing
    text path (``port.reply``) so existing truncation and redaction
    rules apply.
    """
    body_lines = [
        f"**Job**: `{job_id}`",
        "",
        "**Diff summary**",
        _truncate(diff_summary, 1500),
    ]
    files = _format_file_list(changed_files)
    if files:
        body_lines.append("")
        body_lines.append("**Changed files**")
        body_lines.append(files)
    buttons: list[dict[str, Any]] = [
        _button("Status", {"action": "status"}),
        _button("Apply", {"action": "apply", "job_id": job_id}, tone="primary"),
        _button("Discard", {"action": "discard", "job_id": job_id}),
    ]
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header("Diff preview", "wathet"),
        "elements": [
            _markdown("\n".join(body_lines)),
            _actions_row(buttons),
        ],
    }


def confirm_action_card(
    token: str,
    title: str,
    body: str,
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
) -> dict[str, Any]:
    """Card sent when a dangerous tool requires confirmation.

    The card carries the existing confirmation token via the button
    ``value`` payload. The callback handler feeds that token into
    ``handlers.tools.confirm`` so binding (operator + chat + channel)
    and TTL still apply.
    """
    body_lines = [
        _truncate(body, 1500),
        "",
        "_Reply in chat with `确认执行` or `取消` also works._",
    ]
    buttons: list[dict[str, Any]] = [
        _button(confirm_label, {"action": "confirm", "token": token}, tone="primary"),
        _button(cancel_label, {"action": "cancel_confirm", "token": token}),
    ]
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header(title, "orange"),
        "elements": [
            _markdown("\n".join(body_lines)),
            _actions_row(buttons),
        ],
    }


def status_card(
    title: str,
    fields: list[tuple[str, str]],
    tone: str = "blue",
) -> dict[str, Any]:
    """Generic status card with a small key/value table.

    Each ``fields`` entry is ``(label, value)``. Values are rendered
    inside a single markdown block so multi-line content stays
    readable. Use this for snapshots (e.g. ``/status``, deploy state).
    """
    if not fields:
        body = "_no data_"
    else:
        rows = [f"**{label}**\n{_truncate(value, 600)}" for label, value in fields]
        body = "\n\n".join(rows)
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header(title, tone),
        "elements": [
            _markdown(body),
        ],
    }


# ---- P5.0: Execution-node card builders ---------------------------------


def node_status_card(summary_text: str) -> dict[str, Any]:
    """Card sent in response to ``/nodes`` on Feishu.

    ``summary_text`` is the same body the text fallback would
    deliver (the ``nodes.status`` tool's output). The card keeps
    it as a single markdown block and adds a refresh button so
    the operator can re-poll without typing.

    Buttons are limited to read-only refreshes. Anything that
    would change state on a node (start a Codex job, restart a
    service, attempt a desktop action) intentionally has no
    shortcut on this card.
    """
    buttons: list[dict[str, Any]] = [
        _button("Refresh", {"action": "nodes_status"}),
        _button("Computer Use", {"action": "computer_status"}),
    ]
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header("Execution nodes", "blue"),
        "elements": [
            _markdown(_truncate(summary_text, 1500)),
            _actions_row(buttons),
        ],
    }


def _observe_readonly_preamble() -> str:
    return (
        "**Read-only · status only**\n"
        "Metadata only. No upload, no preview, or Computer Use control from this card."
    )


def _desktop_observe_card_context(summary_text: str) -> tuple[str, str, str]:
    """Return (title, color, body) for observe result cards."""
    text = (summary_text or "").strip()
    if text.startswith("❌") or "截图失败" in text[:120]:
        return "截图失败", "red", text
    if text.startswith("⏱️") or "已过期" in text[:80]:
        return "截图已过期", "orange", text
    if text.startswith("🚫") or "已取消" in text[:80]:
        return "截图已取消", "grey", text
    if "仅元数据" in text or "不上传图片" in text:
        preamble = "**只读 · 仅元数据**\n不会上传图片或缩略图预览。"
        body = f"{preamble}\n\n{text}" if text else preamble
        return "桌面截图请求", "turquoise", body
    if text:
        return "桌面截图", "blue", text
    return "桌面截图", "blue", "已创建截图请求，请稍候。"


def desktop_observe_request_card(summary_text: str) -> dict[str, Any]:
    """Card for remote observe request creation (P5.3+)."""
    title, color, body = _desktop_observe_card_context(summary_text)
    buttons: list[dict[str, Any]] = [
        _button("刷新状态", {"action": "desktop_observe_status"}),
        _button("节点状态", {"action": "nodes_status"}),
    ]
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header(title, color),
        "elements": [
            _markdown(_truncate(body, 1500)),
            _actions_row(buttons),
        ],
    }


def desktop_observe_status_card(summary_text: str) -> dict[str, Any]:
    """Card for observe status / recent requests (P5.3)."""
    preamble = _observe_readonly_preamble()
    body = preamble
    if summary_text.strip():
        body = f"{preamble}\n\n{summary_text.strip()}"
    buttons: list[dict[str, Any]] = [
        _button("Refresh", {"action": "desktop_observe_status"}),
        _button("Nodes", {"action": "nodes_status"}),
    ]
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header("Desktop Observe Status", "turquoise"),
        "elements": [
            _markdown(_truncate(body, 1500)),
            _actions_row(buttons),
        ],
    }


def desktop_screenshot_status_card(summary_text: str) -> dict[str, Any]:
    """Card for ``/desktop_screenshot_status`` / ``/screenshot_status``.

    Read-only status only — no capture, upload, preview, or analyze buttons.
    """
    preamble = (
        "**Read-only · status only**\n"
        "This card shows local screenshot metadata. "
        "It does not capture, upload, or preview images."
    )
    body = preamble
    if summary_text.strip():
        body = f"{preamble}\n\n{summary_text.strip()}"
    buttons: list[dict[str, Any]] = [
        _button("Refresh", {"action": "desktop_screenshot_status"}),
        _button("Nodes", {"action": "nodes_status"}),
    ]
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header("Desktop Screenshot Observe", "turquoise"),
        "elements": [
            _markdown(_truncate(body, 1500)),
            _actions_row(buttons),
        ],
    }


def desktop_upload_request_card(summary_text: str) -> dict[str, Any]:
    """Card for manual screenshot thumbnail upload request (P5.4)."""
    preamble = (
        "**Thumbnail Upload Request**\n"
        "Manual thumbnail preview only — no full-resolution upload, no Computer Use control."
    )
    body = preamble
    if summary_text.strip():
        body = f"{preamble}\n\n{summary_text.strip()}"
    buttons: list[dict[str, Any]] = [
        _button("Refresh status", {"action": "desktop_upload_status"}),
        _button("Nodes", {"action": "nodes_status"}),
    ]
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header("Thumbnail Upload Request", "orange"),
        "elements": [
            _markdown(_truncate(body, 1500)),
            _actions_row(buttons),
        ],
    }


def desktop_upload_status_card(summary_text: str) -> dict[str, Any]:
    """Card for upload status / recent requests (P5.4)."""
    preamble = (
        "**Thumbnail Upload Status**\n"
        "Shows recent thumbnail upload requests. "
        "Thumbnail only — no full-resolution upload, no Computer Use control."
    )
    body = preamble
    if summary_text.strip():
        body = f"{preamble}\n\n{summary_text.strip()}"
    buttons: list[dict[str, Any]] = [
        _button("Refresh status", {"action": "desktop_upload_status"}),
        _button("Nodes", {"action": "nodes_status"}),
    ]
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header("Thumbnail Upload Status", "orange"),
        "elements": [
            _markdown(_truncate(body, 1500)),
            _actions_row(buttons),
        ],
    }


def computer_status_card(summary_text: str) -> dict[str, Any]:
    """Card sent in response to ``/computer_status`` on Feishu.

    Same shape as :func:`node_status_card` but with a Computer
    Use header tone and a refresh button. The body is the same
    "stub / not implemented" content — this card exists so the
    operator gets the same UX whether they type the command or
    click the button on the nodes card.
    """
    buttons: list[dict[str, Any]] = [
        _button("Refresh", {"action": "computer_status"}),
        _button("Back to nodes", {"action": "nodes_status"}),
    ]
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": _header("Computer Use", "wathet"),
        "elements": [
            _markdown(_truncate(summary_text, 1500)),
            _actions_row(buttons),
        ],
    }


# ---- Public introspection (test helpers) ------------------------------------


def flatten_card_to_text(card: dict[str, Any]) -> str:
    """Best-effort text rendering of a card for non-card channels.

    Telegram (and any other non-Feishu channel) does not render
    interactive cards, so ``OutboundPort.send_card`` flattens the
    card to plain text. The result preserves the header title and
    the markdown content of each element, joined with blank lines.
    Truncation matches the card-side limit so a long card does not
    silently inflate a text reply.
    """
    if not isinstance(card, dict):
        return ""
    parts: list[str] = []
    header = card.get("header") or {}
    if isinstance(header, dict):
        title = header.get("title") or {}
        if isinstance(title, dict):
            content = title.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
    for el in card.get("elements") or []:
        if not isinstance(el, dict):
            continue
        if el.get("tag") == "markdown":
            md = el.get("content")
            if isinstance(md, str) and md:
                parts.append(md)
        elif el.get("tag") == "div":
            text = el.get("text") or {}
            if isinstance(text, dict):
                content = text.get("content")
                if isinstance(content, str) and content:
                    parts.append(content)
    return _truncate("\n\n".join(p for p in parts if p), 3000)


def action_to_command(action: str) -> str | None:
    """Map a card action to the slash command it wraps, or ``None``.

    Confirmation actions (confirm / cancel_confirm) do not map to a
    slash command — they reuse the existing token-based confirmation
    system, not the dispatch command table. Execution-node actions
    (P5.0) map to the same /nodes and /computer_status commands the
    operator could type.
    """
    mapping = {
        "status": "status",
        "diff": "diff",
        "apply": "apply",
        "discard": "discard",
        "cancel": "cancel",
        # P5.0: Execution nodes
        "nodes_status": "nodes",
        "computer_status": "computer_status",
        "desktop_screenshot_status": "desktop_screenshot_status",
        "desktop_observe_status": "observe_status",
        "desktop_observe_cancel": "observe_cancel",
        "desktop_upload_status": "upload_status",
        "desktop_upload_cancel": "upload_cancel",
    }
    return mapping.get(action)


# ---- Event extraction (pure, no SDK dependency) ---------------------------


def extract_card_action(msg: Any) -> tuple[dict[str, str], dict[str, Any]] | None:
    """Best-effort extraction of operator/chat + action payload from a
    Feishu card action event.

    Different lark-oapi versions pass the event in slightly different
    shapes; this helper accepts both ``msg.event.*`` (newer, attribute
    style) and ``msg.event.*`` (newer, dict style) and the flat
    ``msg`` (older) layouts, and is defensive against missing fields.
    Returns ``None`` if any required field is missing or invalid.

    The first dict in the tuple carries the channel-agnostic
    identity fields the dispatcher needs (operator_id, chat_id,
    message_id); the second is the validated action payload (as
    returned by :func:`parse_action`).
    """
    if msg is None:
        return None

    # Unwrap the ``event`` envelope for both attribute and dict form.
    if isinstance(msg, dict):
        if isinstance(msg.get("event"), dict):
            event_obj: Any = msg["event"]
        else:
            event_obj = msg
    else:
        inner = getattr(msg, "event", None)
        event_obj = inner if inner is not None else msg

    def _get(obj: Any, key: str) -> Any:
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    operator = _get(event_obj, "operator")
    operator_id = _get(operator, "open_id")
    if not operator_id:
        return None
    context = _get(event_obj, "context")
    chat_id = _get(context, "open_chat_id")
    if not chat_id:
        return None
    message_id = _get(context, "open_message_id")
    action_obj = _get(event_obj, "action")
    value = _get(action_obj, "value")
    payload = parse_action(value)
    if payload is None:
        return None
    identity = {
        "operator_id": str(operator_id),
        "chat_id": str(chat_id),
        "message_id": str(message_id) if message_id else "",
    }
    return identity, payload
