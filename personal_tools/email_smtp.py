"""email_smtp.py — SMTP email sending for Conveyor personal tools.

Uses Python stdlib smtplib with STARTTLS on port 587.
Requires GMAIL_BACKEND=imap_smtp + GMAIL_ADDRESS + GMAIL_APP_PASSWORD.
Sending requires WRITE confirmation through the tool registry.
App password is NEVER exposed in outputs, logs, or errors.
"""
from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate

from config import Settings
from personal_tools.base import ToolResult
from redaction import redact_text, truncate

logger = logging.getLogger(__name__)


async def email_send_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    """Adapter for email.send tool. Parses pipe-separated args."""
    parsed = parse_email_send_args(arg)
    if parsed is None:
        return ToolResult(
            ok=False,
            text="⚠️ 用法: /email_send <收件人> | <主题> | <正文>\n示例: /email_send test@example.com | 测试主题 | 测试内容"
        )
    to, subject, body = parsed
    return send_email(settings, to, subject, body)


def _check_config(settings: Settings) -> str | None:
    """Return error message if Gmail config is missing, else None."""
    if settings.gmail_backend != "imap_smtp":
        return "Gmail 后端未配置（需要 GMAIL_BACKEND=imap_smtp）"
    if not settings.gmail_address:
        return "GMAIL_ADDRESS 未设置"
    if not settings.gmail_app_password:
        return "GMAIL_APP_PASSWORD 未设置"
    return None


def build_email_preview(to: str, subject: str, body: str) -> str:
    """Build a preview of the email for confirmation."""
    lines = [
        "📧 邮件发送预览",
        "",
        f"To: {to}",
        f"Subject: {subject}",
        f"Date: {formatdate(localtime=True)}",
        "",
        "Body:",
        truncate(body, 500),
    ]
    return "\n".join(lines)


def send_email(settings: Settings, to: str, subject: str, body: str) -> ToolResult:
    """Send a plain-text email via SMTP with STARTTLS.

    This function should ONLY be called through the WRITE confirmation flow.
    """
    err = _check_config(settings)
    if err:
        return ToolResult(ok=False, text=f"⚠️ {err}")

    if not to.strip():
        return ToolResult(ok=False, text="⚠️ 收件人不能为空")
    if not subject.strip():
        return ToolResult(ok=False, text="⚠️ 主题不能为空")

    try:
        # Build message
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = settings.gmail_address
        msg["To"] = to.strip()
        msg["Subject"] = subject.strip()
        msg["Date"] = formatdate(localtime=True)

        # Send via SMTP with STARTTLS
        with smtplib.SMTP(settings.gmail_smtp_host, settings.gmail_smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(settings.gmail_address, settings.gmail_app_password)
            server.send_message(msg)

        return ToolResult(ok=True, text=f"✅ 邮件已发送\nTo: {to}\nSubject: {subject}")
    except smtplib.SMTPAuthenticationError:
        return ToolResult(ok=False, text="⚠️ Gmail 认证失败，请检查 App Password")
    except smtplib.SMTPRecipientsRefused:
        return ToolResult(ok=False, text=f"⚠️ 收件人被拒绝: {to}")
    except Exception as exc:
        return ToolResult(ok=False, text=f"⚠️ 发送失败: {redact_text(str(exc))}")


def parse_email_send_args(arg: str) -> tuple[str, str, str] | None:
    """Parse pipe-separated email args: to | subject | body.

    Returns (to, subject, body) or None if parsing fails.
    """
    parts = [p.strip() for p in arg.split("|")]
    if len(parts) < 3:
        return None
    to, subject = parts[0], parts[1]
    body = "|".join(parts[2:])  # Body may contain pipes
    if not to or not subject:
        return None
    return to, subject, body
