"""personal_tools/web_fetch.py — Web Fetch MVP for Conveyor (P4.1 Phase A).

READ-only curl wrapper with strict URL validation.
Rejects private IPs, localhost, metadata endpoints, non-http(s) schemes.
All output passes redact_text + truncate.
"""
from __future__ import annotations

import ipaddress
import logging
import shutil
import socket
import subprocess
from urllib.parse import urlparse

from config import Settings
from personal_tools.base import ToolResult
from redaction import redact_text, truncate

logger = logging.getLogger(__name__)

# Blocked IP ranges
_BLOCKED_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)

_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "metadata.google.internal",
    "instance-data",
})


def validate_url(url: str) -> tuple[bool, str]:
    """Validate URL for safe fetching. Returns (ok, error_msg)."""
    if not url or not isinstance(url, str):
        return False, "URL 不能为空"

    url = url.strip()

    try:
        parsed = urlparse(url)
    except Exception:
        return False, "URL 格式无效"

    # Only http/https
    if parsed.scheme not in ("http", "https"):
        return False, f"不支持的协议: {parsed.scheme}（仅支持 http/https）"

    hostname = parsed.hostname
    if not hostname:
        return False, "URL 缺少主机名"

    # Blocked hostnames
    if hostname.lower() in _BLOCKED_HOSTNAMES:
        return False, f"拒绝访问: {hostname}"

    # Resolve and check IP
    try:
        addrinfos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"无法解析主机名: {hostname}"

    for family, _, _, _, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue

        for net in _BLOCKED_NETWORKS:
            if ip in net:
                return False, f"拒绝访问: {hostname} 解析到私有 IP {ip}"

    return True, ""


def _curl_argv(url: str, settings: Settings, *, headers_only: bool = False) -> list[str]:
    """Build safe curl argv. Always uses shell=False."""
    argv = [
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--max-redirs", str(settings.web_fetch_max_redirects),
        "--connect-timeout", "5",
        "--max-time", str(settings.web_fetch_timeout_seconds),
        "--max-filesize", str(settings.web_fetch_max_bytes),
        "--proto", "=http,https",
        "--proto-redir", "=http,https",
        "--user-agent", settings.web_user_agent,
    ]
    if headers_only:
        argv.append("--head")
    argv.append(url)
    return argv


def _run_curl(argv: list[str]) -> tuple[int, str, str]:
    """Run curl with shell=False. Returns (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            shell=False,
            timeout=30,
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        return -1, "", "curl 未安装"
    except subprocess.TimeoutExpired:
        return -1, "", "curl 超时"
    except Exception as exc:
        return -1, "", str(exc)


def fetch_headers(settings: Settings, url: str) -> ToolResult:
    """Fetch HTTP headers for a URL."""
    if not settings.web_fetch_enabled:
        return ToolResult(ok=False, text="⚠️ Web Fetch 已禁用")

    ok, err = validate_url(url)
    if not ok:
        return ToolResult(ok=False, text=f"⚠️ URL 验证失败: {err}")

    argv = _curl_argv(url, settings, headers_only=True)
    rc, stdout, stderr = _run_curl(argv)
    if rc != 0:
        return ToolResult(ok=False, text=f"⚠️ 获取 headers 失败: {redact_text(stderr)}")

    return ToolResult(ok=True, text=truncate(redact_text(stdout)))


def fetch_text(settings: Settings, url: str) -> ToolResult:
    """Fetch URL content as text."""
    if not settings.web_fetch_enabled:
        return ToolResult(ok=False, text="⚠️ Web Fetch 已禁用")

    ok, err = validate_url(url)
    if not ok:
        return ToolResult(ok=False, text=f"⚠️ URL 验证失败: {err}")

    argv = _curl_argv(url, settings, headers_only=False)
    rc, stdout, stderr = _run_curl(argv)
    if rc != 0:
        return ToolResult(ok=False, text=f"⚠️ 获取内容失败: {redact_text(stderr)}")

    # Check content type via headers (lightweight)
    text = html_to_text(stdout)
    return ToolResult(ok=True, text=truncate(redact_text(text)))


def html_to_text(html: str) -> str:
    """Strip HTML tags and normalize whitespace. Simple regex approach."""
    import re
    # Remove script/style blocks
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Replace br/p/div/li with newlines
    text = re.sub(r'<(br|p|div|li|h[1-6])[^>]*/?>', '\n', text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r'<[^>]+>', '', text)
    # Decode common entities
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    # Normalize whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()


# --- Adapters for personal_tools/registry.py ---

async def web_fetch_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    return fetch_text(settings, arg)


async def web_text_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    return fetch_text(settings, arg)


async def web_headers_adapter(settings: Settings, arg: str, **kw) -> ToolResult:
    return fetch_headers(settings, arg)
