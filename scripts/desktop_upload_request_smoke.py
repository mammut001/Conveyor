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

    print("\nAll upload request store tests PASSED!")

if __name__ == "__main__":
    run_tests()
