#!/usr/bin/env python3
"""deploy_verify_p5_2.py — P5.2 deployment-readiness checks (no capture).

Safe on VPS and Mac. Does not invoke capture-screen-helper, read PNG
bytes, or attempt upload.

Run: .venv/bin/python scripts/deploy_verify_p5_2.py
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/deploy_verify_workspace")
os.environ.setdefault("CODEX_MEMORY_ROOT", "/tmp/deploy_verify_memory")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "1")

FAILURES: list[str] = []


def _fail(name: str, detail: str) -> None:
    print(f"[fail] {name}: {detail}")
    FAILURES.append(name)


def _pass(name: str) -> None:
    print(f"[pass] {name}")


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
    except Exception:
        return "(unknown)"


def _check_git_sha_printed() -> None:
    sha = _git_sha()
    print(f"Git SHA: {sha}")
    if sha == "(unknown)":
        _fail("git_sha", "could not resolve HEAD")
        return
    _pass("git_sha")


def _check_run_py_imports_file_lock() -> None:
    try:
        mod = importlib.import_module("runner.operators.run")
        source = Path(mod.__file__).read_text(encoding="utf-8")
    except Exception as exc:
        _fail("run_file_lock_import", str(exc))
        return
    if "from runner.file_lock import file_lock" not in source:
        _fail("run_file_lock_import", "top-level file_lock import missing")
        return
    if "with file_lock(lock_path):" not in source:
        _fail("run_file_lock_import", "file_lock usage missing in start()")
        return
    _pass("run_file_lock_import")


def _check_desktop_screenshot_import() -> None:
    try:
        import desktop_screenshot  # noqa: F401
    except Exception as exc:
        _fail("desktop_screenshot_import", str(exc))
        return
    _pass("desktop_screenshot_import")


def _settings(helper: str | None, screenshot_dir: Path):
    from config import load_settings

    os.environ["CONVEYOR_DESKTOP_SCREENSHOT_HELPER"] = helper or ""
    os.environ["CONVEYOR_DESKTOP_SCREENSHOT_DIR"] = str(screenshot_dir)
    return load_settings()


def _check_metadata_on_empty_dir() -> None:
    from desktop_screenshot import latest_screenshot_metadata, list_screenshot_metadata

    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings("/usr/local/bin/fake-capture-screen-helper", Path(tmp))
        records = list_screenshot_metadata(settings)
        latest = latest_screenshot_metadata(settings)
        if records:
            _fail("metadata_empty_dir", f"expected [], got {records}")
            return
        if latest is not None:
            _fail("metadata_empty_dir", f"expected None, got {latest}")
            return
    _pass("metadata_empty_dir")


def _check_relative_helper_rejected() -> None:
    from desktop_screenshot import helper_configuration_error

    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings("relative/helper", Path(tmp))
        error = helper_configuration_error(settings)
        if error != "screenshot_helper_path_not_absolute":
            _fail("relative_helper_rejected", f"error={error}")
            return
    _pass("relative_helper_rejected")


def _check_absolute_helper_accepted() -> None:
    from desktop_screenshot import helper_configuration_error, resolve_helper_path

    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings("/usr/local/bin/fake-capture-screen-helper", Path(tmp))
        if helper_configuration_error(settings) is not None:
            _fail("absolute_helper_accepted", "unexpected helper error")
            return
        helper = resolve_helper_path(settings)
        if helper is None or not helper.is_absolute():
            _fail("absolute_helper_accepted", f"helper={helper}")
            return
    _pass("absolute_helper_accepted")


def _check_screenshot_status_command() -> None:
    from handlers.commands import COMMAND_TABLE

    for name in ("screenshot_status", "desktop_screenshot_status"):
        if name not in COMMAND_TABLE:
            _fail("screenshot_status_command", f"missing {name}")
            return
    _pass("screenshot_status_command")


def _check_tool_registry() -> None:
    import handlers.tools.executors  # noqa: F401 — populate TOOL_REGISTRY
    from handlers.tools.registry import TOOL_REGISTRY

    required = (
        "desktop.screenshot.status",
        "desktop.observe.request",
        "desktop.observe.status",
        "desktop.observe.cancel",
    )
    for name in required:
        if name not in TOOL_REGISTRY:
            _fail("tool_registry", f"{name} missing")
            return
    _pass("tool_registry")


def _check_observe_commands() -> None:
    from handlers.commands import COMMAND_TABLE

    for name in (
        "observe_request", "observe_status", "observe_cancel",
        "screenshot_request", "request_screenshot",
    ):
        if name not in COMMAND_TABLE:
            _fail("observe_commands", f"missing {name}")
            return
    _pass("observe_commands")


def _check_feishu_card_and_action() -> None:
    from channel.feishu_cards import (
        action_to_command,
        desktop_screenshot_status_card,
        parse_action,
    )

    card = desktop_screenshot_status_card("helper configured")
    elements = card.get("elements") or []
    markdown_blocks = [
        el.get("content", "")
        for el in elements
        if isinstance(el, dict) and el.get("tag") == "markdown"
    ]
    body = "\n".join(markdown_blocks)
    for phrase in ("Read-only", "status only", "does not capture"):
        if phrase.lower() not in body.lower():
            _fail("feishu_card_wording", f"missing {phrase!r} in card body")
            return
    actions = []
    for el in elements:
        if isinstance(el, dict) and el.get("tag") == "action":
            actions.extend(el.get("actions") or [])
    labels = {
        (btn.get("text") or {}).get("content")
        for btn in actions
        if isinstance(btn, dict)
    }
    if labels != {"Refresh", "Nodes"}:
        _fail("feishu_card_buttons", f"buttons={labels}")
        return
    forbidden = {"Capture", "Upload", "Preview", "Analyze"}
    if labels & forbidden:
        _fail("feishu_card_buttons", f"forbidden buttons present: {labels & forbidden}")
        return
    payload = parse_action({"action": "desktop_screenshot_status"})
    if payload is None:
        _fail("feishu_action_mapping", "parse_action rejected desktop_screenshot_status")
        return
    if action_to_command("desktop_screenshot_status") != "desktop_screenshot_status":
        _fail("feishu_action_mapping", "action_to_command mismatch")
        return
    _pass("feishu_card_and_action")


def _check_observe_request_store_transitions() -> None:
    import tempfile
    from unittest import mock

    from channel.types import InboundMessage
    from desktop_observe_requests import (
        claim_observe_request,
        complete_observe_request,
        create_observe_request,
        load_observe_requests,
    )

    msg = InboundMessage(
        channel="feishu",
        operator_id="ou_test",
        chat_id="oc_test",
        message_id="om_test",
        text="test",
    )
    fake_result = {
        "screenshot_id": "shot-1",
        "path": "/tmp/shot.png",
        "metadata_path": "/tmp/shot.json",
        "sha256": "a" * 64,
        "width": 1,
        "height": 1,
        "bytes": 1,
        "node_id": "macbook-payton",
    }
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CODEX_MEMORY_ROOT"] = tmp
        os.environ["CONVEYOR_DESKTOP_NODE_ENABLED"] = "true"
        os.environ["CONVEYOR_DESKTOP_SCREENSHOT_HELPER"] = "/usr/local/bin/fake-helper"
        settings = _settings("/usr/local/bin/fake-helper", Path(tmp))
        with mock.patch("nodes.state.is_desktop_online", return_value=True):
            created = create_observe_request(settings, msg, "test request")
        if not created.get("ok"):
            _fail("observe_store_create", str(created))
            return
        request_id = created["request"]["request_id"]
        claim = claim_observe_request(settings, request_id, "macbook-payton")
        if not claim.get("ok"):
            _fail("observe_store_claim", str(claim))
            return
        complete = complete_observe_request(
            settings, request_id, "macbook-payton", fake_result,
        )
        if not complete.get("ok"):
            _fail("observe_store_complete", str(complete))
            return
        raw = load_observe_requests(settings)
        if "base64" in json.dumps(raw).lower():
            _fail("observe_store_no_image", "image data in store")
            return
    _pass("observe_request_store")


def _check_observe_lock_and_concurrency() -> None:
    from desktop_observe_requests import observe_requests_lock_path, validate_observe_result
    from runner.file_lock import file_lock
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CODEX_MEMORY_ROOT"] = tmp
        os.environ["CONVEYOR_DESKTOP_NODE_ENABLED"] = "true"
        settings = _settings("/usr/local/bin/fake-helper", Path(tmp))

        # 1. observe request lock path check
        lock_path = observe_requests_lock_path(settings)
        expected_lock = Path(tmp) / "state" / "desktop_observe_requests.lock"
        if lock_path.resolve() != expected_lock.resolve():
            _fail("deploy_verify_lock_path", f"expected {expected_lock.resolve()}, got {lock_path.resolve()}")
            return


        with file_lock(lock_path):
            if not lock_path.exists():
                _fail("deploy_verify_lock_path_exists", "lock file not created")
                return

        # 2. metadata-only validation check
        fake_result = {
            "screenshot_id": "shot-1",
            "path": "/tmp/shot.png",
            "metadata_path": "/tmp/shot.json",
            "sha256": "a" * 64,
            "width": 1,
            "height": 1,
            "bytes": 1,
            "node_id": "macbook-payton",
        }
        if validate_observe_result(fake_result) is None:
            _fail("deploy_verify_validation_good", "rejected valid metadata")
            return
        if validate_observe_result({**fake_result, "base64": "data"}) is not None:
            _fail("deploy_verify_validation_bad_base64", "accepted base64")
            return
        if validate_observe_result({**fake_result, "thumbnail": "data"}) is not None:
            _fail("deploy_verify_validation_bad_thumbnail", "accepted thumbnail")
            return

        # 3. concurrent create mini-check
        from nodes.state import register_desktop_node
        from desktop_observe_requests import create_observe_request, load_observe_requests
        from channel.types import InboundMessage
        import threading

        register_desktop_node(settings, "macbook-payton", "Payton MacBook", "0.3.0", {})

        msg = InboundMessage(
            channel="feishu", operator_id="ou_test", chat_id="oc_test", message_id="om_test", text="test",
        )

        def task(idx: int):
            create_observe_request(settings, msg, f"req-{idx}")

        threads = [threading.Thread(target=task, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        store = load_observe_requests(settings)
        if len(store) != 3:
            _fail("deploy_verify_concurrent_create", f"expected 3, got {len(store)}")
            return

    _pass("observe_lock_and_concurrency")


def _check_no_capture_in_status_executor() -> None:
    import asyncio
    from unittest import mock

    from handlers.tools.executors import exec_desktop_screenshot_status

    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings("/usr/local/bin/fake-capture-screen-helper", Path(tmp))
        with mock.patch("desktop_screenshot.capture_screenshot_once") as capture_mock:
            text = asyncio.run(exec_desktop_screenshot_status(settings, ""))
            capture_mock.assert_not_called()
        for phrase in (
            "This command does not capture a screenshot.",
            "Use /observe_request to create a remote observe request (P5.3).",
            "Upload is disabled in P5.2/P5.3.",
        ):
            if phrase not in text:
                _fail("status_executor_wording", f"missing {phrase!r}")
                return
    _pass("status_executor_no_capture")


def main() -> int:
    print("P5.2/P5.3 deploy verify")
    _check_git_sha_printed()
    _check_run_py_imports_file_lock()
    _check_desktop_screenshot_import()
    _check_metadata_on_empty_dir()
    _check_relative_helper_rejected()
    _check_absolute_helper_accepted()
    _check_screenshot_status_command()
    _check_tool_registry()
    _check_observe_commands()
    _check_feishu_card_and_action()
    _check_observe_request_store_transitions()
    _check_observe_lock_and_concurrency()
    _check_no_capture_in_status_executor()

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("\nAll P5.2/P5.3 deploy verify checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())