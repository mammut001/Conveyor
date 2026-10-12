"""One-hop HTTPS transport with literal public-IP pinning and strict TLS SNI.

Chromium's host-resolver-rules do not apply to Playwright Route.fetch(), which
uses its own request stack and could independently re-resolve/rebind the host.
For G1 we fetch a single response over an explicitly pinned TCP socket then
fulfill the browser route only after inspecting it. Redirects are NEVER followed.
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import socket
import ssl


@dataclass(frozen=True)
class SingleHopResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, port: int, ip: str):
        super().__init__(hostname, port=port, timeout=8,
                         context=ssl.create_default_context())
        self._pinned_ip = ip
        # The hostname remains unchanged for certificate validation and SNI.
        self._create_connection = self._dial_pinned_ip

    def _dial_pinned_ip(self, _address, timeout, source_address=None):
        return socket.create_connection(
            (self._pinned_ip, self.port), timeout=timeout,
            source_address=source_address)


def fetch_single_https_response(host: str, port: int, ip: str,
                                path: str, method: str = "GET") -> SingleHopResponse:
    """Only a single reviewed path; no cookies, auth, redirects or disk state."""
    if method not in ("GET", "HEAD"):
        raise ValueError("unsafe_http_method")
    conn = _PinnedHTTPSConnection(host, port, ip)
    try:
        authority = host if port == 443 else f"{host}:{port}"
        conn.request(method, path, headers={
            "Host": authority, "Accept": "text/html, text/plain;q=0.9",
            "Connection": "close",
        })
        response = conn.getresponse()
        headers = tuple((name.lower(), value) for name, value
                        in response.getheaders())
        # Always bound the response size, including untrusted oversized bodies.
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError("oversized_response")
        return SingleHopResponse(response.status, headers, body)
    finally:
        conn.close()
