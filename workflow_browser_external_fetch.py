"""Single-hop HTTPS transport: pinned public IP, strict response framing, 20s deadline.

No redirects, cookies, decompression, or follow-up connections. Hostname
remains the TLS SNI and certificate verification name, not the pinned IP.
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import socket
import ssl
import threading
import time


MAX_BODY = 1024 * 1024
FETCH_DEADLINE_SECONDS = 20


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
        self._create_connection = self._dial_pinned_ip

    def _dial_pinned_ip(self, _address, timeout, source_address=None):
        return socket.create_connection(
            (self._pinned_ip, self.port), timeout=timeout,
            source_address=source_address)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("fetch_deadline_exceeded")
    return min(8.0, remaining)


def fetch_single_https_response(host: str, port: int, ip: str,
                                path: str, method: str = "GET") -> SingleHopResponse:
    """One bounded TLS exchange, rejecting ambiguous framing before fulfillment."""
    if method not in ("GET", "HEAD"):
        raise ValueError("unsafe_http_method")
    deadline = time.monotonic() + FETCH_DEADLINE_SECONDS
    conn = _PinnedHTTPSConnection(host, port, ip)
    expired = threading.Event()
    # HTTPResponse may take ownership of its socket after getresponse(),
    # clearing conn.sock even though the response is still reading from it.
    # Hold a separate reference so the watchdog can forcibly terminate it.
    active_socket = [None]

    def stop_socket() -> None:
        expired.set()
        sock = active_socket[0] or conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            finally:
                sock.close()

    watchdog = threading.Timer(FETCH_DEADLINE_SECONDS, stop_socket)
    watchdog.daemon = True
    watchdog.start()
    try:
        authority = host if port == 443 else f"{host}:{port}"
        conn.timeout = _remaining(deadline)
        conn.request(method, path, headers={
            "Host": authority, "Accept": "text/html, text/plain;q=0.9",
            "Accept-Encoding": "identity", "Connection": "close",
        })
        if conn.sock:
            active_socket[0] = conn.sock
            conn.sock.settimeout(_remaining(deadline))
        response = conn.getresponse()
        headers = tuple((name.lower(), value) for name, value
                        in response.getheaders())
        encoding = [v.strip().lower() for k, v in headers if k == "content-encoding"]
        if encoding and encoding != ["identity"]:
            raise ValueError("encoded_response_denied")
        declared = [v.strip() for k, v in headers if k == "content-length"]
        if len(declared) > 1:
            raise ValueError("ambiguous_content_length")
        content_length = None
        if declared:
            if not declared[0].isascii() or not declared[0].isdigit():
                raise ValueError("invalid_content_length")
            content_length = int(declared[0])
            if content_length > MAX_BODY:
                raise ValueError("oversized_response")
            if any(k == "transfer-encoding" for k, _ in headers):
                raise ValueError("ambiguous_framing")
        chunks = []
        size = 0
        if method != "HEAD":
            while True:
                # read1 performs at most one underlying socket read. Unlike
                # read(n), it does not wait for all n bytes: each slow-drip
                # byte returns to us for a monotonic deadline check.
                remaining = _remaining(deadline)
                if active_socket[0]:
                    active_socket[0].settimeout(remaining)
                chunk = response.read1(min(4096, MAX_BODY + 1 - size))
                _remaining(deadline)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BODY:
                    raise ValueError("oversized_response")
                chunks.append(chunk)
            if content_length is not None and size != content_length:
                raise ValueError("content_length_mismatch")
        _remaining(deadline)
        if expired.is_set():
            raise TimeoutError("fetch_deadline_exceeded")
        return SingleHopResponse(response.status, headers, b"".join(chunks))
    except (OSError, http.client.HTTPException) as exc:
        if expired.is_set():
            raise TimeoutError("fetch_deadline_exceeded") from exc
        raise
    finally:
        watchdog.cancel()
        conn.close()
