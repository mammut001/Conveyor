#!/usr/bin/env python3
"""scripts/desktop_upload_request_smoke.py — smoke tests for P5.4 upload request store."""
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone

# Add parent directory to sys.path so we can import from top level
sys.path.append(str(Path(__file__).parent.parent.resolve()))

from config import load_settings
from channel.types import InboundMessage
from desktop_upload_requests import (
    create_upload_request,
    claim_upload_request,
    complete_upload_request,
    fail_upload_request,
    cancel_upload_request,
    load_upload_requests,
    save_upload_requests,
    list_pending_upload_requests,
    list_recent_upload_requests,
)

def run_tests():
    print("Running P5.4 upload request store smoke tests...")
    
    settings = load_settings()
    import dataclasses
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = dataclasses.replace(
            settings,
            codex_memory_root=tmp_path,
            conveyor_desktop_upload_enabled=True,
            conveyor_desktop_screenshot_dir=str(tmp_path / "desktop" / "screenshots"),
        )

        requests = load_upload_requests(settings)
        assert len(requests) == 0, f"Expected empty store, got {requests}"
        print("✓ Empty store load passes")

        observe_record = {
            "request_id": "obs_20260702T055402Z_84139ed4",
            "node_id": "macbook-payton",
            "status": "completed",
            "result": {
                "screenshot_id": "scr_20260702T055402Z_12345678",
                "path": "/local/path/to/screenshot.png",
                "sha256": "8a5c37b8d8...",
            }
        }

        inbound_msg = InboundMessage(
            message_id="msg_001",
            channel="feishu",
            chat_id="chat_123",
            operator_id="usr_abc",
            text="/observe_upload obs_20260702T055402Z_84139ed4",
        )

        create_res = create_upload_request(settings, observe_record, inbound_msg)
        assert create_res.get("ok"), f"Create upload request failed: {create_res}"
        upload_req = create_res["request"]
        upload_id = upload_req["upload_id"]
        assert upload_req["status"] == "pending"
        assert upload_req["screenshot_id"] == "scr_20260702T055402Z_12345678"
        print(f"✓ Create upload request passes: {upload_id}")

        pending = list_pending_upload_requests(settings, "macbook-payton")
        assert len(pending) == 1
        assert pending[0]["upload_id"] == upload_id
        print("✓ Pending upload request list passes")

        claim_res = claim_upload_request(settings, upload_id, "macbook-payton")
        assert claim_res.get("ok"), f"Claim failed: {claim_res}"
        assert claim_res["request"]["status"] == "claimed"
        print("✓ Claim upload request passes")

        thumbnail_meta = {
            "upload_id": upload_id,
            "thumbnail_path": "/vps/desktop/uploads/thumb.png",
            "sha256": "abcde12345abcde12345abcde12345abcde12345abcde12345abcde123456789",
            "bytes": 50000,
            "width": 1280,
            "height": 800,
            "created_at": "2026-07-02T12:00:00Z",
            "source_screenshot_id": "scr_20260702T055402Z_12345678",
            "node_id": "macbook-payton",
        }
        complete_res = complete_upload_request(settings, upload_id, "macbook-payton", thumbnail_meta)
        assert complete_res.get("ok"), f"Complete failed: {complete_res}"
        assert complete_res["request"]["status"] == "completed"
        assert complete_res["request"]["result"]["bytes"] == 50000
        print("✓ Complete upload request passes")

        bad_meta = {
            "upload_id": upload_id,
            "thumbnail_path": "/vps/desktop/uploads/thumb.png",
            "sha256": "abcde12345",
            "png_bytes": "this-is-binary-content-which-is-forbidden",
        }
        bad_res = complete_upload_request(settings, upload_id, "macbook-payton", bad_meta)
        assert not bad_res.get("ok")
        assert bad_res.get("error") == "invalid_result"
        print("✓ Forbidden fields validation passes (binary bytes rejected from json store)")

        cancel_res = cancel_upload_request(settings, upload_id)
        assert not cancel_res.get("ok")
        print("✓ Cancellation on completed request is correctly rejected")

        recent = list_recent_upload_requests(settings, limit=5)
        assert len(recent) == 1
        assert recent[0]["upload_id"] == upload_id
        print("✓ List recent upload requests passes")

        # ----------------------------------------------------
        # Atomic delivery tests
        # ----------------------------------------------------
        from desktop_upload_requests import (
            get_upload_request,
            mark_upload_delivered,
            mark_upload_delivery_failed,
        )
        
        # Test get_upload_request
        req_val = get_upload_request(settings, upload_id)
        assert req_val is not None
        assert req_val["upload_id"] == upload_id
        print("✓ get_upload_request passes")

        # Test mark_upload_delivered on completed request
        del_res = mark_upload_delivered(settings, upload_id, channel="telegram", chat_id="chat_123")
        assert del_res.get("ok"), f"mark_upload_delivered failed: {del_res}"
        assert del_res["request"]["delivered"] is True
        assert del_res["request"]["delivered_channel"] == "telegram"
        assert del_res["request"]["delivered_chat_id"] == "chat_123"
        print("✓ mark_upload_delivered on completed request passes")

        # Test mark_upload_delivered on non-existent request
        del_bad = mark_upload_delivered(settings, "upl_nonexistent")
        assert not del_bad.get("ok")
        assert del_bad.get("error") == "request_not_found"
        print("✓ mark_upload_delivered on non-existent request is correctly rejected")

        # Test mark_upload_delivery_failed
        # Create a new completed upload request to test failure marking
        observe_record_2 = {
            "request_id": "obs_20260702T055402Z_84139ed5",
            "node_id": "macbook-payton",
            "status": "completed",
            "result": {
                "screenshot_id": "scr_20260702T055402Z_12345679",
                "path": "/local/path/to/screenshot2.png",
                "sha256": "8a5c37b8d9...",
            }
        }
        create_res_2 = create_upload_request(settings, observe_record_2, inbound_msg)
        upload_req_2 = create_res_2["request"]
        upload_id_2 = upload_req_2["upload_id"]
        claim_upload_request(settings, upload_id_2, "macbook-payton")
        complete_upload_request(settings, upload_id_2, "macbook-payton", thumbnail_meta)
        
        fail_del = mark_upload_delivery_failed(settings, upload_id_2, "delivery_error_code", message="Failed to send photo")
        assert fail_del.get("ok")
        assert fail_del["request"]["delivery_failed"] is True
        assert fail_del["request"]["delivery_error"] == "delivery_error_code"
        assert fail_del["request"]["delivery_error_message"] == "Failed to send photo"
        print("✓ mark_upload_delivery_failed passes")

        # ----------------------------------------------------
        # Temp dir configuration tests
        # ----------------------------------------------------
        from handlers.tools.observe_tools import upload_temp_dir_configuration_error
        
        # Test relative temp dir
        settings_rel = dataclasses.replace(settings, conveyor_desktop_upload_temp_dir="relative/temp/path")
        err_rel = upload_temp_dir_configuration_error(settings_rel)
        assert err_rel is not None
        assert "must be an absolute path" in err_rel
        print("✓ Relative upload temp dir validation passes")

        # Test absolute temp dir
        settings_abs = dataclasses.replace(settings, conveyor_desktop_upload_temp_dir="/absolute/temp/path")
        err_abs = upload_temp_dir_configuration_error(settings_abs)
        assert err_abs is None
        print("✓ Absolute upload temp dir validation passes")

        # ----------------------------------------------------
        # Mac agent local screenshot source path validation tests
        # ----------------------------------------------------
        from desktop_agent import resolve_local_screenshot_source
        import os
        
        # Setup screenshots dir
        screenshot_dir = tmp_path / "desktop" / "screenshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        
        # Case 1: Valid screenshot and valid metadata
        valid_id = "scr_valid123"
        valid_png = screenshot_dir / f"{valid_id}.png"
        valid_json = screenshot_dir / f"{valid_id}.json"
        
        import hashlib
        png_data = b"\x89PNG\r\n\x1a\nvalid_data"
        valid_png.write_bytes(png_data)
        png_sha = hashlib.sha256(png_data).hexdigest()
        
        meta = {
            "screenshot_id": valid_id,
            "path": str(valid_png),
            "sha256": png_sha,
            "width": 100,
            "height": 100,
        }
        valid_json.write_text(json.dumps(meta), encoding="utf-8")
        
        resolved = resolve_local_screenshot_source(settings, valid_id)
        assert resolved == valid_png.resolve()
        print("✓ Local screenshot validation: valid metadata and PNG matches")

        # Case 2: Traversing/relative path in metadata (should reject)
        bad_id = "scr_bad_path"
        bad_json = screenshot_dir / f"{bad_id}.json"
        bad_meta = {
            "screenshot_id": bad_id,
            "path": "../../../etc/passwd",
            "sha256": "some-sha",
        }
        bad_json.write_text(json.dumps(bad_meta), encoding="utf-8")
        assert resolve_local_screenshot_source(settings, bad_id) is None
        print("✓ Local screenshot validation: relative path in metadata rejected")

        # Case 3: Symlink screenshot (should reject)
        symlink_id = "scr_symlink"
        symlink_png = screenshot_dir / f"{symlink_id}.png"
        target_file = tmp_path / "target.png"
        target_file.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        os.symlink(target_file, symlink_png)
        
        symlink_json = screenshot_dir / f"{symlink_id}.json"
        symlink_meta = {
            "screenshot_id": symlink_id,
            "path": str(symlink_png),
            "sha256": hashlib.sha256(b"\x89PNG\r\n\x1a\nfake").hexdigest(),
        }
        symlink_json.write_text(json.dumps(symlink_meta), encoding="utf-8")
        assert resolve_local_screenshot_source(settings, symlink_id) is None
        print("✓ Local screenshot validation: symlink PNG rejected")

        # Case 4: Non-existent files
        assert resolve_local_screenshot_source(settings, "nonexistent_id") is None
        print("✓ Local screenshot validation: non-existent ID rejected")

        # Case 5: SHA-256 mismatch
        mismatch_id = "scr_sha_mismatch"
        mismatch_png = screenshot_dir / f"{mismatch_id}.png"
        mismatch_json = screenshot_dir / f"{mismatch_id}.json"
        mismatch_png.write_bytes(b"\x89PNG\r\n\x1a\ndata")
        mismatch_meta = {
            "screenshot_id": mismatch_id,
            "path": str(mismatch_png),
            "sha256": "wrong-sha-value",
        }
        mismatch_json.write_text(json.dumps(mismatch_meta), encoding="utf-8")
        assert resolve_local_screenshot_source(settings, mismatch_id) is None
        print("✓ Local screenshot validation: SHA-256 mismatch rejected")

        # Case 6: Directory traversal via screenshot_id (should reject)
        assert resolve_local_screenshot_source(settings, "../state/desktop_upload_requests") is None
        print("✓ Local screenshot validation: traversing screenshot_id rejected")

        # Case 7: Fallback to screenshot_dir / f"{screenshot_id}.png" works if no metadata path specified
        fallback_id = "scr_fallback"
        fallback_png = screenshot_dir / f"{fallback_id}.png"
        fallback_png.write_bytes(b"\x89PNG\r\n\x1a\ndata")
        resolved_fb = resolve_local_screenshot_source(settings, fallback_id)
        assert resolved_fb == fallback_png.resolve()
        print("✓ Local screenshot validation: fallback works if no metadata file")

        # ----------------------------------------------------
        # P5.4.2 Delivery correctness tests
        # ----------------------------------------------------
        from desktop_upload_requests import (
            mark_upload_delivered,
            mark_upload_delivery_failed,
            reset_upload_delivery,
        )
        import asyncio

        # Build a minimal fake observe record for upload creation
        fake_obs_record = {
            "request_id": "obs_delivery_test_001",
            "node_id": "macbook-payton",
            "status": "completed",
            "created_at": "2026-07-02T12:00:00Z",
            "updated_at": "2026-07-02T12:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
            "user_request": "test",
            "result": {"screenshot_id": "scr_delivery_test_001"},
            "error": None,
        }
        fake_msg = type("FakeMsg", (), {"chat_id": "chat_delivery_test", "channel": "feishu", "operator_id": "op1"})()

        create_res = create_upload_request(settings, fake_obs_record, fake_msg)
        assert create_res.get("ok"), f"create failed: {create_res}"
        delivery_upload_id = create_res["request"]["upload_id"]

        # Advance to completed state
        claim_res = claim_upload_request(settings, delivery_upload_id, "macbook-payton")
        assert claim_res.get("ok")
        thumb_path = tmp_path / "desktop" / "uploads" / f"{delivery_upload_id}.png"
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        thumb_data = b"\x89PNG\r\n\x1a\ntest_thumbnail"
        thumb_path.write_bytes(thumb_data)
        thumb_sha = hashlib.sha256(thumb_data).hexdigest()
        complete_res = complete_upload_request(settings, delivery_upload_id, "macbook-payton", {
            "upload_id": delivery_upload_id,
            "source_screenshot_id": "scr_delivery_test_001",
            "thumbnail_path": str(thumb_path),
            "sha256": thumb_sha,
            "width": 100,
            "height": 100,
            "bytes": len(thumb_data),
        })
        assert complete_res.get("ok"), f"complete failed: {complete_res}"

        # Test 1: mark_upload_delivered correctly sets delivered=True and clears error fields
        deliver_res = mark_upload_delivered(settings, delivery_upload_id, channel="feishu", chat_id="chat_test")
        assert deliver_res.get("ok")
        record = deliver_res["request"]
        assert record["delivered"] is True
        assert record["delivered_channel"] == "feishu"
        assert record["delivered_chat_id"] == "chat_test"
        assert record["delivery_error"] is None
        assert record["delivery_failed_at"] is None
        print("✓ P5.4.2: mark_upload_delivered sets delivered=True and clears error fields")

        # Test 2: reset_upload_delivery clears all delivery fields
        reset_res = reset_upload_delivery(settings, delivery_upload_id)
        assert reset_res.get("ok")
        record = reset_res["request"]
        assert record["delivered"] is False
        assert record["delivered_at"] is None
        assert record["delivery_error"] is None
        assert record["delivery_failed_at"] is None
        print("✓ P5.4.2: reset_upload_delivery clears all delivery tracking fields")

        # Test 3: mark_upload_delivery_failed sets error fields and delivered=False
        fail_res = mark_upload_delivery_failed(
            settings, delivery_upload_id,
            "FeishuAPIError",
            message="upload_media returned 403"
        )
        assert fail_res.get("ok")
        record = fail_res["request"]
        assert record["delivered"] is False
        assert record["delivery_error"] == "FeishuAPIError"
        assert record["delivery_error_message"] == "upload_media returned 403"
        assert record["delivery_failed_at"] is not None
        print("✓ P5.4.2: mark_upload_delivery_failed records error and delivered=False")

        # Test 4: Fake port — send_image succeeds → upload_status marks delivered
        reset_upload_delivery(settings, delivery_upload_id)

        class FakeSuccessPort:
            send_image_called = False
            async def send_image(self, chat_id, image_path, *, caption=None):
                FakeSuccessPort.send_image_called = True

        from handlers.tools.observe_tools import exec_desktop_upload_status
        status_text = asyncio.get_event_loop().run_until_complete(
            exec_desktop_upload_status(settings, "", port=FakeSuccessPort(), msg=fake_msg)
        )
        assert FakeSuccessPort.send_image_called, "send_image should have been called"
        assert "delivered to chat" in status_text, f"Expected delivered status, got: {status_text}"
        print("✓ P5.4.2: send_image success → upload_status marks delivered")

        # Test 5: Fake port — send_image raises → upload_status marks delivery_failed, not delivered
        reset_upload_delivery(settings, delivery_upload_id)

        class FakeFailPort:
            send_image_called = False
            async def send_image(self, chat_id, image_path, *, caption=None):
                FakeFailPort.send_image_called = True
                raise RuntimeError("Feishu API error: 403 forbidden")

        status_text2 = asyncio.get_event_loop().run_until_complete(
            exec_desktop_upload_status(settings, "", port=FakeFailPort(), msg=fake_msg)
        )
        assert FakeFailPort.send_image_called, "send_image should have been called"
        assert "delivery failed" in status_text2, f"Expected delivery failed, got: {status_text2}"
        assert "RuntimeError" in status_text2, f"Expected error name, got: {status_text2}"
        print("✓ P5.4.2: send_image raises → upload_status marks delivery_failed, not delivered")

        # Test 6: Already-delivered upload is not re-sent by upload_status
        mark_upload_delivered(settings, delivery_upload_id, channel="feishu", chat_id="chat_test")

        class FakeResendPort:
            send_image_called = False
            async def send_image(self, chat_id, image_path, *, caption=None):
                FakeResendPort.send_image_called = True

        status_text3 = asyncio.get_event_loop().run_until_complete(
            exec_desktop_upload_status(settings, "", port=FakeResendPort(), msg=fake_msg)
        )
        assert not FakeResendPort.send_image_called, "send_image should NOT be called for already-delivered upload"
        print("✓ P5.4.2: already-delivered upload is not re-sent by upload_status")

        # Test 7: Missing thumbnail file → delivery_failed with thumbnail_missing
        reset_upload_delivery(settings, delivery_upload_id)
        thumb_path.unlink()  # Remove the thumbnail

        class FakeMissingThumbPort:
            send_image_called = False
            async def send_image(self, chat_id, image_path, *, caption=None):
                FakeMissingThumbPort.send_image_called = True

        status_text4 = asyncio.get_event_loop().run_until_complete(
            exec_desktop_upload_status(settings, "", port=FakeMissingThumbPort(), msg=fake_msg)
        )
        assert not FakeMissingThumbPort.send_image_called, "send_image should NOT be called when thumbnail missing"
        assert "thumbnail_missing" in status_text4, f"Expected thumbnail_missing, got: {status_text4}"
        print("✓ P5.4.2: missing thumbnail → delivery_failed with thumbnail_missing, send_image not called")

        # Test 8: /upload_resend re-delivers a delivery-failed request (after recreating thumb file)
        thumb_path.write_bytes(thumb_data)  # Recreate the thumbnail
        reset_upload_delivery(settings, delivery_upload_id)
        mark_upload_delivery_failed(settings, delivery_upload_id, "PreviousError")

        from handlers.tools.observe_tools import exec_desktop_upload_resend

        class FakeResendSuccessPort:
            send_image_called = False
            async def send_image(self, chat_id, image_path, *, caption=None):
                FakeResendSuccessPort.send_image_called = True

        resend_text = asyncio.get_event_loop().run_until_complete(
            exec_desktop_upload_resend(
                settings, delivery_upload_id,
                port=FakeResendSuccessPort(), msg=fake_msg
            )
        )
        assert FakeResendSuccessPort.send_image_called, "send_image should be called on resend"
        assert "delivered" in resend_text.lower(), f"Expected delivered in resend response, got: {resend_text}"
        print("✓ P5.4.2: upload_resend re-delivers a delivery-failed request")

        # P5.4.3 idempotent ensure
        from desktop_upload_requests import ensure_upload_request_for_observe
        os.environ["CONVEYOR_DESKTOP_UPLOAD_ENABLED"] = "true"
        settings = load_settings()
        obs_e = {"request_id": "obs_e_1", "node_id": "macbook-payton", "status": "completed", "result": {"screenshot_id": "s1"}}
        fm = type("M", (), {"chat_id": "c", "channel": "feishu", "operator_id": None})()
        r1 = ensure_upload_request_for_observe(settings, obs_e, created_by_channel="feishu", created_by_chat_id="c")
        assert r1.get("ok")
        u1 = r1["request"]["upload_id"]
        r2 = ensure_upload_request_for_observe(settings, obs_e, created_by_channel="feishu", created_by_chat_id="c")
        assert r2["request"]["upload_id"] == u1
        print("✓ P5.4.3: ensure idempotent no dup")

        os.environ["CONVEYOR_DESKTOP_UPLOAD_ENABLED"] = "false"
        s_off = load_settings()
        roff = ensure_upload_request_for_observe(s_off, obs_e, created_by_channel="f", created_by_chat_id="c")
        assert not roff.get("ok")
        print("✓ P5.4.3: ensure no create if upload disabled")

    print("\nAll upload request store tests PASSED!")

if __name__ == "__main__":
    import json
    run_tests()
