"""handlers/tools/observe_tools.py — P5.3 remote observe request tools."""
from __future__ import annotations

import time

from channel.types import InboundMessage
from config import Settings
from desktop_observe_requests import (
    cancel_observe_request,
    create_observe_request,
    list_recent_observe_requests,
)
from handlers.tools.executors import (
    _format_latest_screenshot_block,
    _safe_truncate,
    _truncate_display_path,
)


def _format_request_summary(record: dict) -> list[str]:
    lines = [
        f"- {record.get('request_id', '?')}: {record.get('status', '?')}",
    ]
    if record.get("created_at"):
        lines.append(f"  created: {record['created_at']}")
    if record.get("user_request"):
        lines.append(f"  request: {record['user_request'][:120]}")
    result = record.get("result")
    if isinstance(result, dict) and record.get("status") == "completed":
        lines.append(f"  screenshot: {result.get('screenshot_id', '?')}")
        sha = result.get("sha256")
        if isinstance(sha, str) and sha:
            lines.append(f"  sha256: {sha[:12]}...")
    error = record.get("error")
    if error and record.get("status") == "failed":
        lines.append(f"  error: {error}")
    return lines


def format_observe_request_created(record: dict, settings: Settings) -> str:
    ttl_minutes = max(1, settings.conveyor_desktop_observe_request_ttl_seconds // 60)
    node_id = record.get("node_id") or settings.conveyor_desktop_node_id or "macbook-payton"
    lines = [
        "📸 Desktop observe request created",
        "",
        f"Request: {record.get('request_id', '?')}",
        f"Target: {node_id}",
        f"Expires: {ttl_minutes} minutes",
        "",
        "The Mac desktop agent will capture one local screenshot and return metadata only.",
        "No image will be uploaded.",
        "Computer Use control is not implemented.",
    ]
    return _safe_truncate("\n".join(lines))


def format_observe_completed(record: dict) -> str:
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    lines = [
        "✅ Desktop observe completed",
        "",
        f"Request: {record.get('request_id', '?')}",
        f"Screenshot: {result.get('screenshot_id', '?')}",
    ]
    if result.get("created_at"):
        lines.append(f"Created: {result['created_at']}")
    width = result.get("width")
    height = result.get("height")
    if width is not None and height is not None:
        lines.append(f"Size: {width}x{height}")
    if result.get("bytes") is not None:
        lines.append(f"Bytes: {result['bytes']}")
    sha = result.get("sha256")
    if isinstance(sha, str) and sha:
        lines.append(f"SHA256: {sha[:12]}...")
    if result.get("path"):
        lines.append(f"Path: {_truncate_display_path(str(result['path']))}")
    lines.extend([
        "",
        "No image was uploaded.",
        "Computer Use control is not implemented.",
    ])
    return _safe_truncate("\n".join(lines))


async def exec_desktop_observe_request(
    settings: Settings,
    msg: InboundMessage,
    user_request: str,
) -> str:
    result = create_observe_request(settings, msg, user_request)
    if not result.get("ok"):
        error = result.get("error", "observe_unavailable")
        message = result.get("message") or "Remote observe is unavailable."
        lines = [
            "Desktop observe request failed",
            "",
            f"Reason: {error}",
            message,
            "",
            "Checks:",
            "- Desktop node enabled",
            "- Desktop agent online (`python desktop_agent.py --poll-observe` on Mac)",
            "- Screenshot helper configured (absolute path)",
            "- Pending request count below limit",
        ]
        return _safe_truncate("\n".join(lines))
    record = result.get("request") or {}
    return format_observe_request_created(record, settings)


async def exec_desktop_observe_status(settings: Settings, _arg: str) -> str:
    from desktop_screenshot import latest_screenshot_metadata
    from nodes.registry import list_nodes
    from nodes.types import NodeStatus, NodeType

    lines = [
        "Desktop Observe Status (P5.3)",
        "",
        "This command does not capture a screenshot.",
        "Use /observe_request (or NL capture phrases) to create a remote observe request.",
        "Mac agent must run: python desktop_agent.py --poll-observe",
        "Upload is disabled in P5.2/P5.3.",
        "",
    ]

    desktop_nodes = [n for n in list_nodes(settings) if n.node_type == NodeType.DESKTOP]
    if desktop_nodes:
        node = desktop_nodes[0]
        state = "online" if node.status == NodeStatus.ONLINE else "offline"
        lines.append(f"Desktop agent: {state} ({node.node_id})")
        if node.status == NodeStatus.ONLINE and node.last_seen_at is not None:
            lines.append(f"Last seen: {max(0, int(time.time() - node.last_seen_at))}s ago")
    else:
        lines.append("Desktop node: not enabled")
    lines.append("")

    recent = list_recent_observe_requests(settings, limit=5)
    if recent:
        lines.append("Recent observe requests:")
        for record in recent:
            lines.extend(_format_request_summary(record))
        lines.append("")
    else:
        lines.append("Recent observe requests: (none)")
        lines.append("")

    latest = latest_screenshot_metadata(settings)
    lines.extend(_format_latest_screenshot_block(latest))
    return _safe_truncate("\n".join(lines))


async def exec_desktop_observe_cancel(settings: Settings, arg: str) -> str:
    request_id = (arg or "").strip()
    if not request_id:
        return "用法: /observe_cancel <request_id>"
    result = cancel_observe_request(settings, request_id)
    if not result.get("ok"):
        return _safe_truncate(
            f"Cancel failed: {result.get('error', 'unknown')} "
            f"(status={result.get('status', '?')})"
        )
    record = result.get("request") or {}
    return _safe_truncate(
        f"Observe request cancelled: {record.get('request_id', request_id)}"
    )