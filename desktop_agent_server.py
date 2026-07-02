"""desktop_agent_server.py — minimal HTTP server for Conveyor control plane."""
from __future__ import annotations

import hmac
import json
import logging
import os
import sys
import time
import socketserver
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

from config import load_settings
from desktop_observe_requests import (
    claim_observe_request,
    complete_observe_request,
    fail_observe_request,
    list_pending_observe_requests,
)
from nodes.state import register_desktop_node, record_heartbeat
from nodes.registry import list_nodes


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("desktop_agent_server")

# Load settings once at startup
try:
    settings = load_settings()
except Exception as e:
    logger.exception("Failed to load settings")
    sys.exit(1)

# Check config constraints
if settings.conveyor_desktop_node_enabled:
    token = settings.conveyor_desktop_agent_token
    if not token or not token.strip():
        sys.exit("Configuration error: CONVEYOR_DESKTOP_AGENT_TOKEN must not be empty when desktop node is enabled.")


MAX_BODY_BYTES = 16 * 1024


class DesktopAgentHTTPHandler(BaseHTTPRequestHandler):
    def send_json(self, status: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authenticate(self) -> bool:
        """Authenticate request. Returns True if authorized, False otherwise."""
        auth_header = self.headers.get("Authorization", "")
        expected_token = settings.conveyor_desktop_agent_token
        if not auth_header.startswith("Bearer "):
            self.send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Unauthorized: Invalid or missing token"})
            return False

        provided = auth_header[7:].strip()
        if not expected_token or not hmac.compare_digest(provided, expected_token):
            self.send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Unauthorized: Invalid or missing token"})
            return False

        return True

    def check_enabled(self) -> bool:
        """Checks if desktop node is enabled. Returns True if enabled, False otherwise."""
        if not settings.conveyor_desktop_node_enabled:
            self.send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Forbidden: Desktop node is disabled."})
            return False
        return True

    def _read_json_body(self) -> dict | None:
        content_length_str = self.headers.get("Content-Length")
        if content_length_str is None:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing Content-Length header"})
            return None

        try:
            content_length = int(content_length_str)
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid Content-Length header"})
            return None

        if content_length < 0:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Negative Content-Length"})
            return None

        if content_length > MAX_BODY_BYTES:
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "Payload Too Large"})
            return None

        post_data = self.rfile.read(content_length)
        try:
            body = json.loads(post_data)
        except Exception:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid JSON body"})
            return None
        if not isinstance(body, dict):
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "JSON body must be an object"})
            return None
        return body

    def _validate_node_id(self, node_id: object) -> str | None:
        expected_node_id = settings.conveyor_desktop_node_id or "macbook-payton"
        if not isinstance(node_id, str) or not node_id.strip():
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid or missing node_id"})
            return None
        if node_id != expected_node_id:
            self.send_json(HTTPStatus.BAD_REQUEST, {
                "ok": False,
                "error": "node_id mismatch",
                "expected_node_id": expected_node_id,
            })
            return None
        return node_id

    def do_POST(self):
        if not self.check_enabled():
            return
        if not self.authenticate():
            return

        body = self._read_json_body()
        if body is None:
            return

        expected_node_id = settings.conveyor_desktop_node_id or "macbook-payton"

        if self.path == "/desktop/register":
            node_id = body.get("node_id")
            display_name = body.get("display_name")
            agent_version = body.get("agent_version")
            host = body.get("host")

            if not isinstance(node_id, str) or not node_id.strip():
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid or missing node_id"})
                return

            if node_id != expected_node_id:
                self.send_json(HTTPStatus.BAD_REQUEST, {
                    "ok": False,
                    "error": "node_id mismatch",
                    "expected_node_id": expected_node_id
                })
                return

            if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 100:
                self.send_json(HTTPStatus.BAD_REQUEST, {
                    "ok": False, 
                    "error": "Invalid display_name: must be a non-empty string <= 100 chars"
                })
                return

            if not isinstance(agent_version, str) or not agent_version.strip() or len(agent_version) > 64:
                self.send_json(HTTPStatus.BAD_REQUEST, {
                    "ok": False, 
                    "error": "Invalid agent_version: must be a non-empty string <= 64 chars"
                })
                return

            if not isinstance(host, dict):
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid host details: must be a dictionary"})
                return

            if len(host) > 10:
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Host details dictionary too large"})
                return

            sanitized_host = {}
            for k in ["platform", "hostname", "arch"]:
                v = host.get(k)
                if v is not None:
                    if not isinstance(v, str):
                        self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"Host field {k} must be a string"})
                        return
                    sanitized_host[k] = v[:128]

            register_desktop_node(settings, node_id, display_name, agent_version, sanitized_host)
            self.send_json(HTTPStatus.OK, {
                "ok": True,
                "node_id": node_id,
                "status": "online",
                "heartbeat_interval_seconds": 30,
                "capabilities": ["screen.screenshot", "desktop.observe", "computer_use.stub"]
            })

        elif self.path == "/desktop/heartbeat":
            node_id = body.get("node_id")
            agent_state = body.get("agent_state")
            last_action = body.get("last_action")

            if not isinstance(node_id, str) or not node_id.strip():
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid or missing node_id"})
                return

            if node_id != expected_node_id:
                self.send_json(HTTPStatus.BAD_REQUEST, {
                    "ok": False,
                    "error": "node_id mismatch",
                    "expected_node_id": expected_node_id
                })
                return

            if not isinstance(agent_state, str) or not agent_state.strip() or len(agent_state) > 64:
                self.send_json(HTTPStatus.BAD_REQUEST, {
                    "ok": False, 
                    "error": "Invalid agent_state: must be a non-empty string <= 64 chars"
                })
                return

            if last_action is not None:
                if not isinstance(last_action, str) or len(last_action) > 128:
                    self.send_json(HTTPStatus.BAD_REQUEST, {
                        "ok": False, 
                        "error": "Invalid last_action: must be a string <= 128 chars"
                    })
                    return

            node_info = record_heartbeat(settings, node_id, agent_state, last_action)

            if node_info is None:
                self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Node {node_id} not registered"})
                return

            self.send_json(HTTPStatus.OK, {
                "ok": True,
                "node_id": node_id,
                "status": "online",
                "server_time": int(time.time())
            })

        elif self.path == "/desktop/observe/claim":
            node_id = self._validate_node_id(body.get("node_id"))
            if node_id is None:
                return
            request_id = body.get("request_id")
            if not isinstance(request_id, str) or not request_id.strip():
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid or missing request_id"})
                return
            result = claim_observe_request(settings, request_id.strip(), node_id)
            status = HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT
            self.send_json(status, result)

        elif self.path == "/desktop/observe/complete":
            node_id = self._validate_node_id(body.get("node_id"))
            if node_id is None:
                return
            request_id = body.get("request_id")
            if not isinstance(request_id, str) or not request_id.strip():
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid or missing request_id"})
                return
            observe_result = body.get("result")
            if not isinstance(observe_result, dict):
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid result: must be an object"})
                return
            result = complete_observe_request(
                settings, request_id.strip(), node_id, observe_result,
            )
            status = HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT
            self.send_json(status, result)

        elif self.path == "/desktop/observe/fail":
            node_id = self._validate_node_id(body.get("node_id"))
            if node_id is None:
                return
            request_id = body.get("request_id")
            if not isinstance(request_id, str) or not request_id.strip():
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid or missing request_id"})
                return
            error = body.get("error")
            if not isinstance(error, str) or not error.strip():
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid or missing error"})
                return
            message = body.get("message")
            if message is not None and not isinstance(message, str):
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid message"})
                return
            result = fail_observe_request(
                settings,
                request_id.strip(),
                node_id,
                error.strip(),
                message=message,
            )
            status = HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT
            self.send_json(status, result)

        elif self.path == "/desktop/observe/request":
            self.send_json(HTTPStatus.NOT_IMPLEMENTED, {
                "ok": False,
                "error": "not_implemented",
                "message": (
                    "External observe request creation is disabled. "
                    "Create requests via chat (/observe_request) or internal control-plane API."
                ),
            })
        else:
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Endpoint not found"})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/desktop/status":
            if not self.check_enabled():
                return
            if not self.authenticate():
                return

            nodes = list_nodes(settings)
            serialized_nodes = []
            for node in nodes:
                serialized_nodes.append({
                    "node_id": node.node_id,
                    "display_name": node.display_name,
                    "node_type": node.node_type.value,
                    "status": node.status.value,
                    "last_seen_at": node.last_seen_at,
                    "capabilities": list(node.capabilities),
                    "trust_level": node.trust_level.value,
                    "metadata": node.metadata,
                })
            self.send_json(HTTPStatus.OK, {
                "ok": True,
                "nodes": serialized_nodes
            })

        elif path == "/desktop/observe/pending":
            if not self.check_enabled():
                return
            if not self.authenticate():
                return

            query = parse_qs(parsed.query)
            node_id_list = query.get("node_id", [])
            node_id = node_id_list[0] if node_id_list else None
            validated = self._validate_node_id(node_id)
            if validated is None:
                return
            requests = list_pending_observe_requests(settings, validated, limit=1)
            self.send_json(HTTPStatus.OK, {"ok": True, "requests": requests})

        else:
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Endpoint not found"})


def main():
    host = os.getenv("CONVEYOR_DESKTOP_AGENT_SERVER_HOST", "127.0.0.1").strip()
    port_str = os.getenv("CONVEYOR_DESKTOP_AGENT_SERVER_PORT", "8766").strip()
    try:
        port = int(port_str)
    except ValueError:
        port = 8766

    logger.info("Starting Conveyor Desktop Agent Server on http://%s:%d", host, port)
    server = ThreadingHTTPServer((host, port), DesktopAgentHTTPHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()