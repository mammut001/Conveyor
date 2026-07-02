"""desktop_agent_server.py — minimal HTTP server for Conveyor control plane."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import socketserver
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer

class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

from config import load_settings
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
        if not auth_header.startswith("Bearer ") or auth_header[7:].strip() != expected_token:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "Unauthorized: Invalid or missing token"})
            return False
        return True

    def check_enabled(self) -> bool:
        """Checks if desktop node is enabled. Returns True if enabled, False otherwise."""
        if not settings.conveyor_desktop_node_enabled:
            self.send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Forbidden: Desktop node is disabled."})
            return False
        return True

    def do_POST(self):
        if not self.check_enabled():
            return
        if not self.authenticate():
            return

        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)
        try:
            body = json.loads(post_data)
        except Exception:
            self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid JSON body"})
            return

        if self.path == "/desktop/register":
            node_id = body.get("node_id")
            display_name = body.get("display_name")
            agent_version = body.get("agent_version")
            host = body.get("host")
            if not all([node_id, display_name, agent_version, host]):
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing required registration fields"})
                return

            register_desktop_node(settings, node_id, display_name, agent_version, host)
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
            if not node_id or not agent_state:
                self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Missing node_id or agent_state"})
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
        else:
            self.send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Endpoint not found"})

    def do_GET(self):
        if self.path == "/desktop/status":
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
