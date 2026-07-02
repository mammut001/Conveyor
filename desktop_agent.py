"""desktop_agent.py — local desktop agent stub for Conveyor."""
from __future__ import annotations

import json
import os
import platform
import socket
import sys
import time
import urllib.error
import urllib.request

# Try to load environment from .env file if dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def post_json(url: str, token: str, data: dict) -> dict:
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    token = os.getenv("CONVEYOR_DESKTOP_AGENT_TOKEN", "").strip()
    if not token:
        print("Error: CONVEYOR_DESKTOP_AGENT_TOKEN environment variable is required.", file=sys.stderr)
        sys.exit(1)

    control_plane_url = os.getenv("CONVEYOR_CONTROL_PLANE_URL", "http://127.0.0.1:8766").rstrip("/")
    node_id = os.getenv("CONVEYOR_DESKTOP_NODE_ID", "macbook-payton").strip()
    display_name = os.getenv("CONVEYOR_DESKTOP_NODE_NAME", "Payton MacBook").strip()
    interval_str = os.getenv("CONVEYOR_DESKTOP_HEARTBEAT_INTERVAL_SECONDS", "30").strip()

    try:
        interval = float(interval_str)
    except ValueError:
        interval = 30.0

    register_url = f"{control_plane_url}/desktop/register"
    reg_data = {
        "node_id": node_id,
        "display_name": display_name,
        "agent_version": "0.1.0",
        "host": {
            "platform": "macOS" if platform.system() == "Darwin" else platform.system(),
            "hostname": socket.gethostname(),
            "arch": platform.machine()
        }
    }

    print("Registering agent with control plane...")
    try:
        res = post_json(register_url, token, reg_data)
        if res.get("ok"):
            print(f"Desktop agent registered: {node_id}")
        else:
            print(f"Registration failed: {res.get('error')}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"Registration failed: {e}", file=sys.stderr)
        sys.exit(1)

    heartbeat_url = f"{control_plane_url}/desktop/heartbeat"
    while True:
        try:
            hb_data = {
                "node_id": node_id,
                "agent_state": "idle",
                "last_action": "heartbeat"
            }
            res = post_json(heartbeat_url, token, hb_data)
            if res.get("ok"):
                print("Heartbeat ok: online")
            else:
                print(f"Heartbeat failed: {res.get('error')}", file=sys.stderr)
        except Exception as e:
            print(f"Heartbeat failed: {e}", file=sys.stderr)

        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAgent stopped.")
        sys.exit(0)
