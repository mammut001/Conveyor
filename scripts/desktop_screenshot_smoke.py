#!/usr/bin/env python3
"""desktop_screenshot_smoke.py — P5.2 read-only screenshot observe tests."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/test_desktop_screenshot_workspace")
os.environ.setdefault("CODEX_MEMORY_ROOT", "/tmp/test_desktop_screenshot_memory")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "1")

import asyncio
from unittest import mock

from config import Settings
from desktop_screenshot import (
    capture_screenshot_once,
    latest_screenshot_metadata,
    list_screenshot_metadata,
    validate_helper_payload,
)
from handlers.intent import route_intent
from handlers.tools.executors import exec_desktop_screenshot_status

FAILURES: list[str] = []


def _fail(name: str, detail: str) -> None:
    print(f"[fail] {name}: {detail}")
    FAILURES.append(name)


def _make_fake_helper(path: Path, mode: str) -> None:
    script = f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

mode = {mode!r}
args = sys.argv[1:]
output = None
for i, arg in enumerate(args):
    if arg == "--output" and i + 1 < len(args):
        output = args[i + 1]

if mode == "permission":
    print(json.dumps({{
        "ok": False,
        "error": "screen_recording_permission_required",
        "message": "Screen Recording permission is required."
    }}))
    sys.exit(1)

if mode == "relative_path":
    print(json.dumps({{
        "ok": True,
        "path": "relative.png",
        "sha256": "00",
        "width": 1,
        "height": 1,
        "display_id": 1,
        "created_at": "2026-07-02T12:00:00Z"
    }}))
    sys.exit(0)

if mode == "sha_mismatch":
    if output:
        Path(output).write_bytes(b"\\x89PNG\\r\\n\\x1a\\nfake")
    print(json.dumps({{
        "ok": True,
        "path": output,
        "sha256": "deadbeef",
        "width": 1,
        "height": 1,
        "display_id": 1,
        "created_at": "2026-07-02T12:00:00Z"
    }}))
    sys.exit(0)

if mode == "success" and output:
    data = b"\\x89PNG\\r\\n\\x1a\\nok"
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_bytes(data)
    digest = __import__("hashlib").sha256(data).hexdigest()
    print(json.dumps({{
        "ok": True,
        "path": output,
        "sha256": digest,
        "width": 2,
        "height": 2,
        "display_id": 1,
        "created_at": "2026-07-02T12:00:00Z",
        "helper_version": "fake-0.1.0"
    }}))
    sys.exit(0)

print(json.dumps({{"ok": False, "error": "fake_helper_error"}}))
sys.exit(2)
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _settings(helper: str | None, screenshot_dir: Path) -> Settings:
    from config import load_settings

    os.environ["CONVEYOR_DESKTOP_SCREENSHOT_HELPER"] = helper or ""
    os.environ["CONVEYOR_DESKTOP_SCREENSHOT_DIR"] = str(screenshot_dir)
    return load_settings()


def _test_helper_not_configured() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(None, Path(tmp))
        result = capture_screenshot_once(settings)
        if result.get("ok"):
            _fail("helper_not_configured", f"result={result}")
            return
        if result.get("error") != "screenshot_helper_not_configured":
            _fail("helper_not_configured", f"error={result.get('error')}")
            return
    print("[pass] helper_not_configured")


def _test_fake_helper_success() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        helper = tmp_path / "fake_helper.py"
        _make_fake_helper(helper, "success")
        settings = _settings(str(helper), tmp_path / "shots")
        result = capture_screenshot_once(settings)
        if not result.get("ok"):
            _fail("fake_helper_success", f"result={result}")
            return
        png_path = Path(result["path"])
        if not png_path.is_file():
            _fail("fake_helper_success", "png missing")
            return
        meta_path = Path(result["metadata_path"])
        if not meta_path.is_file():
            _fail("fake_helper_success", "metadata missing")
            return
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("sha256") != result.get("sha256"):
            _fail("fake_helper_success", "metadata sha mismatch")
            return
        if "base64" in meta or "ocr" in meta:
            _fail("fake_helper_success", "forbidden metadata fields present")
            return
    print("[pass] fake_helper_success")


def _test_fake_helper_permission_error() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        helper = tmp_path / "fake_helper_perm.py"
        _make_fake_helper(helper, "permission")
        settings = _settings(str(helper), tmp_path / "shots")
        result = capture_screenshot_once(settings)
        if result.get("ok"):
            _fail("fake_helper_permission_error", f"result={result}")
            return
        if result.get("error") != "screen_recording_permission_required":
            _fail("fake_helper_permission_error", f"error={result.get('error')}")
            return
    print("[pass] fake_helper_permission_error")


def _test_fake_helper_sha_mismatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        helper = tmp_path / "fake_helper_sha.py"
        _make_fake_helper(helper, "sha_mismatch")
        settings = _settings(str(helper), tmp_path / "shots")
        result = capture_screenshot_once(settings)
        if result.get("ok"):
            _fail("fake_helper_sha_mismatch", f"result={result}")
            return
        if result.get("error") != "sha256_mismatch":
            _fail("fake_helper_sha_mismatch", f"error={result.get('error')}")
            return
    print("[pass] fake_helper_sha_mismatch")


def _test_validate_relative_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "out.png"
        payload = {
            "ok": True,
            "path": "relative.png",
            "sha256": "abc",
            "width": 1,
            "height": 1,
        }
        result = validate_helper_payload(payload, expected_output_path=output, max_bytes=5000)
        if result.get("ok"):
            _fail("validate_relative_path", f"result={result}")
            return
        if result.get("error") != "path_mismatch":
            _fail("validate_relative_path", f"error={result.get('error')}")
            return
    print("[pass] validate_relative_path")


def _test_validate_direct_relative_payload() -> None:
    payload = {
        "ok": True,
        "path": "relative.png",
        "sha256": "abc",
        "width": 1,
        "height": 1,
    }
    result = validate_helper_payload(
        payload,
        expected_output_path=Path("/tmp/expected.png"),
        max_bytes=5000,
    )
    if result.get("error") != "path_mismatch":
        _fail("validate_direct_relative_payload", f"error={result.get('error')}")
        return
    print("[pass] validate_direct_relative_payload")


def _test_nl_routes_to_desktop_screenshot_status() -> None:
    phrases = [
        "截图看看我电脑现在是什么",
        "看一下 MacBook 屏幕",
        "take a screenshot on my desktop",
    ]
    for phrase in phrases:
        route = route_intent(phrase)
        if route.kind != "deterministic":
            _fail("nl_routes_screenshot_observe", f"{phrase!r} kind={route.kind}")
            return
        if "desktop.observe.request" not in route.tools:
            _fail("nl_routes_screenshot_observe", f"{phrase!r} tools={route.tools}")
            return
        if "computer.status" in route.tools:
            _fail("nl_routes_screenshot_observe", f"{phrase!r} wrongly routed to computer.status")
            return
    print("[pass] nl_routes_screenshot_observe")


def _test_relative_helper_path_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings("bin/capture-screen-helper", Path(tmp))
        result = capture_screenshot_once(settings)
        if result.get("ok"):
            _fail("relative_helper_path_refused", f"result={result}")
            return
        if result.get("error") != "screenshot_helper_path_not_absolute":
            _fail("relative_helper_path_refused", f"error={result.get('error')}")
            return
    print("[pass] relative_helper_path_refused")


def _test_subprocess_uses_list_args_no_shell() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        helper = tmp_path / "fake_helper.py"
        _make_fake_helper(helper, "success")
        settings = _settings(str(helper), tmp_path / "shots")
        with mock.patch("desktop_screenshot.subprocess.run") as run_mock:
            run_mock.return_value = mock.Mock(stdout='{"ok": false, "error": "stopped"}', stderr="")
            capture_screenshot_once(settings)
            if not run_mock.called:
                _fail("subprocess_no_shell", "subprocess.run not called")
                return
            _, kwargs = run_mock.call_args
            if kwargs.get("shell"):
                _fail("subprocess_no_shell", "shell=True was used")
                return
            cmd = run_mock.call_args.args[0]
            if not isinstance(cmd, list):
                _fail("subprocess_no_shell", f"cmd type={type(cmd)}")
                return
    print("[pass] subprocess_no_shell")


def _test_metadata_listing_and_latest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        settings = _settings(None, tmp_path)
        old = {
            "screenshot_id": "old-shot",
            "created_at": "2026-07-01T10:00:00Z",
            "sha256": "a" * 64,
            "width": 1,
            "height": 1,
            "bytes": 10,
        }
        new = {
            "screenshot_id": "new-shot",
            "created_at": "2026-07-02T12:00:00Z",
            "sha256": "b" * 64,
            "width": 2,
            "height": 2,
            "bytes": 20,
        }
        (tmp_path / "old-shot.json").write_text(json.dumps(old), encoding="utf-8")
        (tmp_path / "new-shot.json").write_text(json.dumps(new), encoding="utf-8")
        (tmp_path / "bad.json").write_text("{not-json", encoding="utf-8")

        records = list_screenshot_metadata(settings, limit=5)
        if len(records) != 2:
            _fail("metadata_listing_and_latest", f"records={records}")
            return
        latest = latest_screenshot_metadata(settings)
        if not latest or latest.get("screenshot_id") != "new-shot":
            _fail("metadata_listing_and_latest", f"latest={latest}")
            return
    print("[pass] metadata_listing_and_latest")


def _test_metadata_missing_dir_empty() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(None, Path(tmp) / "missing")
        if list_screenshot_metadata(settings):
            _fail("metadata_missing_dir_empty", "expected empty list")
            return
        if latest_screenshot_metadata(settings) is not None:
            _fail("metadata_missing_dir_empty", "expected None latest")
            return
    print("[pass] metadata_missing_dir_empty")


def _test_status_shows_truncated_sha_and_no_capture() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        helper = tmp_path / "fake_helper.py"
        _make_fake_helper(helper, "success")
        settings = _settings(str(helper), tmp_path / "shots")
        meta = {
            "screenshot_id": "status-shot",
            "created_at": "2026-07-02T04:30:00Z",
            "sha256": "abcd1234efgh5678" + "0" * 48,
            "width": 3024,
            "height": 1964,
            "bytes": 1234567,
            "path": str(tmp_path / "shots" / "status-shot.png"),
        }
        (tmp_path / "shots").mkdir(parents=True, exist_ok=True)
        (tmp_path / "shots" / "status-shot.json").write_text(json.dumps(meta), encoding="utf-8")

        with mock.patch("desktop_screenshot.capture_screenshot_once") as capture_mock:
            text = asyncio.run(exec_desktop_screenshot_status(settings, ""))
            capture_mock.assert_not_called()
        if "abcd1234efgh" not in text or "..." not in text:
            _fail("status_truncated_sha", f"text={text}")
            return
        if "status-shot" not in text:
            _fail("status_truncated_sha", "missing screenshot id")
            return
        if "This command does not capture a screenshot or upload." not in text:
            _fail("status_truncated_sha", "missing no-capture disclaimer")
            return
        if "P5.4 thumbnail upload is available" not in text:
            _fail("status_truncated_sha", "missing upload disclaimer")
            return
    print("[pass] status_truncated_sha")


def _test_nl_status_phrases_route() -> None:
    phrases = [
        "截图状态",
        "最近的截图",
        "看看最近截图",
        "desktop screenshot status",
        "latest desktop screenshot",
        "Mac 截图状态",
    ]
    for phrase in phrases:
        route = route_intent(phrase)
        if route.kind != "deterministic":
            _fail("nl_status_phrases_route", f"{phrase!r} -> {route}")
            return
        if "desktop.observe.status" not in route.tools and "desktop.screenshot.status" not in route.tools:
            _fail("nl_status_phrases_route", f"{phrase!r} -> {route}")
            return
    print("[pass] nl_status_phrases_route")


def _test_validate_sha256_helper() -> None:
    data = b"\x89PNG\r\n\x1a\nok"
    digest = hashlib.sha256(data).hexdigest()
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "shot.png"
        output.write_bytes(data)
        payload = {
            "ok": True,
            "path": str(output),
            "sha256": digest,
            "width": 2,
            "height": 2,
        }
        result = validate_helper_payload(payload, expected_output_path=output, max_bytes=5000)
        if not result.get("ok"):
            _fail("validate_sha256_helper", f"result={result}")
            return
    print("[pass] validate_sha256_helper")


def main() -> int:
    _test_helper_not_configured()
    _test_relative_helper_path_refused()
    _test_subprocess_uses_list_args_no_shell()
    _test_fake_helper_success()
    _test_fake_helper_permission_error()
    _test_fake_helper_sha_mismatch()
    _test_validate_relative_path()
    _test_validate_direct_relative_payload()
    _test_metadata_listing_and_latest()
    _test_metadata_missing_dir_empty()
    _test_status_shows_truncated_sha_and_no_capture()
    _test_validate_sha256_helper()
    _test_nl_routes_to_desktop_screenshot_status()
    _test_nl_status_phrases_route()

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("\nAll desktop screenshot smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())