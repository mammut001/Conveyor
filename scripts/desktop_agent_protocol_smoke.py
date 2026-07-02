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
        if "desktop agent online, control not enabled" not in text_comp or "Screenshot, mouse" not in text_comp:
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

    finally:
        # Shutdown server
        print("Shutting down test server...")
        httpd.shutdown()
        httpd.server_close()
        server_thread.join()

    total_tests = 16
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
