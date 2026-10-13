"""Opt-in loopback HTTP forward / HTTPS CONNECT proxy. Never inspects TLS bodies."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit

_LOCK = threading.RLock()
_CURRENT = None


def enabled(settings=None):
    if settings is not None and hasattr(settings, "egress_proxy_enabled"):
        return bool(settings.egress_proxy_enabled)
    return os.getenv("CONVEYOR_EGRESS_PROXY", "").lower() in ("1", "true", "yes", "on")


def allowlist(settings=None):
    raw = getattr(settings, "egress_allowlist", None)
    if raw is None:
        raw = os.getenv("CONVEYOR_EGRESS_ALLOWLIST", "")
    return tuple(sorted({s.strip().lower().rstrip(".") for s in str(raw).split(",") if s.strip()}))


def _canonical(host):
    if not host or len(host) > 253 or any(c in host for c in "/\\@% \r\n\t"):
        raise ValueError("invalid_host")
    try:
        return str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        pass
    name = host.rstrip(".").encode("idna").decode("ascii").lower()
    if not name or any(not label or len(label) > 63 for label in name.split(".")):
        raise ValueError("invalid_host")
    return name


def _match(host, rules):
    # A wildcard matches subdomains only, not the bare suffix.
    exact = host in rules
    wildcard = any(rule.startswith("*.") and host.endswith(rule[1:])
                   and host != rule[2:] for rule in rules)
    return exact, (exact or wildcard)


def destination(host, port, rules, resolver=socket.getaddrinfo):
    """Return (IP, reason). Resolve once and connect ONLY to the validated IP."""
    try:
        host = _canonical(host)
        if not (1 <= int(port) <= 65535):
            return None, "invalid_port"
        exact, permitted = _match(host, rules)
        if not permitted:
            return None, "not_allowlisted"
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if not literal.is_global and not exact:
                return None, "private_ip"
            return str(literal), "explicit_local" if not literal.is_global else "allowlisted"
        addresses = {str(ipaddress.ip_address(row[4][0])) for row in
                     resolver(host, int(port), type=socket.SOCK_STREAM)}
        if not addresses:
            return None, "dns_empty"
        # A wildcard never authorizes private DNS answers. Check ALL answers
        # (not just the one connected to) to avoid rebinding/mixed records.
        if any(not ipaddress.ip_address(ip).is_global for ip in addresses) and not exact:
            return None, "private_dns"
        return sorted(addresses)[0], "explicit_local" if any(
            not ipaddress.ip_address(ip).is_global for ip in addresses) else "allowlisted"
    except (OSError, ValueError, UnicodeError, IndexError):
        return None, "dns_or_host_error"


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Handler(socketserver.StreamRequestHandler):
    timeout = 30

    def _decision(self, host, port, ip, reason):
        record = {"time": datetime.now(timezone.utc).isoformat(),
                  "host": str(host)[:253].replace("\n", ""),
                  "port": port, "decision": "allow" if ip else "deny",
                  "reason": reason}
        server = self.server
        with server.log_lock:
            server.log_path.parent.mkdir(parents=True, exist_ok=True)
            with server.log_path.open("a", encoding="utf-8") as stream:
                os.chmod(server.log_path, 0o600)
                stream.write(json.dumps(record, sort_keys=True) + "\n")

    def _error(self, code, message):
        self.wfile.write(f"HTTP/1.1 {code} {message}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
        self.wfile.flush()

    def _headers(self):
        result = []
        total = 0
        for _ in range(100):
            line = self.rfile.readline(8193)
            total += len(line)
            if not line or len(line) > 8192 or total > 32768:
                raise ValueError("headers_too_large")
            if line in (b"\r\n", b"\n"):
                return result
            if b":" not in line or not line.endswith(b"\n"):
                raise ValueError("malformed_header")
            result.append(line)
        raise ValueError("headers_too_large")

    @staticmethod
    def _copy(src, dst, count):
        while count:
            data = src.read(min(65536, count))
            if not data:
                raise ConnectionError("truncated_body")
            dst.sendall(data)
            count -= len(data)

    def _body(self, headers, remote):
        values = {}
        for line in headers:
            key, value = line.split(b":", 1)
            values.setdefault(key.strip().lower(), []).append(value.strip().lower())
        if b"transfer-encoding" in values:
            if values[b"transfer-encoding"] != [b"chunked"] or b"content-length" in values:
                raise ValueError("ambiguous_body")
            while True:
                line = self.rfile.readline(128)
                if not line.endswith(b"\n"):
                    raise ValueError("invalid_chunk")
                n = int(line.split(b";", 1)[0].strip(), 16)
                if n > 8 * 1024 * 1024:
                    raise ValueError("chunk_too_large")
                remote.sendall(line)
                self._copy(self.rfile, remote, n + 2)
                if n == 0:
                    # The terminating CRLF above consumed only the empty trailer.
                    break
        elif b"content-length" in values:
            if len(values[b"content-length"]) != 1:
                raise ValueError("ambiguous_body")
            n = int(values[b"content-length"][0])
            if n < 0 or n > 8 * 1024 * 1024:
                raise ValueError("invalid_length")
            self._copy(self.rfile, remote, n)

    def _tunnel(self, remote):
        # Connection is unidirectional at each select iteration; neither body
        # nor TLS payload is inspected or written to the decision log.
        peers = (self.connection, remote)
        for sock in peers:
            sock.setblocking(False)
        while True:
            readable, _, _ = select.select(peers, [], [], 30)
            if not readable:
                return
            for src in readable:
                try:
                    chunk = src.recv(65536)
                except (BlockingIOError, OSError):
                    return
                if not chunk:
                    return
                dst = remote if src is self.connection else self.connection
                dst.sendall(chunk)

    def handle(self):
        host, port = "", 0
        try:
            first = self.rfile.readline(8193)
            if not first or len(first) > 8192:
                raise ValueError("invalid_request")
            parts = first.decode("ascii").strip().split(" ")
            if len(parts) != 3 or parts[2] not in ("HTTP/1.0", "HTTP/1.1"):
                raise ValueError("invalid_request")
            method, target, version = parts
            headers = self._headers()
            if method.upper() == "CONNECT":
                if target.count(":") != 1 and not target.startswith("["):
                    raise ValueError("invalid_connect_target")
                parsed = urlsplit("//" + target)
                host, port = parsed.hostname or "", parsed.port or 443
                if parsed.username or parsed.password or parsed.path or parsed.query:
                    raise ValueError("invalid_connect_target")
            else:
                parsed = urlsplit(target)
                if parsed.scheme.lower() != "http" or not parsed.hostname:
                    raise ValueError("invalid_http_target")
                if parsed.username or parsed.password or parsed.fragment:
                    raise ValueError("invalid_http_target")
                host, port = parsed.hostname, parsed.port or 80
                host_headers = [line.split(b":", 1)[1].strip().decode("ascii")
                                for line in headers if line.lower().startswith(b"host:")]
                if len(host_headers) != 1 or _canonical(urlsplit("//" + host_headers[0]).hostname) != _canonical(host):
                    raise ValueError("host_mismatch")
            host = _canonical(host)
            ip, reason = destination(host, port, self.server.rules)
            self._decision(host, port, ip, reason)
            if ip is None:
                self._error(403, "Forbidden")
                return
            with socket.create_connection((ip, port), timeout=10) as remote:
                remote.settimeout(30)
                if method.upper() == "CONNECT":
                    self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    self.wfile.flush()
                    self._tunnel(remote)
                    return
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                remote.sendall(f"{method} {path} {version}\r\n".encode("ascii"))
                for line in headers:
                    if line.split(b":", 1)[0].strip().lower() in (
                        b"proxy-authorization", b"proxy-connection", b"connection"):
                        continue
                    remote.sendall(line)
                remote.sendall(b"Connection: close\r\n\r\n")
                self._body(headers, remote)
                while True:
                    data = remote.recv(65536)
                    if not data:
                        break
                    self.wfile.write(data)
                self.wfile.flush()
        except (OSError, ValueError, UnicodeError, ConnectionError) as exc:
            # Do not include request data or exception text in logs.
            if not getattr(self, "_logged", False):
                try:
                    self._decision(host, port, None, "malformed_or_upstream_error")
                except OSError:
                    pass
            try:
                self._error(400, "Bad Request")
            except OSError:
                pass


class EgressProxy:
    def __init__(self, rules, log_path, port=0):
        self.rules = tuple(rules)
        self.log_path = Path(log_path)
        self.server = _Server(("127.0.0.1", int(port)), _Handler)
        self.server.rules = self.rules
        self.server.log_path = self.log_path
        self.server.log_lock = threading.Lock()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def ensure_proxy(settings=None):
    """Start one local proxy per process; used only when the flag is enabled."""
    global _CURRENT
    if not enabled(settings):
        return None
    rules = allowlist(settings)
    root = Path(getattr(settings, "codex_memory_root", os.getenv("CODEX_MEMORY_ROOT", ".")))
    logfile = root / "egress_decisions.jsonl"
    port = int(getattr(settings, "egress_proxy_port", 0))
    with _LOCK:
        if _CURRENT is not None:
            if _CURRENT.rules == rules and _CURRENT.log_path == logfile:
                return _CURRENT.url
            _CURRENT.close()
        _CURRENT = EgressProxy(rules, logfile, port)
        return _CURRENT.url


def proxy_env(env, settings=None):
    """Copy child environment and force proxy variables (no ambient NO_PROXY)."""
    if not enabled(settings):
        return env
    url = ensure_proxy(settings)
    result = dict(os.environ if env is None else env)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        result[name] = url
    for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
        result.pop(name, None)
    return result


def status(settings=None, limit=40):
    rules = allowlist(settings)
    root = Path(getattr(settings, "codex_memory_root", os.getenv("CODEX_MEMORY_ROOT", ".")))
    logfile = root / "egress_decisions.jsonl"
    denied = deque(maxlen=max(1, min(int(limit), 100)))
    try:
        with logfile.open(encoding="utf-8") as stream:
            for line in stream:
                if len(line) > 4096:
                    continue
                try:
                    row = json.loads(line)
                    if row.get("decision") == "deny":
                        denied.append(row)
                except (ValueError, TypeError):
                    pass
    except FileNotFoundError:
        pass
    return {"enabled": enabled(settings), "allowlist": list(rules),
            "recent_denials": list(denied)}
