#!/usr/bin/env python3
"""desktop_observe_request_smoke.py — P5.3 remote observe request tests."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/observe_request_workspace")
os.environ.setdefault("CODEX_MEMORY_ROOT", "/tmp/observe_request_memory")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "1")
os.environ.setdefault("CONVEYOR_DESKTOP_NODE_ENABLED", "true")
os.environ.setdefault("CONVEYOR_DESKTOP_SCREENSHOT_HELPER", "/usr/local/bin/capture-screen-helper")

import asyncio

from channel.types import InboundMessage
from config import load_settings
from desktop_observe_requests import (
    claim_observe_request,
    complete_observe_request,
    create_observe_request,
    fail_observe_request,
    list_pending_observe_requests,
    load_observe_requests,
    validate_observe_result,
)
from handlers.intent import route_intent
from handlers.tools.observe_tools import exec_desktop_observe_status

FAILURES: list[str] = []


def _fail(name: str, detail: str) -> None:
    print(f"[fail] {name}: {detail}")
    FAILURES.append(name)


def _msg(text: str = "截图看看我电脑现在是什么") -> InboundMessage:
    return InboundMessage(
        channel="feishu",
        operator_id="ou_test",
        chat_id="oc_test",
        message_id="om_test",
        text=text,
    )


def _settings(tmp: str, *, max_pending: int = 3) -> object:
    os.environ["CODEX_MEMORY_ROOT"] = tmp
    os.environ["CONVEYOR_DESKTOP_OBSERVE_MAX_PENDING"] = str(max_pending)
    return load_settings()


def _fake_result() -> dict:
    return {
        "screenshot_id": "20260702T123500Z_abcd1234",
        "path": "/Users/test/.codex/desktop/screenshots/20260702T123500Z_abcd1234.png",
        "metadata_path": "/Users/test/.codex/desktop/screenshots/20260702T123500Z_abcd1234.json",
        "sha256": "abcd1234efgh5678" + "0" * 48,
        "width": 3024,
        "height": 1964,
        "display_id": 1,
        "created_at": "2026-07-02T12:35:00Z",
        "bytes": 1234567,
        "node_id": "macbook-payton",
        "helper_version": "0.1.0",
    }


def _test_create_and_pending() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(tmp)
        with mock.patch("nodes.state.is_desktop_online", return_value=True):
            created = create_observe_request(settings, _msg(), "截图看看我电脑现在是什么")
        if not created.get("ok"):
            _fail("create_request", str(created))
            return
        request_id = created["request"]["request_id"]
        pending = list_pending_observe_requests(settings, "macbook-payton", limit=1)
        if not pending or pending[0]["request_id"] != request_id:
            _fail("pending_list", str(pending))
            return
    print("[pass] create_and_pending")


def _test_claim_complete() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(tmp)
        with mock.patch("nodes.state.is_desktop_online", return_value=True):
            created = create_observe_request(settings, _msg(), "test")
        request_id = created["request"]["request_id"]
        claim = claim_observe_request(settings, request_id, "macbook-payton")
        if not claim.get("ok") or claim["request"]["status"] != "claimed":
            _fail("claim", str(claim))
            return
        complete = complete_observe_request(
            settings, request_id, "macbook-payton", _fake_result(),
        )
        if not complete.get("ok") or complete["request"]["status"] != "completed":
            _fail("complete", str(complete))
            return
    print("[pass] claim_complete")


def _test_claim_fail() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(tmp)
        with mock.patch("nodes.state.is_desktop_online", return_value=True):
            created = create_observe_request(settings, _msg(), "test")
        request_id = created["request"]["request_id"]
        claim_observe_request(settings, request_id, "macbook-payton")
        failed = fail_observe_request(
            settings, request_id, "macbook-payton",
            "screen_recording_permission_required",
            message="Screen Recording permission is required.",
        )
        if not failed.get("ok") or failed["request"]["status"] != "failed":
            _fail("fail_transition", str(failed))
            return
    print("[pass] claim_fail")


def _test_expired_cannot_claim() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(tmp)
        with mock.patch("nodes.state.is_desktop_online", return_value=True):
            created = create_observe_request(settings, _msg(), "test")
        request_id = created["request"]["request_id"]
        store = load_observe_requests(settings)
        store[request_id]["expires_at"] = "2020-01-01T00:00:00Z"
        from desktop_observe_requests import save_observe_requests
        save_observe_requests(settings, store)
        claim = claim_observe_request(settings, request_id, "macbook-payton")
        if claim.get("ok"):
            _fail("expired_claim", "expected failure")
            return
    print("[pass] expired_claim")


def _test_wrong_node_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(tmp)
        with mock.patch("nodes.state.is_desktop_online", return_value=True):
            created = create_observe_request(settings, _msg(), "test")
        request_id = created["request"]["request_id"]
        claim = claim_observe_request(settings, request_id, "wrong-node")
        if claim.get("ok"):
            _fail("wrong_node", "expected rejection")
            return
    print("[pass] wrong_node")


def _test_too_many_pending() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(tmp, max_pending=1)
        with mock.patch("nodes.state.is_desktop_online", return_value=True):
            first = create_observe_request(settings, _msg(), "one")
            second = create_observe_request(settings, _msg(), "two")
        if not first.get("ok") or second.get("ok"):
            _fail("too_many_pending", f"first={first} second={second}")
            return
    print("[pass] too_many_pending")


def _test_result_validation() -> None:
    good = validate_observe_result(_fake_result())
    if good is None:
        _fail("result_validation_good", "rejected valid metadata")
        return
    bad = validate_observe_result({**_fake_result(), "base64": "data"})
    if bad is not None:
        _fail("result_validation_bad", "accepted forbidden field")
        return
    print("[pass] result_validation")


def _test_corrupt_json_safe() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(tmp)
        path = settings.codex_memory_root / "state" / "desktop_observe_requests.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")
        pending = list_pending_observe_requests(settings, "macbook-payton")
        if pending:
            _fail("corrupt_json", f"expected empty, got {pending}")
            return
    print("[pass] corrupt_json")


def _test_status_output() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(tmp)
        text = asyncio.run(exec_desktop_observe_status(settings, ""))
        if "Recent observe requests" not in text:
            _fail("status_output", text)
            return
    print("[pass] status_output")


def _test_nl_routing() -> None:
    req = route_intent("截图看看我电脑现在是什么")
    if req.kind != "deterministic" or "desktop.observe.request" not in req.tools:
        _fail("nl_request_route", str(req))
        return
    status = route_intent("截图状态")
    if status.kind != "deterministic" or "desktop.observe.status" not in status.tools:
        _fail("nl_status_route", str(status))
        return
    print("[pass] nl_routing")


def _test_feishu_card_buttons() -> None:
    from channel.feishu_cards import (
        desktop_observe_request_card,
        desktop_observe_status_card,
        flatten_card_to_text,
    )

    for builder in (desktop_observe_request_card, desktop_observe_status_card):
        card = builder("pending request obs_test")
        text = flatten_card_to_text(card).lower()
        for phrase in ("metadata", "no upload", "no preview"):
            if phrase not in text:
                _fail("feishu_card_wording", f"missing {phrase}")
                return
        actions = []
        for el in card.get("elements") or []:
            if el.get("tag") == "action":
                for btn in el.get("actions") or []:
                    label = (btn.get("text") or {}).get("content")
                    if label:
                        actions.append(label)
        forbidden = {"Capture", "Upload", "Preview", "Analyze"}
        if forbidden & set(actions):
            _fail("feishu_card_buttons", str(actions))
            return
    print("[pass] feishu_card_buttons")


def _test_no_image_bytes_stored() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(tmp)
        with mock.patch("nodes.state.is_desktop_online", return_value=True):
            created = create_observe_request(settings, _msg(), "test")
        request_id = created["request"]["request_id"]
        claim_observe_request(settings, request_id, "macbook-payton")
        complete_observe_request(settings, request_id, "macbook-payton", _fake_result())
        raw = (settings.codex_memory_root / "state" / "desktop_observe_requests.json").read_text()
        if "base64" in raw.lower() or "png_bytes" in raw:
            _fail("no_image_bytes", "forbidden fields in store")
            return
        data = json.loads(raw)
        if any("image" in str(v).lower() for v in data.values()):
            pass  # metadata path contains 'image' substring ok if not image bytes
    print("[pass] no_image_bytes")


def main() -> int:
    _test_create_and_pending()
    _test_claim_complete()
    _test_claim_fail()
    _test_expired_cannot_claim()
    _test_wrong_node_rejected()
    _test_too_many_pending()
    _test_result_validation()
    _test_corrupt_json_safe()
    _test_status_output()
    _test_nl_routing()
    _test_feishu_card_buttons()
    _test_no_image_bytes_stored()

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("\nAll desktop observe request smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())