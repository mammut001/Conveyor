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

_OBSERVE_ERROR_HINTS_ZH: dict[str, tuple[str, list[str]]] = {
    "screen_recording_permission_required": (
        "Mac 还没有「屏幕录制」权限（或授权后 agent 未重启）。",
        [
            "系统设置 → 隐私与安全性 → 屏幕录制：同时打开 Conveyor Agent 和 capture-screen-helper",
            "菜单栏 Conveyor Agent →「开启屏幕录制权限…」（会自动定位两个程序）",
            "若弹出提示，选择「退出并重新打开」",
            "菜单栏点 Restart All，再在飞书重试截图",
        ],
    ),
    "screenshot_helper_not_configured": (
        "Mac 未配置截图工具。",
        [
            "确认 .desktop-agent.env 里 CONVEYOR_DESKTOP_SCREENSHOT_HELPER 是绝对路径",
            "重新运行 scripts/setup-desktop-agent.sh 或安装 capture-screen-helper",
        ],
    ),
    "screenshot_helper_not_found": (
        "找不到截图工具可执行文件。",
        [
            "检查 CONVEYOR_DESKTOP_SCREENSHOT_HELPER 路径是否存在",
            "从 capture-your-screen 重新构建并安装 helper",
        ],
    ),
    "screenshot_helper_path_not_absolute": (
        "截图工具路径必须是绝对路径。",
        [
            "把 CONVEYOR_DESKTOP_SCREENSHOT_HELPER 改成类似 /Users/你/.local/bin/capture-screen-helper",
        ],
    ),
    "screenshot_helper_timeout": (
        "截图工具执行超时。",
        ["稍后重试；若反复出现，重启 Conveyor Agent"],
    ),
    "helper_empty_output": (
        "截图工具没有返回结果。",
        ["检查 capture-screen-helper 是否可执行，并查看 ~/Library/Logs/conveyor-desktop-agent.log"],
    ),
    "helper_invalid_json": (
        "截图工具返回了无效数据。",
        ["尝试在终端运行 capture-screen-helper --check-permission --json 排查"],
    ),
    "observe_capture_failed": (
        "Mac 本地截图失败。",
        ["查看 agent 日志；确认屏幕录制权限与 agent 在线"],
    ),
    "capture_failed": (
        "屏幕捕获失败。",
        ["确认屏幕录制权限已授予 capture-screen-helper"],
    ),
    "output_missing": (
        "截图文件没有生成。",
        ["检查磁盘空间与 CONVEYOR_DESKTOP_SCREENSHOT_DIR 是否可写"],
    ),
    "screenshot_too_large": (
        "截图文件超过大小上限。",
        ["可调大 CONVEYOR_DESKTOP_SCREENSHOT_MAX_BYTES，或降低分辨率"],
    ),
}


def _observe_error_hint_zh(error_code: str) -> tuple[str, list[str]]:
    code = (error_code or "").strip()
    if code in _OBSERVE_ERROR_HINTS_ZH:
        return _OBSERVE_ERROR_HINTS_ZH[code]
    return (
        f"截图环节出错（{code or 'unknown'}）。",
        ["用 /observe_status 查看详情；确认 Mac 上 Conveyor Agent 在运行"],
    )


def format_observe_failure(record: dict) -> str:
    """User-facing Chinese explanation when an observe request fails."""
    status = (record.get("status") or "failed").strip()
    req_id = record.get("request_id") or "?"
    user_request = (record.get("user_request") or "").strip()

    if status == "expired":
        lines = [
            "⏱️ 截图请求已过期",
            "",
            f"请求：{req_id}",
            "请在飞书重新发送：截图看看我电脑现在是什么",
        ]
        return _safe_truncate("\n".join(lines))

    if status == "cancelled":
        lines = [
            "🚫 截图请求已取消",
            "",
            f"请求：{req_id}",
        ]
        return _safe_truncate("\n".join(lines))

    error = (record.get("error") or "").strip()
    error_message = (record.get("error_message") or record.get("message") or "").strip()
    summary, steps = _observe_error_hint_zh(error)

    lines = [
        "❌ 截图失败",
        "",
        f"请求：{req_id}",
    ]
    if user_request:
        lines.append(f"内容：{user_request[:120]}")
    lines.append(f"原因：{summary}")
    if error_message and error_message not in summary:
        lines.append(f"详情：{error_message}")
    lines.append("")
    lines.append("你可以这样处理：")
    for step in steps:
        lines.append(f"• {step}")
    return _safe_truncate("\n".join(lines))


def format_observe_upload_failure(
    upload_id: str,
    observe_id: str,
    *,
    status: str,
    error: str | None = None,
) -> str:
    lines = [
        "❌ 缩略图发送失败",
        "",
        f"截图请求：{observe_id}",
        f"上传任务：{upload_id}",
        f"状态：{status}",
    ]
    if error:
        lines.append(f"原因：{error}")
    lines.extend([
        "",
        "你可以这样处理：",
        "• 用 /upload_status 查看上传进度",
        "• 用 /observe_upload 重新申请缩略图",
        "• 确认 Mac 上 Conveyor Agent 在线且屏幕录制权限已开启",
    ])
    return _safe_truncate("\n".join(lines))


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
        hint, _ = _observe_error_hint_zh(str(error))
        lines.append(f"  error: {error}")
        lines.append(f"  hint: {hint}")
    return lines


def format_observe_request_created(record: dict, settings: Settings) -> str:
    ttl_minutes = max(1, settings.conveyor_desktop_observe_request_ttl_seconds // 60)
    node_id = record.get("node_id") or settings.conveyor_desktop_node_id or "macbook-payton"
    auto = bool(record.get("auto_upload_thumbnail"))
    if auto:
        lines = [
            "📸 已发起截图请求",
            "",
            f"请求：{record.get('request_id', '?')}",
            f"目标 Mac：{node_id}",
            "",
            "截图完成后会把缩略图发到这里。",
            "高清原图只保存在 Mac 本地。",
        ]
    else:
        lines = [
            "📸 已创建桌面截图请求（仅元数据）",
            "",
            f"请求：{record.get('request_id', '?')}",
            f"目标 Mac：{node_id}",
            f"有效期：{ttl_minutes} 分钟",
            "",
            "Mac agent 会在本地截一张图，只回传元数据，不上传图片。",
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
    *,
    port: Any = None,
) -> str:
    """Create observe request, optionally with auto thumbnail delivery (P5.4.3).

    - Natural language capture phrases (e.g. 截图看看我电脑现在是什么) trigger preview if enabled.
    - /observe_request --preview forces auto thumbnail.
    - /observe_request --metadata-only forces metadata only.
    - Plain /observe_request follows CONVEYOR_DESKTOP_AUTO_THUMBNAIL_ON_OBSERVE.
    - If preview: polls briefly for observe complete + creates upload via ensure + polls + delivers via send_image.
    - Returns user-friendly text; does not lie about delivery. Timeout says "still processing".
    """
    import asyncio
    import re
    from desktop_observe_requests import (
        create_observe_request,
        get_observe_request,
    )
    from desktop_upload_requests import ensure_upload_request_for_observe, get_upload_request

    text = (user_request or "").strip()
    # Parse flags (support from slash arg or embedded in NL text)
    preview_flag = False
    metadata_only = False
    if re.search(r"--preview\b", text, re.IGNORECASE):
        preview_flag = True
    if re.search(r"--metadata(?:-only)?\b", text, re.IGNORECASE):
        metadata_only = True
        preview_flag = False
    # clean for record
    clean_text = re.sub(r"\s*--(?:preview|metadata(?:-only)?)\b", "", text, flags=re.IGNORECASE).strip() or text

    # Decide auto
    upload_enabled = bool(getattr(settings, "conveyor_desktop_upload_enabled", False))
    auto_on = bool(getattr(settings, "conveyor_desktop_auto_thumbnail_on_observe", True))

    # Heuristic for natural language screenshot request phrases (explicit consent)
    nl_capture_patterns = [
        r"截图看看我电脑现在是什么",
        r"帮我截一下.*mac",
        r"看一下.*(macbook|mac|电脑|屏幕)",
        r"take a screenshot",
        r"request.*screenshot",
        r"capture.*(screen|desktop|mac)",
        r"screenshot.*(my|on).* (mac|desktop|screen)",
        r"截图",
        r"截屏",
    ]
    is_nl_capture = any(re.search(p, text, re.IGNORECASE) for p in nl_capture_patterns)

    auto_upload = False
    if metadata_only:
        auto_upload = False
    elif preview_flag:
        auto_upload = upload_enabled
    elif is_nl_capture:
        auto_upload = upload_enabled and auto_on
    else:
        # plain /observe_request or other
        auto_upload = upload_enabled and auto_on

    auto_delivery = auto_upload

    result = create_observe_request(
        settings, msg, clean_text,
        auto_upload_thumbnail=auto_upload,
        auto_delivery=auto_delivery,
    )
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
    req_id = record.get("request_id")

    if not auto_upload:
        # metadata only path (or upload disabled)
        if upload_enabled is False and auto_on:
            # created but auto disabled by config
            base = format_observe_request_created(record, settings)
            return _safe_truncate(
                base + "\n\n缩略图自动回传已关闭（CONVEYOR_DESKTOP_UPLOAD_ENABLED=false）。"
            )
        return format_observe_request_created(record, settings)

    # Preview path: if upload still disabled here (race?), fallback
    if not upload_enabled:
        base = format_observe_request_created(record, settings)
        return _safe_truncate(base + "\n\n缩略图自动回传未开启（上传功能关闭）。")

    # Now attempt wait-for-complete + auto upload + deliver (Option 1)
    # Do not block forever.
    timeout = int(getattr(settings, "conveyor_desktop_auto_thumbnail_timeout_seconds", 45) or 45)
    start = time.time()
    completed_obs = None
    while time.time() - start < timeout:
        cur = get_observe_request(settings, req_id) if req_id else None
        if cur and cur.get("status") == "completed":
            completed_obs = cur
            break
        if cur and cur.get("status") in ("failed", "expired", "cancelled"):
            return format_observe_failure(cur)
        await asyncio.sleep(0.8)
    if not completed_obs:
        base = format_observe_request_created(record, settings)
        return _safe_truncate(
            base + "\n\n截图还在处理中。\n"
            "可用 /observe_status 或 /upload_status 查看进度。"
        )

    # Create (idempotent) upload request
    upl_res = ensure_upload_request_for_observe(
        settings,
        completed_obs,
        created_by_channel=msg.channel,
        created_by_chat_id=msg.chat_id,
        created_by_operator_id=msg.operator_id,
    )
    if not upl_res.get("ok"):
        base = format_observe_request_created(record, settings)
        return _safe_truncate(
            base + f"\n\n截图已完成，但缩略图上传任务未创建：{upl_res.get('error')}。"
            "\n可用 /observe_upload 重试。"
        )
    upl = upl_res.get("request") or {}
    upl_id = upl.get("upload_id")

    # Poll upload complete briefly (agent will process via its poll loop)
    upload_poll_start = time.time()
    upload_poll_timeout = min(30, max(5, timeout - int(time.time() - start)))
    completed_upl = None
    while time.time() - upload_poll_start < upload_poll_timeout:
        cur_u = get_upload_request(settings, upl_id) if upl_id else None
        if cur_u and cur_u.get("status") == "completed":
            completed_upl = cur_u
            break
        if cur_u and cur_u.get("status") in ("failed", "expired", "cancelled"):
            return format_observe_upload_failure(
                upl_id or "?",
                req_id or "?",
                status=str(cur_u.get("status") or "failed"),
                error=str(cur_u.get("error") or "") or None,
            )
        await asyncio.sleep(0.8)

    base_msg = format_observe_request_created(record, settings)

    if not completed_upl:
        # Upload request created, will be processed by agent; delivery may happen later via status or next event
        return _safe_truncate(
            base_msg + "\n\n缩略图准备好后会发到这里。"
        )

    # Try immediate delivery using helper (only to this chat)
    if port is not None:
        try:
            delivered = await deliver_completed_uploads(
                settings,
                port=port,
                msg=msg,
                only_chat_id=msg.chat_id,
                limit=1,
            )
            if delivered:
                return _safe_truncate(base_msg + "\n\n✅ 缩略图已发送。")
        except Exception:
            # delivery failure will be marked by helper; fall through to will-send
            pass

    return _safe_truncate(
        base_msg + "\n\n缩略图准备好后会发到这里。"
    )


async def exec_desktop_observe_status(settings: Settings, _arg: str) -> str:
    from desktop_screenshot import latest_screenshot_metadata
    from nodes.registry import list_nodes
    from nodes.types import NodeStatus, NodeType

    lines = [
        "Desktop Observe Status (P5.4)",
        "",
        "This command does not capture a screenshot or upload.",
        "Use /observe_request (or NL capture phrases) to create a remote observe request.",
        "Use /observe_preview or screenshot phrases (e.g. 截图看看我电脑现在是什么) for auto thumbnail.",
        "Mac agent must run: python desktop_agent.py --poll-observe",
        "P5.4 thumbnail upload available when CONVEYOR_DESKTOP_UPLOAD_ENABLED=true.",
        "Auto thumbnail on explicit request: CONVEYOR_DESKTOP_AUTO_THUMBNAIL_ON_OBSERVE (default true).",
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
        elif record.get("delivery_error"):
            lines.append("  status: delivery failed")
            lines.append(f"  error: {record['delivery_error']}")
            if record.get("delivery_failed_at"):
                lines.append(f"  failed_at: {record['delivery_failed_at']}")
        elif record.get("delivery_failed"):
            lines.append("  status: delivery failed")
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
    msg: Any = None,
) -> str:
    import logging
    from pathlib import Path
    from desktop_upload_requests import (
        load_upload_requests,
        list_recent_upload_requests,
        mark_upload_delivered,
        mark_upload_delivery_failed,
    )
    from nodes.registry import list_nodes
    from nodes.types import NodeStatus, NodeType

    logger = logging.getLogger("conveyor.observe_tools")
    temp_dir_err = upload_temp_dir_configuration_error(settings)

    if temp_dir_err is None and port is not None and msg is not None:
        store = load_upload_requests(settings)
        for upload_id, record in store.items():
            if record.get("status") == "completed" and not record.get("delivered") and not record.get("delivery_error"):
                result = record.get("result")
                if not isinstance(result, dict) or not result.get("thumbnail_path"):
                    continue
                thumbnail_path = result.get("thumbnail_path")
                if not Path(thumbnail_path).exists():
                    logger.warning(
                        "upload_status: thumbnail_missing upload_id=%s path=%s",
                        upload_id,
                        thumbnail_path,
                    )
                    mark_upload_delivery_failed(
                        settings,
                        upload_id,
                        "thumbnail_missing",
                        message="Thumbnail temp file not found on VPS.",
                    )
                    continue
                if not hasattr(port, "send_image"):
                    continue
                caption = f"Thumbnail for observe {record.get('observe_request_id')}"
                target_chat_id = record.get("created_by_chat_id") or msg.chat_id
                target_channel = record.get("created_by_channel") or msg.channel
                logger.debug(
                    "upload_status: sending upload_id=%s to channel=%s chat_id=%s...",
                    upload_id,
                    target_channel,
                    (target_chat_id or "")[:8],
                )
                try:
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
                    logger.info(
                        "upload_status: delivered upload_id=%s to channel=%s",
                        upload_id,
                        target_channel,
                    )
                except Exception as exc:
                    error_name = type(exc).__name__
                    error_msg = str(exc)[:200]
                    logger.exception(
                        "upload_status: delivery failed upload_id=%s error=%s",
                        upload_id,
                        error_name,
                    )
                    mark_upload_delivery_failed(
                        settings,
                        upload_id,
                        error_name,
                        message=error_msg,
                    )

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


async def exec_desktop_upload_resend(
    settings: Settings,
    arg: str,
    *,
    port: Any = None,
    msg: Any = None,
) -> str:
    """Resend a completed upload thumbnail to chat (P5.4.2).

    Resets delivery tracking fields, then re-attempts send_image.
    Accepts an upload_id argument. Without argument, looks for the
    most recent completed-but-undelivered or delivery-failed request.
    """
    import logging
    from pathlib import Path
    from desktop_upload_requests import (
        get_upload_request,
        list_recent_upload_requests,
        mark_upload_delivered,
        mark_upload_delivery_failed,
        reset_upload_delivery,
    )

    logger = logging.getLogger("conveyor.observe_tools")
    upload_id = (arg or "").strip()

    # Find the target record
    record = None
    if upload_id:
        record = get_upload_request(settings, upload_id)
        if not record:
            return _safe_truncate(f"Upload request not found: {upload_id}")
    else:
        # Use most recent completed request
        for r in list_recent_upload_requests(settings, limit=10):
            if r.get("status") == "completed":
                record = r
                upload_id = r.get("upload_id", "")
                break
        if not record:
            return _safe_truncate("No completed upload request found to resend.")

    if record.get("status") != "completed":
        return _safe_truncate(
            f"Upload {upload_id} is not completed (status={record.get('status')}). "
            "Only completed uploads can be resent."
        )

    result = record.get("result")
    if not isinstance(result, dict) or not result.get("thumbnail_path"):
        return _safe_truncate(
            f"Upload {upload_id}: no thumbnail path in completed result."
        )

    thumbnail_path = result["thumbnail_path"]
    if not Path(thumbnail_path).exists():
        return _safe_truncate(
            f"Upload {upload_id}: thumbnail file missing (already cleaned up?). "
            "Create a new upload request."
        )

    if port is None or not hasattr(port, "send_image"):
        return _safe_truncate(
            "Cannot resend: no outbound port with send_image available."
        )

    # Reset delivery tracking before resend
    reset_result = reset_upload_delivery(settings, upload_id)
    if not reset_result.get("ok"):
        return _safe_truncate(
            f"Reset failed: {reset_result.get('error', 'unknown')}"
        )

    if msg is None:
        return _safe_truncate("Cannot resend: no inbound message context.")

    caption = f"Thumbnail for observe {record.get('observe_request_id')}"
    target_chat_id = record.get("created_by_chat_id") or msg.chat_id
    target_channel = record.get("created_by_channel") or msg.channel
    logger.debug(
        "upload_resend: sending upload_id=%s to channel=%s chat_id=%s...",
        upload_id,
        target_channel,
        (target_chat_id or "")[:8],
    )
    try:
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
        logger.info("upload_resend: delivered upload_id=%s", upload_id)
        return _safe_truncate(
            f"✅ Thumbnail resent and marked delivered.\n"
            f"Upload: {upload_id}\n"
            f"Channel: {target_channel}\n"
            f"Run /upload_status to confirm."
        )
    except Exception as exc:
        error_name = type(exc).__name__
        error_msg = str(exc)[:200]
        logger.exception("upload_resend: delivery failed upload_id=%s", upload_id)
        mark_upload_delivery_failed(
            settings,
            upload_id,
            error_name,
            message=error_msg,
        )
        return _safe_truncate(
            f"❌ Resend failed for upload {upload_id}.\n"
            f"Error: {error_name}\n"
            f"Run /upload_status for details."
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


async def deliver_completed_uploads(
    settings: Settings,
    *,
    port: Any,
    msg: Any,
    only_chat_id: str | None = None,
    limit: int = 3,
) -> list[dict]:
    """Deliver undelivered completed thumbnail uploads to their original chat.

    - Only sends to created_by_chat_id (scoped by only_chat_id if provided).
    - Calls send_image then mark only on success; on fail marks delivery_failed.
    - Does not hold upload lock across network I/O.
    - Reuses P5.4.2 delivery correctness (no false delivered, safe paths).
    - Never sends full paths or to wrong chat.
    """
    import logging
    from pathlib import Path
    from desktop_upload_requests import (
        load_upload_requests,
        mark_upload_delivered,
        mark_upload_delivery_failed,
    )

    logger = logging.getLogger("conveyor.observe_tools")
    if port is None or not hasattr(port, "send_image"):
        return []

    store = load_upload_requests(settings)
    candidates: list[tuple[str, dict, str]] = []
    for upload_id, record in list(store.items()):
        if not isinstance(record, dict):
            continue
        if record.get("status") != "completed":
            continue
        if record.get("delivered") or record.get("delivery_failed"):
            continue
        if only_chat_id and record.get("created_by_chat_id") != only_chat_id:
            continue
        result = record.get("result")
        if not isinstance(result, dict) or not result.get("thumbnail_path"):
            continue
        thumb_path = result["thumbnail_path"]
        candidates.append((upload_id, record, thumb_path))
        if len(candidates) >= limit:
            break

    delivered: list[dict] = []
    for upload_id, record, thumb_path in candidates:
        p = Path(thumb_path)
        if not p.exists():
            mark_upload_delivery_failed(
                settings,
                upload_id,
                "thumbnail_missing",
                message="Thumbnail temp file not found on VPS.",
            )
            continue
        caption = f"Thumbnail for observe {record.get('observe_request_id')}"
        target_chat_id = record.get("created_by_chat_id") or (getattr(msg, "chat_id", None) if msg else None)
        target_channel = record.get("created_by_channel") or (getattr(msg, "channel", None) if msg else None)
        if not target_chat_id:
            continue
        logger.debug(
            "deliver_completed_uploads: sending upload_id=%s to channel=%s chat=%s",
            upload_id,
            target_channel,
            (target_chat_id or "")[:8],
        )
        try:
            await port.send_image(
                chat_id=target_chat_id,
                image_path=str(thumb_path),
                caption=caption,
            )
            mark_upload_delivered(
                settings,
                upload_id,
                channel=target_channel,
                chat_id=target_chat_id,
            )
            delivered.append(dict(record))
            logger.info("deliver_completed_uploads: delivered upload_id=%s", upload_id)
        except Exception as exc:
            error_name = type(exc).__name__
            error_msg = str(exc)[:200]
            logger.exception("deliver_completed_uploads: delivery failed upload_id=%s", upload_id)
            mark_upload_delivery_failed(
                settings,
                upload_id,
                error_name,
                message=error_msg,
            )
            # do not mark delivered; do not include in returned list
    return delivered
