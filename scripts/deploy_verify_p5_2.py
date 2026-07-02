#!/usr/bin/env python3
"""deploy_verify_p5_2.py — P5.2 deployment-readiness checks (no capture).

Safe on VPS and Mac. Does not invoke capture-screen-helper, read PNG
bytes, or attempt upload.

Run: .venv/bin/python scripts/deploy_verify_p5_2.py
"""
from __future__ import annotations

import importlib
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

    if "desktop.screenshot.status" not in TOOL_REGISTRY:
        _fail("tool_registry", "desktop.screenshot.status missing")
        return
    _pass("tool_registry")


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
            "Remote screenshot trigger is not implemented yet.",
            "Upload is disabled in P5.2.",
        ):
            if phrase not in text:
                _fail("status_executor_wording", f"missing {phrase!r}")
                return
    _pass("status_executor_no_capture")


def main() -> int:
    print("P5.2 deploy verify")
    _check_git_sha_printed()
    _check_run_py_imports_file_lock()
    _check_desktop_screenshot_import()
    _check_metadata_on_empty_dir()
    _check_relative_helper_rejected()
    _check_absolute_helper_accepted()
    _check_screenshot_status_command()
    _check_tool_registry()
    _check_feishu_card_and_action()
    _check_no_capture_in_status_executor()

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("\nAll P5.2 deploy verify checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())