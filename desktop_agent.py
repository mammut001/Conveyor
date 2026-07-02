"""desktop_agent.py — local desktop agent for Conveyor."""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import socket
import sys
import time
import urllib.error
import urllib.request

from config import Settings, load_settings
from desktop_screenshot import capture_screenshot_once

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("desktop_agent")

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
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def register_agent(settings: Settings) -> dict:
    token = (settings.conveyor_desktop_agent_token or "").strip()
    if not token:
        return {"ok": False, "error": "missing_token"}

    control_plane_url = os.getenv(
        "CONVEYOR_CONTROL_PLANE_URL", "http://127.0.0.1:8766"
    ).rstrip("/")
    node_id = settings.conveyor_desktop_node_id or "macbook-payton"
    display_name = settings.conveyor_desktop_node_name or "Payton MacBook"

    register_url = f"{control_plane_url}/desktop/register"
    reg_data = {
        "node_id": node_id,
        "display_name": display_name,
        "agent_version": "0.2.0",
        "host": {
            "platform": "macOS" if platform.system() == "Darwin" else platform.system(),
            "hostname": socket.gethostname(),
            "arch": platform.machine(),
        },
    }
    return post_json(register_url, token, reg_data)


def heartbeat_loop(settings: Settings) -> None:
    token = (settings.conveyor_desktop_agent_token or "").strip()
    if not token:
        print("Error: CONVEYOR_DESKTOP_AGENT_TOKEN environment variable is required.", file=sys.stderr)
        sys.exit(1)

    control_plane_url = os.getenv(
        "CONVEYOR_CONTROL_PLANE_URL", "http://127.0.0.1:8766"
    ).rstrip("/")
    node_id = settings.conveyor_desktop_node_id or "macbook-payton"
    interval_str = os.getenv("CONVEYOR_DESKTOP_HEARTBEAT_INTERVAL_SECONDS", "30").strip()

    try:
        interval = float(interval_str)
    except ValueError:
        interval = 30.0

    print("Registering agent with control plane...")
    try:
        res = register_agent(settings)
        if res.get("ok"):
            print(f"Desktop agent registered: {node_id}")
        else:
            print(f"Registration failed: {res.get('error')}", file=sys.stderr)
            sys.exit(1)
    except Exception as exc:
        print(f"Registration failed: {exc}", file=sys.stderr)
        sys.exit(1)

    heartbeat_url = f"{control_plane_url}/desktop/heartbeat"
    while True:
        try:
            hb_data = {
                "node_id": node_id,
                "agent_state": "idle",
                "last_action": "heartbeat",
            }
            res = post_json(heartbeat_url, token, hb_data)
            if res.get("ok"):
                print("Heartbeat ok: online")
            else:
                print(f"Heartbeat failed: {res.get('error')}", file=sys.stderr)
        except Exception as exc:
            print(f"Heartbeat failed: {exc}", file=sys.stderr)

        time.sleep(interval)


def observe_once(settings: Settings, *, try_register: bool = True) -> dict:
    if try_register and settings.conveyor_desktop_agent_token:
        try:
            register_agent(settings)
        except Exception as exc:
            logger.info("register skipped or failed during observe-once: %s", exc)

    return capture_screenshot_once(settings)


def main() -> None:
    parser = argparse.ArgumentParser(description="Conveyor desktop agent")
    parser.add_argument(
        "--observe-once",
        action="store_true",
        help="Capture one read-only screenshot locally and print safe JSON.",
    )
    args = parser.parse_args()

    settings = load_settings()

    if args.observe_once:
        result = observe_once(settings)
        print(json.dumps(result, indent=2, sort_keys=True))
        sys.exit(0 if result.get("ok") else 1)

    heartbeat_loop(settings)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAgent stopped.")
        sys.exit(0)