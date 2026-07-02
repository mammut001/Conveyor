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
import urllib.parse
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


def _control_plane_url() -> str:
    return os.getenv("CONVEYOR_CONTROL_PLANE_URL", "http://127.0.0.1:8766").rstrip("/")


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


def get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def register_agent(settings: Settings) -> dict:
    token = (settings.conveyor_desktop_agent_token or "").strip()
    if not token:
        return {"ok": False, "error": "missing_token"}

    control_plane_url = _control_plane_url()
    node_id = settings.conveyor_desktop_node_id or "macbook-payton"
    display_name = settings.conveyor_desktop_node_name or "Payton MacBook"

    register_url = f"{control_plane_url}/desktop/register"
    reg_data = {
        "node_id": node_id,
        "display_name": display_name,
        "agent_version": "0.3.0",
        "host": {
            "platform": "macOS" if platform.system() == "Darwin" else platform.system(),
            "hostname": socket.gethostname(),
            "arch": platform.machine(),
        },
    }
    return post_json(register_url, token, reg_data)


def send_heartbeat_once(settings: Settings) -> dict:
    token = (settings.conveyor_desktop_agent_token or "").strip()
    control_plane_url = _control_plane_url()
    node_id = settings.conveyor_desktop_node_id or "macbook-payton"
    heartbeat_url = f"{control_plane_url}/desktop/heartbeat"
    hb_data = {
        "node_id": node_id,
        "agent_state": "idle",
        "last_action": "heartbeat",
    }
    return post_json(heartbeat_url, token, hb_data)


def _observe_result_from_capture(capture: dict, settings: Settings) -> dict:
    node_id = settings.conveyor_desktop_node_id or "macbook-payton"
    return {
        "screenshot_id": capture.get("screenshot_id"),
        "path": capture.get("path"),
        "metadata_path": capture.get("metadata_path"),
        "sha256": capture.get("sha256"),
        "width": capture.get("width"),
        "height": capture.get("height"),
        "display_id": capture.get("display_id"),
        "created_at": capture.get("created_at"),
        "bytes": capture.get("bytes"),
        "node_id": node_id,
        "helper_version": capture.get("helper_version"),
    }


def poll_observe_once(settings: Settings) -> None:
    """Poll for one pending observe request, capture locally, submit metadata only."""
    token = (settings.conveyor_desktop_agent_token or "").strip()
    if not token:
        return

    control_plane_url = _control_plane_url()
    node_id = settings.conveyor_desktop_node_id or "macbook-payton"
    pending_url = (
        f"{control_plane_url}/desktop/observe/pending?"
        f"{urllib.parse.urlencode({'node_id': node_id})}"
    )

    try:
        payload = get_json(pending_url, token)
    except Exception as exc:
        logger.info("observe poll failed: %s", exc)
        return

    requests = payload.get("requests") if isinstance(payload, dict) else None
    if not isinstance(requests, list) or not requests:
        return

    pending = requests[0]
    if not isinstance(pending, dict):
        return
    request_id = pending.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return

    try:
        claim_res = post_json(
            f"{control_plane_url}/desktop/observe/claim",
            token,
            {"request_id": request_id, "node_id": node_id},
        )
    except Exception as exc:
        logger.info("observe claim failed request_id=%s: %s", request_id, exc)
        return

    if not claim_res.get("ok"):
        logger.info(
            "observe claim rejected request_id=%s error=%s",
            request_id,
            claim_res.get("error"),
        )
        return

    logger.info("Observe request claimed: %s", request_id)

    capture = capture_screenshot_once(settings)
    if capture.get("ok"):
        result = _observe_result_from_capture(capture, settings)
        try:
            complete_res = post_json(
                f"{control_plane_url}/desktop/observe/complete",
                token,
                {"request_id": request_id, "node_id": node_id, "result": result},
            )
        except Exception as exc:
            logger.info("observe complete failed request_id=%s: %s", request_id, exc)
            return
        if complete_res.get("ok"):
            logger.info(
                "Observe completed: screenshot_id=%s",
                result.get("screenshot_id"),
            )
        else:
            logger.info(
                "observe complete rejected request_id=%s error=%s",
                request_id,
                complete_res.get("error"),
            )
        return

    error_code = capture.get("error") or "observe_capture_failed"
    error_message = capture.get("message") or "Screenshot capture failed."
    logger.info("Observe failed: %s", error_code)
    try:
        post_json(
            f"{control_plane_url}/desktop/observe/fail",
            token,
            {
                "request_id": request_id,
                "node_id": node_id,
                "error": error_code,
                "message": error_message,
            },
        )
    except Exception as exc:
        logger.info("observe fail report failed request_id=%s: %s", request_id, exc)


def heartbeat_loop(settings: Settings, *, poll_observe: bool = False) -> None:
    token = (settings.conveyor_desktop_agent_token or "").strip()
    if not token:
        print("Error: CONVEYOR_DESKTOP_AGENT_TOKEN environment variable is required.", file=sys.stderr)
        sys.exit(1)

    interval_str = os.getenv("CONVEYOR_DESKTOP_HEARTBEAT_INTERVAL_SECONDS", "30").strip()
    try:
        heartbeat_interval = float(interval_str)
    except ValueError:
        heartbeat_interval = 30.0

    poll_interval = float(settings.conveyor_desktop_observe_poll_interval_seconds)
    sleep_seconds = min(heartbeat_interval, poll_interval) if poll_observe else heartbeat_interval

    print("Registering agent with control plane...")
    try:
        res = register_agent(settings)
        if res.get("ok"):
            node_id = settings.conveyor_desktop_node_id or "macbook-payton"
            print(f"Desktop agent registered: {node_id}")
            if poll_observe:
                print("Observe polling enabled (metadata only; no upload).")
        else:
            print(f"Registration failed: {res.get('error')}", file=sys.stderr)
            sys.exit(1)
    except Exception as exc:
        print(f"Registration failed: {exc}", file=sys.stderr)
        sys.exit(1)

    last_heartbeat = 0.0
    last_poll = 0.0
    while True:
        now = time.time()
        if now - last_heartbeat >= heartbeat_interval:
            try:
                res = send_heartbeat_once(settings)
                if res.get("ok"):
                    print("Heartbeat ok: online")
                else:
                    print(f"Heartbeat failed: {res.get('error')}", file=sys.stderr)
            except Exception as exc:
                print(f"Heartbeat failed: {exc}", file=sys.stderr)
            last_heartbeat = now

        if poll_observe and now - last_poll >= poll_interval:
            poll_observe_once(settings)
            last_poll = now

        time.sleep(sleep_seconds)


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
    parser.add_argument(
        "--poll-observe",
        action="store_true",
        help="Run heartbeat loop and poll for remote observe requests (metadata only).",
    )
    args = parser.parse_args()

    settings = load_settings()

    if args.observe_once:
        result = observe_once(settings)
        print(json.dumps(result, indent=2, sort_keys=True))
        sys.exit(0 if result.get("ok") else 1)

    heartbeat_loop(settings, poll_observe=args.poll_observe)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAgent stopped.")
        sys.exit(0)