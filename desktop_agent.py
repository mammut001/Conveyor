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


def generate_thumbnail(
    source_path: Path,
    dest_path: Path,
    max_width: int,
    max_height: int,
    max_bytes: int,
) -> bool:
    import subprocess
    from pathlib import Path
    dim = max_width
    for attempt in range(4):
        cmd = ["sips", "-Z", str(dim), str(source_path), "--out", str(dest_path)]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode == 0 and dest_path.is_file():
                size = dest_path.stat().st_size
                if size <= max_bytes:
                    return True
                else:
                    dim = int(dim * 0.8)
            else:
                logger.error("sips failure: %s %s", res.stdout, res.stderr)
        except Exception as e:
            logger.error("sips execution failed: %s", e)
            break
    return False


def resolve_local_screenshot_source(
    settings: Settings,
    screenshot_id: str,
) -> Path | None:
    import os
    import json
    import hashlib
    from pathlib import Path
    from desktop_screenshot import resolve_screenshot_dir

    if not screenshot_id or not isinstance(screenshot_id, str):
        return None
    screenshot_id = screenshot_id.strip()
    if not screenshot_id or "/" in screenshot_id or ".." in screenshot_id or len(screenshot_id) > 128:
        return None

    screenshot_dir = resolve_screenshot_dir(settings)
    if not screenshot_dir.is_dir():
        return None

    screenshot_dir = screenshot_dir.resolve()

    meta_path = (screenshot_dir / f"{screenshot_id}.json").resolve()
    try:
        meta_path.relative_to(screenshot_dir)
    except ValueError:
        return None

    source_path = None
    expected_sha = None

    if meta_path.is_file() and not meta_path.is_symlink():
        try:
            meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
            p = meta_data.get("path")
            if p:
                path_obj = Path(p)
                if path_obj.is_absolute():
                    resolved_p = path_obj.resolve()
                    resolved_p.relative_to(screenshot_dir)
                    if resolved_p.is_file() and not resolved_p.is_symlink() and resolved_p.suffix == ".png":
                        source_path = resolved_p
                        expected_sha = meta_data.get("sha256")
        except Exception:
            pass

    if source_path is None:
        fallback_p = (screenshot_dir / f"{screenshot_id}.png").resolve()
        try:
            fallback_p.relative_to(screenshot_dir)
            if fallback_p.is_file() and not fallback_p.is_symlink():
                source_path = fallback_p
                if not expected_sha and meta_path.is_file() and not meta_path.is_symlink():
                    try:
                        meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
                        expected_sha = meta_data.get("sha256")
                    except Exception:
                        pass
        except (ValueError, Exception):
            pass

    if source_path is None:
        return None

    if not source_path.is_absolute():
        return None
    if not source_path.is_file():
        return None
    if source_path.is_symlink() or os.path.islink(source_path):
        return None
    if source_path.suffix != ".png":
        return None

    if expected_sha:
        try:
            hasher = hashlib.sha256()
            with source_path.open("rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    hasher.update(chunk)
            if hasher.hexdigest() != expected_sha:
                return None
        except Exception:
            return None

    return source_path


def poll_upload_once(settings: Settings) -> None:
    token = (settings.conveyor_desktop_agent_token or "").strip()
    if not token:
        return

    control_plane_url = _control_plane_url()
    node_id = settings.conveyor_desktop_node_id or "macbook-payton"
    pending_url = (
        f"{control_plane_url}/desktop/upload/pending?"
        f"{urllib.parse.urlencode({'node_id': node_id})}"
    )

    try:
        payload = get_json(pending_url, token)
    except Exception as exc:
        logger.info("upload poll failed: %s", exc)
        return

    requests = payload.get("requests") if isinstance(payload, dict) else None
    if not isinstance(requests, list) or not requests:
        return

    pending = requests[0]
    if not isinstance(pending, dict):
        return
    upload_id = pending.get("upload_id")
    screenshot_id = pending.get("screenshot_id")
    max_width = pending.get("max_width") or 1280
    max_height = pending.get("max_height") or 800
    max_bytes = pending.get("max_bytes") or 750000

    if not isinstance(upload_id, str) or not upload_id:
        return

    try:
        claim_res = post_json(
            f"{control_plane_url}/desktop/upload/claim",
            token,
            {"upload_id": upload_id, "node_id": node_id},
        )
    except Exception as exc:
        logger.info("upload claim failed upload_id=%s: %s", upload_id, exc)
        return

    if not claim_res.get("ok"):
        logger.info(
            "upload claim rejected upload_id=%s error=%s",
            upload_id,
            claim_res.get("error"),
        )
        return

    logger.info("Upload request claimed: %s", upload_id)
    source_img_path = resolve_local_screenshot_source(settings, screenshot_id)
    if not source_img_path:
        logger.error("Local screenshot source validation failed for screenshot_id=%s", screenshot_id)
        try:
            post_json(
                f"{control_plane_url}/desktop/upload/fail",
                token,
                {
                    "upload_id": upload_id,
                    "node_id": node_id,
                    "error": "invalid_screenshot_source",
                    "message": "Local screenshot source validation failed.",
                },
            )
        except Exception as exc:
            logger.info("upload fail report failed: %s", exc)
        return

    from desktop_screenshot import resolve_screenshot_dir
    screenshot_dir = resolve_screenshot_dir(settings)

    thumb_path = screenshot_dir / f"thumb_{upload_id}.png"
    ok = generate_thumbnail(source_img_path, thumb_path, max_width, max_height, max_bytes)
    
    if not ok or not thumb_path.is_file():
        logger.error("Thumbnail generation failed for upload_id=%s", upload_id)
        try:
            post_json(
                f"{control_plane_url}/desktop/upload/fail",
                token,
                {
                    "upload_id": upload_id,
                    "node_id": node_id,
                    "error": "thumbnail_generation_failed",
                    "message": "Failed to generate thumbnail via sips.",
                },
            )
        except Exception as exc:
            logger.info("upload fail report failed: %s", exc)
        return

    try:
        thumb_bytes = thumb_path.read_bytes()
    except Exception as e:
        logger.error("Failed to read generated thumbnail: %s", e)
        if thumb_path.exists():
            try:
                thumb_path.unlink()
            except Exception:
                pass
        try:
            post_json(
                f"{control_plane_url}/desktop/upload/fail",
                token,
                {
                    "upload_id": upload_id,
                    "node_id": node_id,
                    "error": "thumbnail_read_failed",
                    "message": str(e),
                },
            )
        except Exception as exc:
            logger.info("upload fail report failed: %s", exc)
        return

    width = max_width
    height = max_height
    try:
        import subprocess
        res = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(thumb_path)], capture_output=True, text=True, check=False)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                if "pixelWidth:" in line:
                    width = int(line.split(":")[-1].strip())
                elif "pixelHeight:" in line:
                    height = int(line.split(":")[-1].strip())
    except Exception:
        pass

    import hashlib
    hasher = hashlib.sha256()
    hasher.update(thumb_bytes)
    sha256 = hasher.hexdigest()

    query_params = {
        'upload_id': upload_id,
        'node_id': node_id,
        'sha256': sha256,
        'width': str(width),
        'height': str(height),
        'bytes': str(len(thumb_bytes)),
    }
    complete_url = f"{control_plane_url}/desktop/upload/complete?{urllib.parse.urlencode(query_params)}"

    try:
        req = urllib.request.Request(
            complete_url,
            data=thumb_bytes,
            headers={
                "Content-Type": "application/octet-stream",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            complete_res = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logger.error("Upload complete request failed: %s", exc)
        if thumb_path.exists():
            try:
                thumb_path.unlink()
            except Exception:
                pass
        return

    if thumb_path.exists():
        try:
            thumb_path.unlink()
        except Exception:
            pass

    if complete_res.get("ok"):
        logger.info("Upload completed successfully: %s", upload_id)
    else:
        logger.error("Upload complete rejected: %s", complete_res.get("error"))


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
            poll_upload_once(settings)
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