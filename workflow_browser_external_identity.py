"""Strict HTTPS site identity and DNS pinning for read-only browsing."""
from __future__ import annotations
import ipaddress
import re
import socket
from urllib.parse import urlsplit

HOST_RE = re.compile(r"(?=.{1,253}$)[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?)+\Z")
PATH_RE = re.compile(r"/[a-zA-Z0-9/_\-.]*\Z")

class ExternalPolicyDenied(RuntimeError):
    """Expose only fixed policy codes, never a URL or request data."""

def canonical_host(host: str) -> str:
    if (not isinstance(host, str) or not host.isascii()
            or not HOST_RE.fullmatch(host) or host != host.lower()
            or host.rsplit(".", 1)[-1].isdigit()):
        raise ExternalPolicyDenied("host_denied")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ExternalPolicyDenied("ip_literal_denied")

def canonical_url(url: str) -> tuple[str, int, str]:
    if not isinstance(url, str) or any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise ExternalPolicyDenied("url_denied")
    try:
        parsed = urlsplit(url)
        host = canonical_host(parsed.hostname or "")
        port = parsed.port or 443
    except (ValueError, ExternalPolicyDenied) as exc:
        raise ExternalPolicyDenied("url_denied") from exc
    authority = host if port == 443 else f"{host}:{port}"
    path = parsed.path or "/"
    if (parsed.scheme != "https" or parsed.netloc != authority or
            not 1 <= port <= 65535 or parsed.query or parsed.fragment or
            "\\" in url or not PATH_RE.fullmatch(path) or
            "//" in path or ".." in path.split("/")):
        raise ExternalPolicyDenied("url_denied")
    return host, port, path

def resolve_public_ipv4(host: str, port: int) -> str:
    """Resolve once, reject mixed internal answers, and pin a public IPv4."""
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM,
                                        proto=socket.IPPROTO_TCP)
        ips = {ipaddress.ip_address(a[4][0]) for a in addresses}
    except (OSError, ValueError, IndexError) as exc:
        raise ExternalPolicyDenied("dns_resolution_denied") from exc
    if not ips or not all(ip.is_global for ip in ips):
        raise ExternalPolicyDenied("dns_private_address_denied")
    ipv4 = sorted(str(ip) for ip in ips if isinstance(ip, ipaddress.IPv4Address))
    if not ipv4:
        raise ExternalPolicyDenied("dns_ipv4_required")
    return ipv4[0]
