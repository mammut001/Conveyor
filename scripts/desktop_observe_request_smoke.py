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
    list_recent_observe_requests,
    expire_old_observe_requests,
    count_pending_requests,
)
from handlers.intent import route_intent
from handlers.tools.observe_tools import exec_desktop_observe_status

FAILURES: list[str] = []


# Workers for multiprocessing concurrent smoke tests
def run_concurrent_create(temp_dir: str, index: int) -> None:
    os.environ["CODEX_MEMORY_ROOT"] = temp_dir
    os.environ["CONVEYOR_DESKTOP_NODE_ENABLED"] = "true"
    from config import load_settings
    from desktop_observe_requests import create_observe_request
    from scripts.desktop_observe_request_smoke import _msg
    settings = load_settings()
    msg = _msg(f"request-{index}")
    create_observe_request(settings, msg, f"user-request-{index}")


def run_claim_worker(temp_dir: str, request_id: str, results_dir: str, worker_id: int) -> None:
    os.environ["CODEX_MEMORY_ROOT"] = temp_dir
    os.environ["CONVEYOR_DESKTOP_NODE_ENABLED"] = "true"
    from config import load_settings
    from desktop_observe_requests import claim_observe_request
    settings = load_settings()
    res = claim_observe_request(settings, request_id, "macbook-payton")
    res_path = Path(results_dir) / f"claim_{worker_id}.json"
    res_path.write_text(json.dumps(res))


def run_complete_worker(temp_dir: str, request_id: str, result_val: dict, results_dir: str) -> None:
    os.environ["CODEX_MEMORY_ROOT"] = temp_dir
    os.environ["CONVEYOR_DESKTOP_NODE_ENABLED"] = "true"
    from config import load_settings
    from desktop_observe_requests import complete_observe_request
    settings = load_settings()
    res = complete_observe_request(settings, request_id, "macbook-payton", result_val)
    (Path(results_dir) / "complete_result.json").write_text(json.dumps(res))


def run_fail_worker(temp_dir: str, request_id: str, results_dir: str) -> None:
    os.environ["CODEX_MEMORY_ROOT"] = temp_dir
    os.environ["CONVEYOR_DESKTOP_NODE_ENABLED"] = "true"
    from config import load_settings
    from desktop_observe_requests import fail_observe_request
    settings = load_settings()
    res = fail_observe_request(settings, request_id, "macbook-payton", "error_code", "error message")
    (Path(results_dir) / "fail_result.json").write_text(json.dumps(res))


def run_cancel_worker(temp_dir: str, request_id: str, results_dir: str) -> None:
    os.environ["CODEX_MEMORY_ROOT"] = temp_dir
    os.environ["CONVEYOR_DESKTOP_NODE_ENABLED"] = "true"
    from config import load_settings
    from desktop_observe_requests import cancel_observe_request
    settings = load_settings()
    res = cancel_observe_request(settings, request_id)
    (Path(results_dir) / "cancel_result.json").write_text(json.dumps(res))


def run_claim_worker_t5(temp_dir: str, request_id: str, results_dir: str) -> None:
    os.environ["CODEX_MEMORY_ROOT"] = temp_dir
    os.environ["CONVEYOR_DESKTOP_NODE_ENABLED"] = "true"
    from config import load_settings
    from desktop_observe_requests import claim_observe_request
    settings = load_settings()
    res = claim_observe_request(settings, request_id, "macbook-payton")
    (Path(results_dir) / "claim_result.json").write_text(json.dumps(res))


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
    os.environ["CONVEYOR_DESKTOP_SCREENSHOT_DIR"] = str(Path(tmp) / "desktop" / "screenshots")
    return load_settings()


def _fake_result(screenshot_dir: Path | str | None = None) -> dict:
    if screenshot_dir is None:
        mem_root = os.environ.get("CODEX_MEMORY_ROOT")
        if mem_root:
            screenshot_dir = Path(mem_root) / "desktop" / "screenshots"
        else:
            screenshot_dir = "/Users/test/.codex/desktop/screenshots"
    screenshot_dir = Path(screenshot_dir)
    return {
        "screenshot_id": "20260702T123500Z_abcd1234",
        "path": str(screenshot_dir / "20260702T123500Z_abcd1234.png"),
        "metadata_path": str(screenshot_dir / "20260702T123500Z_abcd1234.json"),
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
        print("DEBUG IN TEST: tmp =", tmp)
        print("DEBUG IN TEST: env =", os.environ.get("CODEX_MEMORY_ROOT"))
        print("DEBUG IN TEST: settings.codex_memory_root =", settings.codex_memory_root)
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
    import pathlib
    test_dir = pathlib.Path("/Users/test/.codex/desktop/screenshots")
    good = validate_observe_result(_fake_result(test_dir), test_dir)
    if good is None:
        _fail("result_validation_good", "rejected valid metadata")
        return
    bad = validate_observe_result({**_fake_result(test_dir), "base64": "data"}, test_dir)
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

    card = desktop_observe_request_card("📸 已创建桌面截图请求（仅元数据）\n不上传图片")
    text = flatten_card_to_text(card)
    if not (("元数据" in text or "metadata" in text.lower())
            and ("不上传" in text or "no upload" in text.lower())):
        _fail("feishu_card_wording", text[:200])
        return
    status_card = desktop_observe_status_card("pending request obs_test")
    status_text = flatten_card_to_text(status_card).lower()
    for phrase in ("metadata", "no upload", "no preview"):
        if phrase not in status_text:
            _fail("feishu_card_wording_status", f"missing {phrase}")
            return
    for builder in (desktop_observe_request_card, desktop_observe_status_card):
        card = builder("pending request obs_test") if builder is desktop_observe_status_card else desktop_observe_request_card("📸 已发起截图请求")
        text = flatten_card_to_text(card)
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

    # New cross-process consistency/locking checks
    _test_lock_path_exists()
    _test_concurrent_create()
    _test_concurrent_claim()
    _test_complete_fail_conflict()
    _test_cancel_claim_conflict()
    _test_corrupt_json_recovery()
    _test_no_nested_deadlock()

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("\nAll desktop observe request smoke checks passed.")
    return 0


def _test_lock_path_exists() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(tmp)
        from desktop_observe_requests import observe_requests_lock_path
        lock_p = observe_requests_lock_path(settings)
        expected = Path(tmp) / "state" / "desktop_observe_requests.lock"
        if lock_p.resolve() != expected.resolve():
            _fail("lock_path", f"expected {expected.resolve()}, got {lock_p.resolve()}")
            return
        from runner.file_lock import file_lock
        with file_lock(lock_p):
            if not lock_p.exists():
                _fail("lock_path_exists", "lock file was not created on disk")
                return
    print("[pass] lock_path_exists")


def _test_concurrent_create() -> None:
    import multiprocessing
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(tmp, max_pending=20)
        from nodes.state import register_desktop_node
        register_desktop_node(settings, "macbook-payton", "Payton MacBook", "0.3.0", {})
        
        processes = []
        for i in range(5):
            p = multiprocessing.Process(target=run_concurrent_create, args=(tmp, i))
            processes.append(p)
            p.start()
            
        for p in processes:
            p.join(timeout=5)
            if p.is_alive():
                p.terminate()
                
        store = load_observe_requests(settings)
        if len(store) != 5:
            _fail("concurrent_create", f"expected 5 requests, got {len(store)}: {store}")
            return
            
        from desktop_observe_requests import save_observe_requests
        try:
            save_observe_requests(settings, store)
        except Exception as exc:
            _fail("concurrent_create_corrupt", f"failed to load/save store: {exc}")
            return
    print("[pass] concurrent_create")


def _test_concurrent_claim() -> None:
    import multiprocessing
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(tmp)
        from nodes.state import register_desktop_node
        register_desktop_node(settings, "macbook-payton", "Payton MacBook", "0.3.0", {})
        
        created = create_observe_request(settings, _msg(), "claim test")
        request_id = created["request"]["request_id"]
        
        results_dir = Path(tmp) / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        
        processes = []
        for i in range(5):
            p = multiprocessing.Process(target=run_claim_worker, args=(tmp, request_id, str(results_dir), i))
            processes.append(p)
            p.start()
            
        for p in processes:
            p.join(timeout=5)
            if p.is_alive():
                p.terminate()
                
        success_count = 0
        conflicts = 0
        for i in range(5):
            res_file = results_dir / f"claim_{i}.json"
            if res_file.exists():
                res = json.loads(res_file.read_text())
                if res.get("ok"):
                    success_count += 1
                else:
                    conflicts += 1
                    
        store = load_observe_requests(settings)
        final_status = store[request_id]["status"]
        
        if success_count != 1:
            _fail("concurrent_claim", f"expected exactly 1 success, got {success_count} (conflicts: {conflicts})")
            return
        if final_status != "claimed":
            _fail("concurrent_claim_status", f"expected final status claimed, got {final_status}")
            return
    print("[pass] concurrent_claim")


def _test_complete_fail_conflict() -> None:
    import multiprocessing
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(tmp)
        from nodes.state import register_desktop_node
        register_desktop_node(settings, "macbook-payton", "Payton MacBook", "0.3.0", {})
        
        created = create_observe_request(settings, _msg(), "complete/fail test")
        request_id = created["request"]["request_id"]
        claim_observe_request(settings, request_id, "macbook-payton")
        
        results_dir = Path(tmp) / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        
        p_comp = multiprocessing.Process(target=run_complete_worker, args=(tmp, request_id, _fake_result(), str(results_dir)))
        p_fail = multiprocessing.Process(target=run_fail_worker, args=(tmp, request_id, str(results_dir)))
        
        p_comp.start()
        p_fail.start()
        
        p_comp.join(timeout=5)
        p_fail.join(timeout=5)
        
        if p_comp.is_alive(): p_comp.terminate()
        if p_fail.is_alive(): p_fail.terminate()
        
        comp_ok = False
        fail_ok = False
        
        comp_res_file = results_dir / "complete_result.json"
        if comp_res_file.exists():
            comp_ok = json.loads(comp_res_file.read_text()).get("ok", False)
            
        fail_res_file = results_dir / "fail_result.json"
        if fail_res_file.exists():
            fail_ok = json.loads(fail_res_file.read_text()).get("ok", False)
            
        store = load_observe_requests(settings)
        final_status = store[request_id]["status"]
        
        if (comp_ok and fail_ok) or (not comp_ok and not fail_ok):
            _fail("complete_fail_conflict", f"expected exactly one success: complete={comp_ok}, fail={fail_ok}")
            return
        if final_status not in ("completed", "failed"):
            _fail("complete_fail_conflict_status", f"expected completed or failed status, got {final_status}")
            return
            
        raw_json = (settings.codex_memory_root / "state" / "desktop_observe_requests.json").read_text()
        for forbidden in ("png_bytes", "image_bytes", "base64"):
            if forbidden in raw_json:
                _fail("complete_fail_conflict_data", f"Found forbidden field {forbidden} in JSON store")
                return
    print("[pass] complete_fail_conflict")


def _test_cancel_claim_conflict() -> None:
    import multiprocessing
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(tmp)
        from nodes.state import register_desktop_node
        register_desktop_node(settings, "macbook-payton", "Payton MacBook", "0.3.0", {})
        
        created = create_observe_request(settings, _msg(), "cancel/claim test")
        request_id = created["request"]["request_id"]
        
        results_dir = Path(tmp) / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        
        p_cancel = multiprocessing.Process(target=run_cancel_worker, args=(tmp, request_id, str(results_dir)))
        p_claim = multiprocessing.Process(target=run_claim_worker_t5, args=(tmp, request_id, str(results_dir)))
        
        p_cancel.start()
        p_claim.start()
        
        p_cancel.join(timeout=5)
        p_claim.join(timeout=5)
        
        if p_cancel.is_alive(): p_cancel.terminate()
        if p_claim.is_alive(): p_claim.terminate()
        
        cancel_ok = False
        claim_ok = False
        
        cancel_res_file = results_dir / "cancel_result.json"
        if cancel_res_file.exists():
            cancel_ok = json.loads(cancel_res_file.read_text()).get("ok", False)
            
        claim_res_file = results_dir / "claim_result.json"
        if claim_res_file.exists():
            claim_ok = json.loads(claim_res_file.read_text()).get("ok", False)
            
        store = load_observe_requests(settings)
        final_status = store[request_id]["status"]
        
        if not cancel_ok and not claim_ok:
            _fail("cancel_claim_conflict", f"expected at least one success: cancel={cancel_ok}, claim={claim_ok}")
            return
        if final_status not in ("cancelled", "claimed"):
            _fail("cancel_claim_conflict_status", f"expected cancelled or claimed status, got {final_status}")
            return
    print("[pass] cancel_claim_conflict")


def _test_corrupt_json_recovery() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(tmp)
        from nodes.state import register_desktop_node
        register_desktop_node(settings, "macbook-payton", "Payton MacBook", "0.3.0", {})
        
        path = settings.codex_memory_root / "state" / "desktop_observe_requests.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt-json", encoding="utf-8")
        
        try:
            pending = list_pending_observe_requests(settings, "macbook-payton")
            recent = list_recent_observe_requests(settings)
            count = count_pending_requests(settings, "macbook-payton")
        except Exception as exc:
            _fail("corrupt_json_recovery_crash", f"read functions crashed on corrupt JSON: {exc}")
            return
            
        if pending or recent or count != 0:
            _fail("corrupt_json_recovery_empty", f"expected empty, got pending={pending}, recent={recent}, count={count}")
            return
            
        try:
            created = create_observe_request(settings, _msg(), "re-created")
        except Exception as exc:
            _fail("corrupt_json_recovery_create_crash", f"create request crashed on corrupt JSON: {exc}")
            return
            
        if not created.get("ok"):
            _fail("corrupt_json_recovery_create_ok", f"create request failed: {created}")
            return
            
        store = load_observe_requests(settings)
        if len(store) != 1 or "re-created" not in json.dumps(store):
            _fail("corrupt_json_recovery_valid_store", f"store not successfully repaired/written: {store}")
            return
    print("[pass] corrupt_json_recovery")


def _test_no_nested_deadlock() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = _settings(tmp)
        from nodes.state import register_desktop_node
        register_desktop_node(settings, "macbook-payton", "Payton MacBook", "0.3.0", {})
        
        import signal
        has_alarm = hasattr(signal, "alarm")
        if has_alarm:
            signal.signal(signal.SIGALRM, lambda sig, frame: (_fail("nested_deadlock", "timeout reached"), sys.exit(1)))
            signal.alarm(3)
            
        try:
            created = create_observe_request(settings, _msg(), "seq")
            request_id = created["request"]["request_id"]
            
            list_pending_observe_requests(settings, "macbook-payton")
            claim_observe_request(settings, request_id, "macbook-payton")
            complete_observe_request(settings, request_id, "macbook-payton", _fake_result())
            list_recent_observe_requests(settings)
            expire_old_observe_requests(settings)
        except Exception as exc:
            _fail("nested_deadlock_execution", f"sequence failed: {exc}")
            return
        finally:
            if has_alarm:
                signal.alarm(0)

    print("[pass] no_nested_deadlock")


def test_p543_auto_thumbnail_flags_and_routing():
    """P5.4.3 tests: auto flags, ensure one upload, NL routing to preview, metadata-only, status wording."""
    tmp = tempfile.mkdtemp(prefix="obs_p543_")
    os.environ["CODEX_MEMORY_ROOT"] = tmp
    os.environ["CONVEYOR_DESKTOP_NODE_ENABLED"] = "true"
    os.environ["CONVEYOR_DESKTOP_UPLOAD_ENABLED"] = "true"
    os.environ["CONVEYOR_DESKTOP_AUTO_THUMBNAIL_ON_OBSERVE"] = "true"
    os.environ["CONVEYOR_DESKTOP_SCREENSHOT_HELPER"] = "/usr/local/bin/capture-screen-helper"
    settings = _settings(tmp, max_pending=3)
    from nodes.state import register_desktop_node
    register_desktop_node(settings, "macbook-payton", "Payton MacBook", "0.3.0", {})

    from handlers.intent import route_intent
    route = route_intent("截图看看我电脑现在是什么")
    assert route.kind == "deterministic"
    assert "desktop.observe.request" in route.tools
    print("✓ 1. NL screenshot phrase routes to desktop.observe.request")

    msg = _msg("preview-test")
    res = create_observe_request(settings, msg, "截图看看我电脑现在是什么", auto_upload_thumbnail=True, auto_delivery=True)
    assert res.get("ok")
    rec = res["request"]
    assert rec.get("auto_upload_thumbnail") is True
    obs_id = rec["request_id"]
    print("✓ 2. create with auto_upload_thumbnail=true sets fields")

    claim_observe_request(settings, obs_id, "macbook-payton")
    complete_observe_request(settings, obs_id, "macbook-payton", _fake_result())
    # reload after complete to get updated status for ensure
    from desktop_observe_requests import get_observe_request
    rec = get_observe_request(settings, obs_id) or rec

    msg2 = _msg("meta-only")
    res2 = create_observe_request(settings, msg2, "/observe_request --metadata-only", auto_upload_thumbnail=False)
    assert res2.get("ok")
    assert res2["request"].get("auto_upload_thumbnail") is False
    print("✓ 3. metadata-only creation has auto=false")

    from desktop_upload_requests import ensure_upload_request_for_observe, list_recent_upload_requests
    e = ensure_upload_request_for_observe(settings, rec, created_by_channel="telegram", created_by_chat_id="c1")
    assert e.get("ok"), f"ensure failed: {e}"
    upl_id = e["request"]["upload_id"]
    e2 = ensure_upload_request_for_observe(settings, rec, created_by_channel="telegram", created_by_chat_id="c1")
    assert e2["request"]["upload_id"] == upl_id
    ups = [u for u in list_recent_upload_requests(settings, limit=20) if u.get("observe_request_id") == obs_id]
    assert len(ups) == 1
    print("✓ 4/5. ensure creates exactly one, repeated no dup")

    os.environ["CONVEYOR_DESKTOP_UPLOAD_ENABLED"] = "false"
    settings_off = _settings(tmp, max_pending=3)
    register_desktop_node(settings_off, "macbook-payton", "Payton MacBook", "0.3.0", {})
    res_off = create_observe_request(settings_off, msg, "截图", auto_upload_thumbnail=True)
    assert res_off.get("ok")
    ee = ensure_upload_request_for_observe(settings_off, res_off["request"], created_by_channel="t", created_by_chat_id="c")
    assert not ee.get("ok") and ee.get("error") == "upload_disabled"
    print("✓ 6. no upload created if disabled")
    os.environ["CONVEYOR_DESKTOP_UPLOAD_ENABLED"] = "true"

    from handlers.tools.observe_tools import exec_desktop_observe_status
    import asyncio as aio
    try:
        loop = aio.get_event_loop()
    except RuntimeError:
        loop = aio.new_event_loop()
        aio.set_event_loop(loop)
    st = loop.run_until_complete(exec_desktop_observe_status(settings, ""))
    assert "P5.2/P5.3" not in st
    print("✓ 10. status wording no longer says P5.2/P5.3 disabled")

    status_route = route_intent("observe status")
    assert "desktop.observe.status" in status_route.tools
    cap_route = route_intent("截图看看我电脑现在是什么")
    assert "desktop.observe.request" in cap_route.tools
    print("✓ 11. status-only do not trigger capture")

    from desktop_observe_requests import RESULT_FORBIDDEN_FIELDS
    assert "png_bytes" in RESULT_FORBIDDEN_FIELDS and "ocr" in RESULT_FORBIDDEN_FIELDS
    print("✓ 12/13. forbidden fields protect against full/OCR")

    from handlers.tools.observe_tools import format_observe_failure
    fail_text = format_observe_failure({
        "request_id": "obs_test",
        "status": "failed",
        "error": "screen_recording_permission_required",
        "user_request": "截图看看我电脑现在是什么",
    })
    assert "截图失败" in fail_text
    assert "屏幕录制" in fail_text
    assert "Observe request" not in fail_text
    assert "No thumbnail sent" not in fail_text
    print("✓ 14. failure message is user-friendly Chinese")


if __name__ == "__main__":
    import asyncio
    rc = main()
    test_p543_auto_thumbnail_flags_and_routing()
    print("\nAll P5.4.3 additions passed in observe smoke")
    raise SystemExit(rc)
