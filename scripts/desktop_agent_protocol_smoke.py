#!/usr/bin/env python3
"""desktop_agent_protocol_smoke.py — integration test suite for P5.1 desktop agent protocol."""
from __future__ import annotations

import os
import sys
import time
import json
import urllib.request
import urllib.error
import threading
import asyncio
from pathlib import Path

# Bootstrap project root on sys.path
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Force neutral/mock environment values
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["TELEGRAM_ALLOWED_USER_ID"] = "1"
os.environ["CODEX_WORKSPACE_ROOT"] = "/tmp/test_nodes_workspace"
os.environ["CODEX_MEMORY_ROOT"] = "/tmp/test_nodes_memory"
os.environ["CONVEYOR_DESKTOP_NODE_ENABLED"] = "true"
os.environ["CONVEYOR_DESKTOP_AGENT_TOKEN"] = "my-secret-token"
os.environ["CONVEYOR_DESKTOP_NODE_ID"] = "macbook-payton"
os.environ["CONVEYOR_DESKTOP_NODE_NAME"] = "Payton MacBook"
# Use a very short TTL (2 seconds) to test expiration without long sleeps
os.environ["CONVEYOR_DESKTOP_HEARTBEAT_TTL_SECONDS"] = "2"

# Start background server
server_host = "127.0.0.1"
server_port = 18766
os.environ["CONVEYOR_DESKTOP_AGENT_SERVER_HOST"] = server_host
os.environ["CONVEYOR_DESKTOP_AGENT_SERVER_PORT"] = str(server_port)

import desktop_agent_server
from config import Settings
from nodes.registry import list_nodes
from nodes.types import NodeStatus, NodeType
from nodes.state import get_desktop_runtime
from handlers.tools.executors import exec_nodes_status, exec_computer_status
from channel.feishu_cards import node_status_card
from handlers.intent import route_intent

FAILURES: list[str] = []


def _fail(name: str, detail: str) -> None:
    print(f"[fail] {name}: {detail}")
    FAILURES.append(name)


def main() -> int:
    # Spin up ThreadingHTTPServer in background thread
    from desktop_agent_server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer((server_host, server_port), desktop_agent_server.DesktopAgentHTTPHandler)

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    print(f"Test server started at http://{server_host}:{server_port}")

    # Ensure clean state file initially
    from nodes.state import desktop_state_path
    init_path = desktop_state_path(desktop_agent_server.settings)
    if init_path.exists():
        try:
            init_path.unlink()
        except Exception:
            pass

    try:
        # 1. Default registry has only vps-main (if desktop disabled)
        print("Testing default registry with desktop disabled...")
        disabled_settings = Settings(
            telegram_bot_token="test-token",
            telegram_allowed_user_id=1,
            codex_workspace_root=Path("/tmp"),
            codex_bin="codex",
            codex_task_root=Path("/tmp/t"),
            codex_model=None,
            codex_timeout_seconds=3,
            telegram_progress_seconds=3,
            codex_retry_429_delays_seconds=(),
            codex_memory_root=Path("/tmp/m"),
            user_timezone="UTC",
            conveyor_desktop_node_enabled=False,
        )
        nodes = list_nodes(disabled_settings)
        if len(nodes) != 1 or nodes[0].node_id != "vps-main":
            _fail("vps_only_when_disabled", f"nodes={nodes}")
        else:
            print("[pass] vps_only_when_disabled")

        # 2. Desktop enabled but no heartbeat -> offline
        print("Testing desktop enabled but no heartbeat (offline status)...")
        nodes = list_nodes(desktop_agent_server.settings)
        desktop_node = [n for n in nodes if n.node_type == NodeType.DESKTOP]
        if not desktop_node or desktop_node[0].status != NodeStatus.OFFLINE:
            _fail("desktop_offline_initially", f"desktop_node={desktop_node}")
        else:
            print("[pass] desktop_offline_initially")

        # Prepare HTTP client variables
        reg_url = f"http://{server_host}:{server_port}/desktop/register"
        hb_url = f"http://{server_host}:{server_port}/desktop/heartbeat"
        status_url = f"http://{server_host}:{server_port}/desktop/status"
        headers = {
            "Authorization": "Bearer my-secret-token",
            "Content-Type": "application/json",
        }
        reg_body = {
            "node_id": "macbook-payton",
            "display_name": "Payton MacBook",
            "agent_version": "0.1.0",
            "host": {
                "platform": "macOS",
                "hostname": "Paytons-MacBook",
                "arch": "arm64"
            }
        }

        # 3. Register desktop node -> online
        print("Testing desktop node registration...")
        req = urllib.request.Request(reg_url, data=json.dumps(reg_body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req) as resp:
            res_data = json.loads(resp.read().decode())
            if not res_data.get("ok") or res_data.get("status") != "online":
                _fail("registration_response", f"res_data={res_data}")
            else:
                print("[pass] registration_response")

        nodes = list_nodes(desktop_agent_server.settings)
        desktop_node = [n for n in nodes if n.node_type == NodeType.DESKTOP]
        if not desktop_node or desktop_node[0].status != NodeStatus.ONLINE:
            _fail("desktop_online_after_reg", f"desktop_node={desktop_node}")
        else:
            print("[pass] desktop_online_after_reg")

        # 4. Heartbeat refreshes last_seen
        print("Testing heartbeat updates last_seen...")
        state1 = get_desktop_runtime(desktop_agent_server.settings, "macbook-payton")
        if not state1:
            _fail("heartbeat_state_retrieval", "no state retrieved")
            return 1
        last_seen1 = state1["last_seen_at"]

        time.sleep(0.5)

        hb_body = {
            "node_id": "macbook-payton",
            "agent_state": "idle",
            "last_action": "heartbeat_test"
        }
        req = urllib.request.Request(hb_url, data=json.dumps(hb_body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req) as resp:
            res_data = json.loads(resp.read().decode())
            if not res_data.get("ok") or res_data.get("status") != "online":
                _fail("heartbeat_response", f"res_data={res_data}")
            else:
                print("[pass] heartbeat_response")

        state2 = get_desktop_runtime(desktop_agent_server.settings, "macbook-payton")
        if not state2 or state2["last_seen_at"] <= last_seen1:
            _fail("heartbeat_refreshes_time", f"last_seen1={last_seen1}, last_seen2={state2.get('last_seen_at') if state2 else None}")
        else:
            print("[pass] heartbeat_refreshes_time")

        # 5. TTL expiration -> offline
        print("Testing heartbeat TTL expiration...")
        time.sleep(2.1)
        nodes = list_nodes(desktop_agent_server.settings)
        desktop_node = [n for n in nodes if n.node_type == NodeType.DESKTOP]
        if not desktop_node or desktop_node[0].status != NodeStatus.OFFLINE:
            _fail("desktop_offline_after_ttl", f"desktop_node={desktop_node}")
        else:
            print("[pass] desktop_offline_after_ttl")

        # 6. Invalid token rejected
        print("Testing invalid token rejection...")
        bad_headers = {
            "Authorization": "Bearer bad-token",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(reg_url, data=json.dumps(reg_body).encode(), headers=bad_headers, method="POST")
        try:
            urllib.request.urlopen(req)
            _fail("invalid_token_accepted", "request succeeded when it should fail with 401")
        except urllib.error.HTTPError as e:
            if e.code != 401:
                _fail("invalid_token_error_code", f"status={e.code}")
            else:
                print("[pass] invalid_token_rejected")

        # 7. Missing token rejected
        print("Testing missing token rejection...")
        no_auth_headers = {
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(reg_url, data=json.dumps(reg_body).encode(), headers=no_auth_headers, method="POST")
        try:
            urllib.request.urlopen(req)
            _fail("missing_token_accepted", "request succeeded when it should fail with 401")
        except urllib.error.HTTPError as e:
            if e.code != 401:
                _fail("missing_token_error_code", f"status={e.code}")
            else:
                print("[pass] missing_token_rejected")

        # 8. Disabled desktop node rejected
        print("Testing disabled desktop node rejection...")
        # Temporarily mock desktop node disabled
        orig_settings = desktop_agent_server.settings
        desktop_agent_server.settings = disabled_settings
        try:
            req = urllib.request.Request(reg_url, data=json.dumps(reg_body).encode(), headers=headers, method="POST")
            urllib.request.urlopen(req)
            _fail("disabled_node_accepted", "request succeeded when it should fail with 403")
        except urllib.error.HTTPError as e:
            if e.code != 403:
                _fail("disabled_node_error_code", f"status={e.code}")
            else:
                print("[pass] disabled_node_rejected")
        finally:
            desktop_agent_server.settings = orig_settings

        # 9. /nodes includes runtime status
        print("Testing /nodes formats online status correctly...")
        # Register again to make it online
        req = urllib.request.Request(reg_url, data=json.dumps(reg_body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req) as resp:
            pass

        text = asyncio.run(exec_nodes_status(desktop_agent_server.settings, ""))
        if "macbook-payton" not in text or "online" not in text or ("agent_state: idle" not in text and "agent_state: registered" not in text) or "last_seen" not in text:
            _fail("nodes_status_online_text", f"text={text}")
        else:
            print("[pass] nodes_status_online_text")

        # 10. computer.status does not claim screenshot/control works
        print("Testing computer.status output...")
        text_comp = asyncio.run(exec_computer_status(desktop_agent_server.settings, ""))
        if "desktop agent online, control not enabled" not in text_comp or "not implemented" not in text_comp.lower():
            _fail("computer_status_online_text", f"text={text_comp}")
        else:
            print("[pass] computer_status_online_text")

        # 11. Feishu node status card includes online/offline state
        print("Testing Feishu card content...")
        card = node_status_card(text)
        card_text = card["elements"][0]["content"]
        if "online" not in card_text or ("agent_state: idle" not in card_text and "agent_state: registered" not in card_text):
            _fail("feishu_card_content", f"card_text={card_text}")
        else:
            print("[pass] feishu_card_content")

        # 12. NL phrase MacBook 在线吗 routes to computer.status or nodes.status
        print("Testing MacBook 在线吗 routing...")
        res_nl1 = route_intent("MacBook 在线吗")
        if res_nl1.kind != "deterministic" or not ({"nodes.status", "computer.status"} & set(res_nl1.tools)):
            _fail("route_macbook_online", f"result={res_nl1}")
        else:
            print("[pass] route_macbook_online")

        # 13. NL phrase 帮我在 Mac 上打开 Xcode routes to computer.status
        print("Testing 帮我在 Mac 上打开 Xcode routing...")
        res_nl2 = route_intent("帮我在 Mac 上打开 Xcode")
        if res_nl2.kind != "deterministic" or "computer.status" not in res_nl2.tools:
            _fail("route_open_xcode_mac", f"result={res_nl2}")
        else:
            print("[pass] route_open_xcode_mac")

        # 14. Test cross-process JSON file state details
        print("Testing cross-process JSON file state details...")
        from nodes.state import load_desktop_state
        state_file_path = desktop_state_path(desktop_agent_server.settings)
        if not state_file_path.exists():
            _fail("json_file_creation", "JSON state file does not exist")
        else:
            print("[pass] json_file_creation")
            
        # Verify JSON content
        with open(state_file_path, "r", encoding="utf-8") as f:
            state_data = json.load(f)
        if "macbook-payton" not in state_data:
            _fail("json_content_missing_node", "macbook-payton not in JSON file")
        else:
            node_data = state_data["macbook-payton"]
            # Enforce: no token, secrets, headers
            forbidden_keys = {"token", "Authorization", "my-secret-token", "headers"}
            found_forbidden = [k for k in forbidden_keys if k in node_data or any(k in str(v) for v in node_data.values())]
            if found_forbidden:
                _fail("json_secrets_leak", f"Found forbidden elements: {found_forbidden}")
            else:
                print("[pass] json_secrets_leak_checked")
                
        # Test corrupt JSON handling
        print("Testing corrupt JSON handling...")
        # Write corrupted JSON
        with open(state_file_path, "w", encoding="utf-8") as f:
            f.write("{invalid_json:")
        loaded = load_desktop_state(desktop_agent_server.settings)
        if loaded != {}:
            _fail("corrupt_json_not_empty", f"loaded={loaded}")
        else:
            # list_nodes should still succeed and fallback to offline node (stub)
            try:
                nodes_after_corrupt = list_nodes(desktop_agent_server.settings)
                desktop_node = [n for n in nodes_after_corrupt if n.node_type == NodeType.DESKTOP]
                if not desktop_node or desktop_node[0].status != NodeStatus.OFFLINE:
                    _fail("corrupt_json_fallback", f"status={desktop_node[0].status if desktop_node else None}")
                else:
                    print("[pass] corrupt_json_does_not_crash")
            except Exception as e:
                _fail("corrupt_json_crash", f"raised: {e}")

        # 19. Register with wrong node_id
        print("Testing register with wrong node_id...")
        bad_reg_body = reg_body.copy()
        bad_reg_body["node_id"] = "wrong-macbook"
        req = urllib.request.Request(reg_url, data=json.dumps(bad_reg_body).encode(), headers=headers, method="POST")
        try:
            urllib.request.urlopen(req)
            _fail("wrong_node_id_register_accepted", "register with wrong node_id succeeded when it should fail with 400")
        except urllib.error.HTTPError as e:
            if e.code != 400:
                _fail("wrong_node_id_register_error_code", f"status={e.code}")
            else:
                body_err = json.loads(e.read().decode())
                if body_err.get("expected_node_id") != "macbook-payton" or body_err.get("error") != "node_id mismatch":
                    _fail("wrong_node_id_register_response", f"body={body_err}")
                else:
                    print("[pass] register_wrong_node_id_rejected")

        # 20. Heartbeat with wrong node_id
        print("Testing heartbeat with wrong node_id...")
        bad_hb_body = hb_body.copy()
        bad_hb_body["node_id"] = "wrong-macbook"
        req = urllib.request.Request(hb_url, data=json.dumps(bad_hb_body).encode(), headers=headers, method="POST")
        try:
            urllib.request.urlopen(req)
            _fail("wrong_node_id_heartbeat_accepted", "heartbeat with wrong node_id succeeded when it should fail with 400")
        except urllib.error.HTTPError as e:
            if e.code != 400:
                _fail("wrong_node_id_heartbeat_error_code", f"status={e.code}")
            else:
                body_err = json.loads(e.read().decode())
                if body_err.get("expected_node_id") != "macbook-payton" or body_err.get("error") != "node_id mismatch":
                    _fail("wrong_node_id_heartbeat_response", f"body={body_err}")
                else:
                    print("[pass] heartbeat_wrong_node_id_rejected")

        # 21. Oversized body (> 16 KB)
        print("Testing oversized body rejection...")
        large_body = {
            "node_id": "macbook-payton",
            "display_name": "Payton MacBook",
            "agent_version": "0.1.0",
            "host": {
                "platform": "macOS",
                "hostname": "A" * 20000, # make body larger than 16 KB
                "arch": "arm64"
            }
        }
        req = urllib.request.Request(reg_url, data=json.dumps(large_body).encode(), headers=headers, method="POST")
        try:
            urllib.request.urlopen(req)
            _fail("oversized_body_accepted", "oversized body succeeded when it should fail with 413")
        except urllib.error.HTTPError as e:
            if e.code != 413:
                _fail("oversized_body_error_code", f"status={e.code}")
            else:
                print("[pass] oversized_body_rejected")

        # 22. Malformed JSON
        print("Testing malformed JSON rejection...")
        req = urllib.request.Request(reg_url, data=b"{malformed_json_bytes", headers=headers, method="POST")
        try:
            urllib.request.urlopen(req)
            _fail("malformed_json_accepted", "malformed JSON succeeded when it should fail with 400")
        except urllib.error.HTTPError as e:
            if e.code != 400:
                _fail("malformed_json_error_code", f"status={e.code}")
            else:
                print("[pass] malformed_json_rejected")

        # 23. Host payload is sanitized
        print("Testing host payload sanitization...")
        host_extra_body = {
            "node_id": "macbook-payton",
            "display_name": "Payton MacBook",
            "agent_version": "0.1.0",
            "host": {
                "platform": "macOS",
                "hostname": "Paytons-MacBook",
                "arch": "arm64",
                "extra_bad_field": "some-value",
                "another_ignored_field": 123
            }
        }
        # First register with the extra field
        req = urllib.request.Request(reg_url, data=json.dumps(host_extra_body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req) as resp:
            pass
        # Load from state and check host content
        runtime_state = get_desktop_runtime(desktop_agent_server.settings, "macbook-payton")
        host_saved = runtime_state.get("host", {})
        if "extra_bad_field" in host_saved or "another_ignored_field" in host_saved:
            _fail("host_sanitization_failed", f"Ignored fields were saved: {host_saved}")
        elif host_saved.get("platform") != "macOS" or host_saved.get("hostname") != "Paytons-MacBook":
            _fail("host_sanitization_data_loss", f"Valid fields were not saved correctly: {host_saved}")
        else:
            print("[pass] host_payload_sanitized")

        # 24. POST /desktop/observe/request returns 501
        print("Testing observe request external creation disabled...")
        observe_req_url = f"http://{server_host}:{server_port}/desktop/observe/request"
        req = urllib.request.Request(
            observe_req_url,
            data=json.dumps({"node_id": "macbook-payton"}).encode(),
            headers=headers,
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
            _fail("observe_request_external", "expected 501")
        except urllib.error.HTTPError as e:
            if e.code != 501:
                _fail("observe_request_external", f"status={e.code}")
            else:
                print("[pass] observe_request_external_disabled")

        # 25–28. Observe pending / claim / complete / fail
        print("Testing observe pending/claim/complete/fail flow...")
        from desktop_observe_requests import save_observe_requests

        observe_store_path = (
            desktop_agent_server.settings.codex_memory_root
            / "state"
            / "desktop_observe_requests.json"
        )
        observe_store_path.parent.mkdir(parents=True, exist_ok=True)
        test_request_id = "obs_20260702T120000Z_test1234"
        save_observe_requests(desktop_agent_server.settings, {
            test_request_id: {
                "request_id": test_request_id,
                "node_id": "macbook-payton",
                "status": "pending",
                "created_at": "2026-07-02T12:00:00Z",
                "updated_at": "2026-07-02T12:00:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
                "user_request": "test observe",
                "result": None,
                "error": None,
            },
        })

        pending_url = (
            f"http://{server_host}:{server_port}/desktop/observe/pending"
            "?node_id=macbook-payton"
        )
        req = urllib.request.Request(pending_url, headers=headers, method="GET")
        with urllib.request.urlopen(req) as resp:
            pending_data = json.loads(resp.read().decode())
        if not pending_data.get("ok") or not pending_data.get("requests"):
            _fail("observe_pending", f"body={pending_data}")
        elif pending_data["requests"][0]["request_id"] != test_request_id:
            _fail("observe_pending", f"requests={pending_data['requests']}")
        else:
            print("[pass] observe_pending")

        claim_url = f"http://{server_host}:{server_port}/desktop/observe/claim"
        claim_body = {"request_id": test_request_id, "node_id": "macbook-payton"}
        req = urllib.request.Request(
            claim_url, data=json.dumps(claim_body).encode(), headers=headers, method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            claim_data = json.loads(resp.read().decode())
        if not claim_data.get("ok") or claim_data.get("request", {}).get("status") != "claimed":
            _fail("observe_claim", f"body={claim_data}")
        else:
            print("[pass] observe_claim")

        complete_url = f"http://{server_host}:{server_port}/desktop/observe/complete"
        complete_body = {
            "request_id": test_request_id,
            "node_id": "macbook-payton",
            "result": {
                "screenshot_id": "shot-test",
                "path": "/tmp/shot-test.png",
                "metadata_path": "/tmp/shot-test.json",
                "sha256": "a" * 64,
                "width": 100,
                "height": 50,
                "display_id": 1,
                "created_at": "2026-07-02T12:01:00Z",
                "bytes": 1234,
                "helper_version": "0.1.0",
            },
        }
        req = urllib.request.Request(
            complete_url, data=json.dumps(complete_body).encode(), headers=headers, method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            complete_data = json.loads(resp.read().decode())
        if not complete_data.get("ok") or complete_data.get("request", {}).get("status") != "completed":
            _fail("observe_complete", f"body={complete_data}")
        else:
            print("[pass] observe_complete")

        fail_request_id = "obs_20260702T120100Z_fail1234"
        save_observe_requests(desktop_agent_server.settings, {
            fail_request_id: {
                "request_id": fail_request_id,
                "node_id": "macbook-payton",
                "status": "pending",
                "created_at": "2026-07-02T12:01:00Z",
                "updated_at": "2026-07-02T12:01:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
                "user_request": "fail test",
                "result": None,
                "error": None,
            },
        })
        req = urllib.request.Request(
            claim_url, data=json.dumps({
                "request_id": fail_request_id, "node_id": "macbook-payton",
            }).encode(), headers=headers, method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            pass
        fail_url = f"http://{server_host}:{server_port}/desktop/observe/fail"
        fail_body = {
            "request_id": fail_request_id,
            "node_id": "macbook-payton",
            "error": "screen_recording_permission_required",
            "message": "Screen Recording permission is required.",
        }
        req = urllib.request.Request(
            fail_url, data=json.dumps(fail_body).encode(), headers=headers, method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            fail_data = json.loads(resp.read().decode())
        if not fail_data.get("ok") or fail_data.get("request", {}).get("status") != "failed":
            _fail("observe_fail", f"body={fail_data}")
        else:
            print("[pass] observe_fail")

        # 29. Complete rejects base64 in result
        print("Testing observe complete rejects image fields...")
        bad_request_id = "obs_20260702T120200Z_bad1234"
        save_observe_requests(desktop_agent_server.settings, {
            bad_request_id: {
                "request_id": bad_request_id,
                "node_id": "macbook-payton",
                "status": "claimed",
                "created_at": "2026-07-02T12:02:00Z",
                "updated_at": "2026-07-02T12:02:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
                "user_request": "bad result",
                "result": None,
                "error": None,
            },
        })
        bad_complete = {
            "request_id": bad_request_id,
            "node_id": "macbook-payton",
            "result": {
                "screenshot_id": "shot-bad",
                "path": "/tmp/shot-bad.png",
                "sha256": "b" * 64,
                "base64": "data",
            },
        }
        req = urllib.request.Request(
            complete_url, data=json.dumps(bad_complete).encode(), headers=headers, method="POST",
        )
        try:
            urllib.request.urlopen(req)
            _fail("observe_complete_rejects_base64", "expected conflict")
        except urllib.error.HTTPError as e:
            if e.code != 409:
                _fail("observe_complete_rejects_base64", f"status={e.code}")
            else:
                print("[pass] observe_complete_rejects_base64")

        # 30-38. Upload pending / claim / fail / complete validations
        print("Testing upload integration endpoints...")
        from desktop_upload_requests import save_upload_requests
        test_upload_id = "upl_20260702T120000Z_test1234"
        save_upload_requests(desktop_agent_server.settings, {
            test_upload_id: {
                "upload_id": test_upload_id,
                "screenshot_id": "scr_test_screenshot_id_123",
                "node_id": "macbook-payton",
                "status": "pending",
                "created_at": "2026-07-02T12:00:00Z",
                "updated_at": "2026-07-02T12:00:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
                "max_width": 1280,
                "max_height": 800,
                "max_bytes": 750000,
                "delivered": False,
                "result": None,
                "error": None,
            }
        })

        # 30. GET /desktop/upload/pending
        pending_upload_url = f"http://{server_host}:{server_port}/desktop/upload/pending?node_id=macbook-payton"
        req = urllib.request.Request(pending_upload_url, headers=headers, method="GET")
        with urllib.request.urlopen(req) as resp:
            pending_up_data = json.loads(resp.read().decode())
        if not pending_up_data.get("ok") or not pending_up_data.get("requests") or pending_up_data["requests"][0]["upload_id"] != test_upload_id:
            _fail("upload_pending", f"body={pending_up_data}")
        else:
            print("[pass] upload_pending")

        # 31. POST /desktop/upload/claim
        claim_upload_url = f"http://{server_host}:{server_port}/desktop/upload/claim"
        req = urllib.request.Request(
            claim_upload_url, data=json.dumps({"upload_id": test_upload_id, "node_id": "macbook-payton"}).encode(),
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            claim_up_data = json.loads(resp.read().decode())
        if not claim_up_data.get("ok") or claim_up_data.get("request", {}).get("status") != "claimed":
            _fail("upload_claim", f"body={claim_up_data}")
        else:
            print("[pass] upload_claim")

        # Enable upload settings for the complete test
        import dataclasses
        orig_upload_enabled = desktop_agent_server.settings.conveyor_desktop_upload_enabled
        desktop_agent_server.settings = dataclasses.replace(
            desktop_agent_server.settings,
            conveyor_desktop_upload_enabled=True,
        )

        # 32. POST /desktop/upload/complete disabled check
        desktop_agent_server.settings = dataclasses.replace(
            desktop_agent_server.settings,
            conveyor_desktop_upload_enabled=False,
        )
        import hashlib
        png_data = b"\x89PNG\r\n\x1a\nvalid_png_content"
        png_sha = hashlib.sha256(png_data).hexdigest()
        complete_up_url = (
            f"http://{server_host}:{server_port}/desktop/upload/complete"
            f"?upload_id={test_upload_id}&node_id=macbook-payton"
            f"&sha256={png_sha}&bytes={len(png_data)}&width=100&height=100"
        )
        req = urllib.request.Request(complete_up_url, data=png_data, headers={**headers, "Content-Type": "image/png", "Content-Length": str(len(png_data))}, method="POST")
        try:
            urllib.request.urlopen(req)
            _fail("upload_complete_disabled", "expected forbidden")
        except urllib.error.HTTPError as e:
            if e.code != 403:
                _fail("upload_complete_disabled", f"status={e.code}")
            else:
                print("[pass] upload_complete_disabled")
        desktop_agent_server.settings = dataclasses.replace(
            desktop_agent_server.settings,
            conveyor_desktop_upload_enabled=True,
        )

        # 33. POST /desktop/upload/complete invalid png check (no PNG magic)
        bad_png_data = b"bad_content_no_png_magic"
        bad_png_sha = hashlib.sha256(bad_png_data).hexdigest()
        bad_complete_up_url = (
            f"http://{server_host}:{server_port}/desktop/upload/complete"
            f"?upload_id={test_upload_id}&node_id=macbook-payton"
            f"&sha256={bad_png_sha}&bytes={len(bad_png_data)}&width=100&height=100"
        )
        req = urllib.request.Request(bad_complete_up_url, data=bad_png_data, headers={**headers, "Content-Type": "image/png", "Content-Length": str(len(bad_png_data))}, method="POST")
        try:
            urllib.request.urlopen(req)
            _fail("upload_complete_invalid_png", "expected bad request")
        except urllib.error.HTTPError as e:
            if e.code != 400:
                _fail("upload_complete_invalid_png", f"status={e.code}")
            else:
                print("[pass] upload_complete_invalid_png")

        # 34. POST /desktop/upload/complete traversal check
        traversal_up_url = (
            f"http://{server_host}:{server_port}/desktop/upload/complete"
            f"?upload_id=upl_../test&node_id=macbook-payton"
            f"&sha256={png_sha}&bytes={len(png_data)}&width=100&height=100"
        )
        req = urllib.request.Request(traversal_up_url, data=png_data, headers={**headers, "Content-Type": "image/png", "Content-Length": str(len(png_data))}, method="POST")
        try:
            urllib.request.urlopen(req)
            _fail("upload_complete_traversal", "expected bad request")
        except urllib.error.HTTPError as e:
            if e.code != 400:
                _fail("upload_complete_traversal", f"status={e.code}")
            else:
                print("[pass] upload_complete_traversal")

        # 35. POST /desktop/upload/complete sha mismatch check
        sha_mismatch_up_url = (
            f"http://{server_host}:{server_port}/desktop/upload/complete"
            f"?upload_id={test_upload_id}&node_id=macbook-payton"
            f"&sha256=wrong_sha_value&bytes={len(png_data)}&width=100&height=100"
        )
        req = urllib.request.Request(sha_mismatch_up_url, data=png_data, headers={**headers, "Content-Type": "image/png", "Content-Length": str(len(png_data))}, method="POST")
        try:
            urllib.request.urlopen(req)
            _fail("upload_complete_sha_mismatch", "expected bad request")
        except urllib.error.HTTPError as e:
            if e.code != 400:
                _fail("upload_complete_sha_mismatch", f"status={e.code}")
            else:
                print("[pass] upload_complete_sha_mismatch")

        # 36. POST /desktop/upload/complete success
        req = urllib.request.Request(complete_up_url, data=png_data, headers={**headers, "Content-Type": "image/png", "Content-Length": str(len(png_data))}, method="POST")
        with urllib.request.urlopen(req) as resp:
            complete_up_data = json.loads(resp.read().decode())
        if not complete_up_data.get("ok") or complete_up_data.get("request", {}).get("status") != "completed":
            _fail("upload_complete_success", f"body={complete_up_data}")
        else:
            print("[pass] upload_complete_success")

        # 37. Verify that source_screenshot_id is correctly mapped to screenshot_id (not upload_id)
        saved_req_record = complete_up_data.get("request", {})
        saved_result = saved_req_record.get("result", {})
        if saved_result.get("source_screenshot_id") != "scr_test_screenshot_id_123":
            _fail("upload_complete_source_screenshot_id_fixed", f"source_screenshot_id={saved_result.get('source_screenshot_id')}")
        else:
            print("[pass] upload_complete_source_screenshot_id_fixed")

        # Restore upload enabled settings
        desktop_agent_server.settings = dataclasses.replace(
            desktop_agent_server.settings,
            conveyor_desktop_upload_enabled=orig_upload_enabled,
        )

        # 38. POST /desktop/upload/fail
        fail_upload_id = "upl_20260702T120100Z_fail1234"
        save_upload_requests(desktop_agent_server.settings, {
            fail_upload_id: {
                "upload_id": fail_upload_id,
                "screenshot_id": "scr_124",
                "node_id": "macbook-payton",
                "status": "pending",
                "created_at": "2026-07-02T12:01:00Z",
                "updated_at": "2026-07-02T12:01:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
                "max_width": 1280,
                "max_height": 800,
                "max_bytes": 750000,
                "delivered": False,
                "result": None,
                "error": None,
            }
        })
        # claim first
        req = urllib.request.Request(
            claim_upload_url, data=json.dumps({"upload_id": fail_upload_id, "node_id": "macbook-payton"}).encode(),
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            pass
        # fail it
        fail_upload_url = f"http://{server_host}:{server_port}/desktop/upload/fail"
        fail_up_body = {
            "upload_id": fail_upload_id,
            "node_id": "macbook-payton",
            "error": "invalid_screenshot_source",
            "message": "Local screenshot source validation failed.",
        }
        req = urllib.request.Request(
            fail_upload_url, data=json.dumps(fail_up_body).encode(), headers=headers, method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            fail_up_data = json.loads(resp.read().decode())
        if not fail_up_data.get("ok") or fail_up_data.get("request", {}).get("status") != "failed":
            _fail("upload_fail", f"body={fail_up_data}")
        else:
            print("[pass] upload_fail")

    finally:
        # Shutdown server
        print("Shutting down test server...")
        httpd.shutdown()
        httpd.server_close()
        server_thread.join()

    total_tests = 38
    failed = len(FAILURES)
    passed = total_tests - failed
    print(f"\n{'=' * 60}")
    print(f"Protocol smoke: {passed}/{total_tests} passed")
    if FAILURES:
        print(f"FAILURES: {', '.join(FAILURES)}")
        return 1
    print("All tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
