"""gmail_imap.py — Gmail IMAP backend for Conveyor personal tools.

Uses Python stdlib imaplib/email for read-only Gmail access.
Requires GMAIL_BACKEND=imap_smtp + GMAIL_ADDRESS + GMAIL_APP_PASSWORD.
App password is NEVER exposed in outputs, logs, or errors.
"""
from __future__ import annotations

import email
import imaplib
import logging
from datetime import datetime
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any

from config import Settings
from personal_tools.base import ToolResult
from redaction import redact_text, truncate

logger = logging.getLogger(__name__)


async def gmail_status_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    return gmail_status(settings)


async def gmail_recent_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    limit = 5
    if arg.strip().isdigit():
        limit = max(1, min(20, int(arg.strip())))
    return gmail_recent(settings, limit)


async def gmail_search_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    if not arg.strip():
        return ToolResult(ok=False, text="⚠️ 用法: /gmail_search <关键词>")
    return gmail_search(settings, arg.strip())


async def gmail_read_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    if not arg.strip():
        return ToolResult(ok=False, text="⚠️ 用法: /gmail_read <邮件ID>")
    return gmail_read(settings, arg.strip())


def _check_config(settings: Settings) -> str | None:
    """Return error message if Gmail config is missing, else None."""
    if settings.gmail_backend != "imap_smtp":
        return "Gmail 后端未配置（需要 GMAIL_BACKEND=imap_smtp）"
    if not settings.gmail_address:
        return "GMAIL_ADDRESS 未设置"
    if not settings.gmail_app_password:
        return "GMAIL_APP_PASSWORD 未设置"
    return None


def _connect_imap(settings: Settings) -> imaplib.IMAP4_SSL:
    """Connect to Gmail IMAP. Raises on failure."""
    err = _check_config(settings)
    if err:
        raise RuntimeError(err)
    conn = imaplib.IMAP4_SSL(settings.gmail_imap_host, settings.gmail_imap_port)
    conn.login(settings.gmail_address, settings.gmail_app_password)
    return conn


def gmail_status(settings: Settings) -> ToolResult:
    """Report Gmail backend status without exposing credentials."""
    err = _check_config(settings)
    if err:
        return ToolResult(ok=False, text=f"⚠️ {err}")

    # Test connection
    try:
        conn = _connect_imap(settings)
        status, data = conn.status("INBOX", "(MESSAGES UNSEEN)")
        conn.logout()
        if status == "OK":
            # Parse: (b'INBOX (MESSAGES 123 UNSEEN 45)',)
            info = data[0].decode() if data else ""
            return ToolResult(ok=True, text=f"✅ Gmail 连接正常\n地址: {settings.gmail_address}\nIMAP: {settings.gmail_imap_host}:{settings.gmail_imap_port}\n{info}")
        else:
            return ToolResult(ok=False, text=f"⚠️ Gmail IMAP 状态查询失败: {status}")
    except Exception as exc:
        return ToolResult(ok=False, text=f"⚠️ Gmail 连接失败: {redact_text(str(exc))}")


def gmail_recent(settings: Settings, limit: int = 5) -> ToolResult:
    """Fetch recent emails from INBOX."""
    err = _check_config(settings)
    if err:
        return ToolResult(ok=False, text=f"⚠️ {err}")

    try:
        conn = _connect_imap(settings)
        conn.select("INBOX", readonly=True)
        status, data = conn.search(None, "ALL")
        if status != "OK":
            conn.logout()
            return ToolResult(ok=False, text="⚠️ 搜索收件箱失败")

        msg_ids = data[0].split()
        if not msg_ids:
            conn.logout()
            return ToolResult(ok=True, text="📭 收件箱为空")

        # Get last N message IDs
        recent_ids = msg_ids[-limit:]
        results = []

        for msg_id in reversed(recent_ids):
            status, msg_data = conn.fetch(msg_id, "(RFC822.HEADER)")
            if status != "OK":
                continue
            raw_header = msg_data[0][1]
            msg = email.message_from_bytes(raw_header)
            subject = _decode_header(msg.get("Subject", "(无主题)"))
            from_addr = _decode_header(msg.get("From", ""))
            date_str = msg.get("Date", "")
            try:
                date = parsedate_to_datetime(date_str).strftime("%m-%d %H:%M")
            except Exception:
                date = date_str[:16] if date_str else "?"
            results.append(f"  [{msg_id.decode()}] {date} | {truncate(subject, 60)} | {from_addr}")

        conn.logout()
        text = f"📬 最近 {len(results)} 封邮件:\n" + "\n".join(results)
        return ToolResult(ok=True, text=truncate(text))
    except Exception as exc:
        return ToolResult(ok=False, text=f"⚠️ 获取邮件失败: {redact_text(str(exc))}")


def gmail_search(settings: Settings, query: str, limit: int = 10) -> ToolResult:
    """Search emails with IMAP SEARCH."""
    err = _check_config(settings)
    if err:
        return ToolResult(ok=False, text=f"⚠️ {err}")

    if not query.strip():
        return ToolResult(ok=False, text="⚠️ 搜索关键词不能为空")

    try:
        conn = _connect_imap(settings)
        conn.select("INBOX", readonly=True)
        # IMAP SEARCH: search subject and from
        status, data = conn.search(None, f'(OR SUBJECT "{query}" FROM "{query}")')
        if status != "OK":
            # Fallback: try BODY search
            status, data = conn.search(None, f'(BODY "{query}")')
        if status != "OK":
            conn.logout()
            return ToolResult(ok=False, text="⚠️ 搜索失败")

        msg_ids = data[0].split()
        if not msg_ids:
            conn.logout()
            return ToolResult(ok=True, text=f"🔍 未找到匹配 '{query}' 的邮件")

        # Get last N results
        recent_ids = msg_ids[-limit:]
        results = []

        for msg_id in reversed(recent_ids):
            status, msg_data = conn.fetch(msg_id, "(RFC822.HEADER)")
            if status != "OK":
                continue
            raw_header = msg_data[0][1]
            msg = email.message_from_bytes(raw_header)
            subject = _decode_header(msg.get("Subject", "(无主题)"))
            from_addr = _decode_header(msg.get("From", ""))
            date_str = msg.get("Date", "")
            try:
                date = parsedate_to_datetime(date_str).strftime("%m-%d %H:%M")
            except Exception:
                date = date_str[:16] if date_str else "?"
            results.append(f"  [{msg_id.decode()}] {date} | {truncate(subject, 60)} | {from_addr}")

        conn.logout()
        text = f"🔍 搜索 '{query}' 找到 {len(results)} 封:\n" + "\n".join(results)
        return ToolResult(ok=True, text=truncate(text))
    except Exception as exc:
        return ToolResult(ok=False, text=f"⚠️ 搜索失败: {redact_text(str(exc))}")


def gmail_read(settings: Settings, message_id: str) -> ToolResult:
    """Read a specific email by message ID."""
    err = _check_config(settings)
    if err:
        return ToolResult(ok=False, text=f"⚠️ {err}")

    if not message_id.strip():
        return ToolResult(ok=False, text="⚠️ 邮件 ID 不能为空")

    try:
        msg_id_bytes = message_id.strip().encode()
        conn = _connect_imap(settings)
        conn.select("INBOX", readonly=True)
        status, msg_data = conn.fetch(msg_id_bytes, "(RFC822)")
        conn.logout()

        if status != "OK" or not msg_data or not msg_data[0]:
            return ToolResult(ok=False, text=f"⚠️ 未找到邮件 #{message_id}")

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject = _decode_header(msg.get("Subject", "(无主题)"))
        from_addr = _decode_header(msg.get("From", ""))
        to_addr = _decode_header(msg.get("To", ""))
        date_str = msg.get("Date", "")
        try:
            date = parsedate_to_datetime(date_str).strftime("%Y-%m-%d %H:%M")
        except Exception:
            date = date_str

        # Get body (text/plain preferred)
        body = _extract_body(msg)

        lines = [
            f"📧 邮件 #{message_id}",
            f"From: {from_addr}",
            f"To: {to_addr}",
            f"Date: {date}",
            f"Subject: {subject}",
            "",
            truncate(body, 3000),
        ]
        return ToolResult(ok=True, text=truncate("\n".join(lines)))
    except Exception as exc:
        return ToolResult(ok=False, text=f"⚠️ 读取邮件失败: {redact_text(str(exc))}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _decode_header(header_value: str) -> str:
    """Decode RFC 2047 encoded header."""
    if not header_value:
        return ""
    parts = decode_header(header_value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _extract_body(msg: Message) -> str:
    """Extract plain text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback to text/html
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return f"[HTML 邮件]\n{payload.decode(charset, errors='replace')[:2000]}"
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return "(无法解析邮件内容)"
