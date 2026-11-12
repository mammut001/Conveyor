#!/usr/bin/env python3
"""Tailnet-only zero-prompt entrance to Conveyor Web Workbench (127.0.0.1:18787).

Listens on 127.0.0.1:18788 and is exposed to the tailnet via `tailscale serve --tcp 18787`.
The browser's Host is forwarded unchanged so the app's Origin check still matches.
X-Forwarded-Host is never used as that authority.
"""
from __future__ import annotations

import http.client
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 18787
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 18788
ENV_FILE = "/opt/conveyor/.env"
_HOST_UNSAFE = ("\r", "\n", "\x00", " ", "\t")


def load_token() -> str:
    try:
        with open(ENV_FILE, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("CONVEYOR_WEB_TOKEN="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def request_authority(host_header: str | None, fallback: str) -> str:
    """Keep the caller's Host so Origin and Host name the same site.

    Absent, empty, or unsafe values fall back to ``fallback`` (the local
    upstream). This function does not read X-Forwarded-Host.
    """
    if not isinstance(host_header, str):
        return fallback
    host = host_header.strip()
    if not host or len(host) > 255 or any(ch in host for ch in _HOST_UNSAFE):
        return fallback
    return host


class PortalProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _serve_boot_js(self, token: str) -> None:
        js = f"""(function(){{
  try {{
    var t = {json.dumps(token)};
    if (t) {{
      sessionStorage.setItem('conveyor-token', t);
      localStorage.setItem('conveyor-token', t);
    }}
    var params = new URLSearchParams(window.location.search);
    var agent = params.get('agent');
    if (agent) {{
      localStorage.setItem('conveyor-agent', agent);
    }}
    var sess = params.get('session');
    if (sess) {{
      sessionStorage.setItem('conveyor-selected-session', sess);
    }}
    function syncHashTab() {{
      var h = (window.location.hash || '').replace(/^#/, '').toLowerCase();
      if (!h) return;
      var labels = {{
        tasks: 'Tasks',
        chat: 'Chat',
        inbox: 'Inbox',
        memory: 'Memory',
        approvals: 'Approvals',
        browser: 'Browser sites',
        skills: 'Skills',
        workflows: 'Workflows',
        connectors: 'Connectors',
        secrets: 'Secrets',
        teammate: 'Teammate'
      }};
      var target = labels[h];
      if (!target) return;
      var tries = 0;
      var timer = setInterval(function() {{
        tries++;
        var btns = document.querySelectorAll('.mode-switch button');
        for (var i = 0; i < btns.length; i++) {{
          var txt = (btns[i].textContent || '').trim();
          if (txt === target || txt.indexOf(target + ' (') === 0) {{
            btns[i].click();
            clearInterval(timer);
            return;
          }}
        }}
        if (tries > 25) clearInterval(timer);
      }}, 120);
    }}
    window.addEventListener('DOMContentLoaded', syncHashTab);
    window.addEventListener('hashchange', syncHashTab);
    setTimeout(syncHashTab, 300);
  }} catch (e) {{}}
}})();
""".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(js)))
        self.end_headers()
        self.wfile.write(js)

    def _proxy(self) -> None:
        token = load_token()
        parsed = urlparse(self.path)
        if self.command == "GET" and parsed.path == "/conveyor-tailnet-boot.js":
            self._serve_boot_js(token)
            return

        content_len = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(content_len) if content_len > 0 else None

        fwd_headers = {}
        for k, v in self.headers.items():
            if k.lower() in ("host", "connection", "accept-encoding"):
                continue
            fwd_headers[k] = v
        # Preserve the authority the browser used. Rewriting Host to the
        # loopback upstream makes Origin fail the app's same-site check.
        fwd_headers["Host"] = request_authority(
            self.headers.get("Host"),
            f"{UPSTREAM_HOST}:{UPSTREAM_PORT}",
        )
        fwd_headers["Accept-Encoding"] = "identity"
        if token and "Authorization" not in fwd_headers and "authorization" not in fwd_headers:
            fwd_headers["Authorization"] = f"Bearer {token}"

        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=60)
        try:
            conn.request(self.command, self.path, body=body, headers=fwd_headers)
            resp = conn.getresponse()
            ctype = (resp.getheader("Content-Type") or "").lower()

            if "text/event-stream" in ctype:
                self.send_response(resp.status)
                for hk, hv in resp.getheaders():
                    if hk.lower() in ("transfer-encoding", "connection", "content-length"):
                        continue
                    self.send_header(hk, hv)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                while True:
                    chunk = resp.read(1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                return

            data = resp.read()
            if self.command == "GET" and "text/html" in ctype and b'<script type="module"' in data:
                inject = b'<script src="/conveyor-tailnet-boot.js"></script>\n    <script type="module"'
                data = data.replace(b'<script type="module"', inject, 1)

            self.send_response(resp.status)
            has_cache_control = False
            for hk, hv in resp.getheaders():
                if hk.lower() in ("content-length", "transfer-encoding", "connection"):
                    continue
                if hk.lower() == "cache-control":
                    has_cache_control = True
                self.send_header(hk, hv)
            self.send_header("Content-Length", str(len(data)))
            if not has_cache_control:
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        finally:
            conn.close()

    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def do_HEAD(self):
        self._proxy()


if __name__ == "__main__":
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), PortalProxyHandler).serve_forever()
