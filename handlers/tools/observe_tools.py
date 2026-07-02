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


def upload_temp_dir_configuration_error(settings: Settings) -> str | None:
    raw = (settings.conveyor_desktop_upload_temp_dir or "").strip()
    if not raw:
        return None
    import os
    from pathlib import Path
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        return "CONVEYOR_DESKTOP_UPLOAD_TEMP_DIR must be an absolute path."
    return None


def resolve_upload_temp_dir(settings: Settings) -> Path:
    import os
    from pathlib import Path
    raw = (settings.conveyor_desktop_upload_temp_dir or "").strip()
    if raw:
        path = Path(os.path.expanduser(raw))
        if path.is_absolute():
            return path
    return settings.codex_memory_root / "desktop" / "uploads"


def _format_upload_summary(record: dict) -> list[str]:
    lines = [
        f"- {record.get('upload_id', '?')}: {record.get('status', '?')}",
    ]
    if record.get("created_at"):
        lines.append(f"  created: {record['created_at']}")
    if record.get("observe_request_id"):
        lines.append(f"  observe_id: {record['observe_request_id']}")
    result = record.get("result")
    if isinstance(result, dict) and record.get("status") == "completed":
        lines.append(f"  size: {result.get('width', '?')}x{result.get('height', '?')}")
        lines.append(f"  bytes: {result.get('bytes', '?')}")
        if record.get("delivered"):
            lines.append("  status: delivered to chat")
        else:
            lines.append("  status: pending delivery")
    error = record.get("error")
    if error and record.get("status") == "failed":
        lines.append(f"  error: {error}")
    return lines


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    from datetime import datetime, timezone
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def exec_desktop_upload_request(
    settings: Settings,
    msg: InboundMessage,
    arg: str,
) -> str:
    from desktop_observe_requests import load_observe_requests
    from desktop_upload_requests import create_upload_request

    temp_dir_err = upload_temp_dir_configuration_error(settings)
    if temp_dir_err:
        return _safe_truncate(f"Desktop upload request failed\n\nReason: invalid_upload_temp_dir\n{temp_dir_err}")

    observe_store = load_observe_requests(settings)
    observe_record = None

    arg = (arg or "").strip()
    if not arg:
        latest_completed = None
        latest_time = None
        for record in observe_store.values():
            if record.get("status") == "completed":
                created = _parse_iso(record.get("created_at"))
                if created is not None:
                    if latest_time is None or created > latest_time:
                        latest_time = created
                        latest_completed = record
        if latest_completed:
            observe_record = latest_completed
        else:
            return _safe_truncate("错误: 找不到任何已完成的 observe 截图请求。")
    elif arg.startswith("obs_"):
        observe_record = observe_store.get(arg)
    else:
        for record in observe_store.values():
            result = record.get("result")
            if isinstance(result, dict) and result.get("screenshot_id") == arg:
                observe_record = record
                break

    if not observe_record:
        return _safe_truncate(f"错误: 找不到对应的 observe 请求或截图 ID: {arg}")

    if observe_record.get("status") != "completed":
        return _safe_truncate(f"错误: observe 请求状态为 {observe_record.get('status')}，必须为 completed 才能申请上传预览。")

    result = create_upload_request(settings, observe_record, msg)
    if not result.get("ok"):
        error = result.get("error", "upload_unavailable")
        message = result.get("message") or "Remote upload is unavailable."
        lines = [
            "Desktop upload request failed",
            "",
            f"Reason: {error}",
            message,
            "",
            "Checks:",
            "- Desktop upload enabled",
            "- Source observe completed",
            "- Pending upload count below limit",
        ]
        return _safe_truncate("\n".join(lines))

    record = result.get("request") or {}
    ttl_minutes = max(1, settings.conveyor_desktop_upload_ttl_seconds // 60)
    lines = [
        "🖼️ Thumbnail upload requested",
        "",
        f"Upload: {record.get('upload_id', '?')}",
        f"Source observe request: {record.get('observe_request_id', '?')}",
        "Mode: thumbnail only",
        f"Limit: {record.get('max_width')}x{record.get('max_height')}, {record.get('max_bytes') // 1000} KB",
        f"Expires: {ttl_minutes} minutes",
        "",
        "The Mac agent will generate a small preview and send it to this chat.",
        "No full-resolution screenshot will be uploaded.",
        "Computer Use control is not implemented.",
    ]
    return _safe_truncate("\n".join(lines))


async def exec_desktop_upload_status(
    settings: Settings,
    arg: str,
    *,
    port: Any = None,
    msg: InboundMessage | None = None,
) -> str:
    from desktop_upload_requests import (
        load_upload_requests,
        list_recent_upload_requests,
        mark_upload_delivered,
    )
    from nodes.registry import list_nodes
    from nodes.types import NodeStatus, NodeType

    temp_dir_err = upload_temp_dir_configuration_error(settings)

    if temp_dir_err is None and port is not None and msg is not None:
        store = load_upload_requests(settings)
        for upload_id, record in store.items():
            if record.get("status") == "completed" and not record.get("delivered"):
                result = record.get("result")
                if isinstance(result, dict) and result.get("thumbnail_path"):
                    thumbnail_path = result.get("thumbnail_path")
                    from pathlib import Path
                    if Path(thumbnail_path).exists():
                        try:
                            if hasattr(port, "send_image"):
                                caption = f"Thumbnail for observe {record.get('observe_request_id')}"
                                target_chat_id = record.get("created_by_chat_id", msg.chat_id)
                                target_channel = record.get("created_by_channel", msg.channel)
                                await port.send_image(
                                    chat_id=target_chat_id,
                                    image_path=thumbnail_path,
                                    caption=caption,
                                )
                                mark_upload_delivered(
                                    settings,
                                    upload_id,
                                    channel=target_channel,
                                    chat_id=target_chat_id,
                                )
                        except Exception:
                            pass

    lines = [
        "Desktop Upload Status (P5.4)",
        "",
        "This command shows status and triggers pending thumbnail deliveries.",
        "Mac agent must run: python desktop_agent.py --poll-observe",
        "",
    ]
    if temp_dir_err:
        lines.append(f"Configuration Error: {temp_dir_err}")
        lines.append("")

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

    recent = list_recent_upload_requests(settings, limit=5)
    if recent:
        lines.append("Recent upload requests:")
        for record in recent:
            lines.extend(_format_upload_summary(record))
        lines.append("")
    else:
        lines.append("Recent upload requests: (none)")
        lines.append("")

    return _safe_truncate("\n".join(lines))


async def exec_desktop_upload_cancel(settings: Settings, arg: str) -> str:
    from desktop_upload_requests import cancel_upload_request
    upload_id = (arg or "").strip()
    if not upload_id:
        return "用法: /upload_cancel <upload_id>"
    result = cancel_upload_request(settings, upload_id)
    if not result.get("ok"):
        return _safe_truncate(
            f"Cancel failed: {result.get('error', 'unknown')} "
            f"(status={result.get('status', '?')})"
        )
    record = result.get("request") or {}
    return _safe_truncate(
        f"Upload request cancelled: {record.get('upload_id', upload_id)}"
    )


async def exec_desktop_upload_cleanup(settings: Settings, _arg: str) -> str:
    temp_dir_err = upload_temp_dir_configuration_error(settings)
    if temp_dir_err:
        return f"Refusing cleanup: {temp_dir_err}"

    upload_dir = resolve_upload_temp_dir(settings)
    if not upload_dir.exists():
        return "No upload temp directory exists."

    upload_dir = upload_dir.resolve()
    retention = settings.conveyor_desktop_upload_retention_seconds
    now = time.time()
    deleted_count = 0
    total_size = 0

    for p in upload_dir.iterdir():
        if p.is_symlink():
            continue
        if p.is_file():
            # Check directory traversal safety
            try:
                p.resolve().relative_to(upload_dir)
            except ValueError:
                continue

            mtime = p.stat().st_mtime
            if now - mtime > retention:
                total_size += p.stat().st_size
                try:
                    p.unlink()
                    deleted_count += 1
                except Exception:
                    pass
    return f"Cleanup completed. Deleted {deleted_count} file(s) ({total_size} bytes)."