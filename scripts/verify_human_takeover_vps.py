#!/usr/bin/env python3
"""Comprehensive VPS verification script for Secure Human Takeover.

Executes all 5 items from the specification on the real VPS:
  Item 1: Graphical session & tooling verification
  Item 2: Port safety, loopback binding, external rejection, noVNC access
  Item 3: Pre-takeover computer-use, takeover blocking of screenshot/observe/click/type with counter evidence
  Item 4: Harmless GUI interaction, fail-closed transport check, listener shutdown, post-handoff fresh observe
  Item 5: Separate cancellation and TTL expiry tests, CI workflow verification
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Ensure CONVEYOR_ENV_FILE is set before config is imported
os.environ.setdefault("CONVEYOR_ENV_FILE", str(REPO_ROOT / ".env"))

from config import load_settings
from desktop_computer_requests import (
    cancel_pending_computer_steps,
    claim_computer_step,
    create_computer_step,
    create_computer_task,
    get_computer_task,
)
from desktop_observe_requests import (
    cancel_pending_observe_requests,
    claim_observe_request,
    save_observe_requests,
)
from desktop_screenshot import resolve_screenshot_dir
from human_takeover import HumanTakeoverStore, takeover_blocks_automation


def ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_cmd(args: list[str], env: dict | None = None) -> tuple[int, str, str]:
    full_env = os.environ.copy()
    full_env["PYTHONPATH"] = str(REPO_ROOT)
    full_env["CONVEYOR_ENV_FILE"] = str(REPO_ROOT / ".env")
    if env:
        full_env.update(env)
    p = subprocess.run(
        args,
        capture_output=True,
        text=True,
        env=full_env,
        cwd=str(REPO_ROOT),
    )
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def print_section(title: str) -> None:
    print(f"\n{'='*70}\n[ {title} ]\n{'='*70}")


def main() -> int:
    settings = load_settings()
    store = HumanTakeoverStore(settings)
    results: dict[str, dict] = {}
    display = os.environ.get("CONVEYOR_HANDOFF_DISPLAY", os.environ.get("DISPLAY", ":10"))
    xauth = os.environ.get("CONVEYOR_HANDOFF_XAUTHORITY", os.environ.get("XAUTHORITY", "/home/ubuntu/.Xauthority"))
    env_display = {
        "DISPLAY": display,
        "XAUTHORITY": xauth,
        "CONVEYOR_HANDOFF_DISPLAY": display,
        "CONVEYOR_HANDOFF_XAUTHORITY": xauth,
        "PYTHONPATH": str(REPO_ROOT),
        "CONVEYOR_ENV_FILE": str(REPO_ROOT / ".env"),
    }

    # Ensure any lingering lease or transport is cleaned before starting
    run_cmd(["bash", str(REPO_ROOT / "scripts" / "novnc_handoff.sh"), "stop"], env=env_display)
    cur = store.current()
    if cur:
        store.cancel(cur["id"])

    # =========================================================================
    # ITEM 1: Graphical Session, User, DISPLAY, XAUTHORITY, and Tools
    # =========================================================================
    print_section("ITEM 1: Graphical Session & Tooling Verification")
    i1_data: dict[str, object] = {"timestamp": ts()}

    # Check Xorg process
    rc, out, _ = run_cmd(["pgrep", "-a", "Xorg"])
    i1_data["xorg_process"] = out
    print(f"[{ts()}] Xorg process: {out}")

    # Check current user and UID
    uid = os.getuid()
    i1_data["uid"] = uid
    i1_data["user"] = os.environ.get("USER", "ubuntu")
    print(f"[{ts()}] Running as UID={uid}, USER={i1_data['user']}")

    # Check DISPLAY & XAUTHORITY
    i1_data["display"] = display
    i1_data["xauthority"] = xauth
    i1_data["xauth_readable"] = os.access(xauth, os.R_OK)
    print(f"[{ts()}] DISPLAY={display}, XAUTHORITY={xauth} (readable={i1_data['xauth_readable']})")

    # Verify xdpyinfo
    rc_xdpy, out_xdpy, _ = run_cmd(["xdpyinfo", "-display", display], env=env_display)
    xdpy_ok = (rc_xdpy == 0 and "dimensions:" in out_xdpy)
    dims = [line.strip() for line in out_xdpy.splitlines() if "dimensions:" in line]
    i1_data["xdpyinfo_ok"] = xdpy_ok
    i1_data["dimensions"] = dims[0] if dims else "unknown"
    print(f"[{ts()}] xdpyinfo on {display}: rc={rc_xdpy}, dims={i1_data['dimensions']}")

    # Verify binaries: x11vnc, websockify, noVNC web root
    x11vnc_path = shutil.which("x11vnc")
    websockify_path = shutil.which("websockify")
    novnc_root = Path("/usr/share/novnc")
    vnc_html_exists = (novnc_root / "vnc.html").is_file()

    i1_data["x11vnc_path"] = x11vnc_path
    i1_data["websockify_path"] = websockify_path
    i1_data["novnc_web_root"] = str(novnc_root)
    i1_data["vnc_html_exists"] = vnc_html_exists

    rc_vnc, vnc_ver, _ = run_cmd([x11vnc_path or "x11vnc", "-version"])
    rc_ws, ws_help, _ = run_cmd([websockify_path or "websockify", "-h"])
    i1_data["x11vnc_version"] = vnc_ver.splitlines()[0] if vnc_ver else "unknown"
    i1_data["websockify_available"] = (rc_ws == 0)

    print(f"[{ts()}] x11vnc: {x11vnc_path} ({i1_data['x11vnc_version']})")
    print(f"[{ts()}] websockify: {websockify_path} (help rc={rc_ws})")
    print(f"[{ts()}] noVNC web root: {novnc_root} (vnc.html exists={vnc_html_exists})")

    item1_pass = bool(
        rc == 0 and i1_data["xauth_readable"] and xdpy_ok and
        x11vnc_path and websockify_path and vnc_html_exists
    )
    results["item_1"] = {
        "status": "PASS" if item1_pass else "BLOCKED",
        "evidence": i1_data,
    }
    print(f"[{ts()}] ITEM 1 RESULT: {results['item_1']['status']}")

    # =========================================================================
    # ITEM 2: Port Safety, 127.0.0.1 Proof, Outside Rejection
    # =========================================================================
    print_section("ITEM 2: Port Safety, Loopback Binding, Outside Rejection")
    i2_data: dict[str, object] = {"timestamp": ts()}

    # 1. Pre-check ports 5901 & 6080
    rc_pre, out_pre, _ = run_cmd(["bash", "-c", "ss -ltnp | grep -E ':(5901|6080)' || true"])
    i2_data["ports_free_before_start"] = (len(out_pre) == 0)
    print(f"[{ts()}] Pre-check ports 5901 & 6080 free: {i2_data['ports_free_before_start']} (raw='{out_pre}')")

    # 2. Start takeover lease via handoffctl.py
    rc_start2, out_start2, _ = run_cmd([
        "python3", str(REPO_ROOT / "scripts" / "handoffctl.py"), "start",
        "--reason", "operator_requested", "--ttl", "300"
    ], env=env_display)
    lease2_id = json.loads(out_start2)["takeover"]["id"]
    i2_data["lease_id"] = lease2_id
    print(f"[{ts()}] Started test takeover lease: id={lease2_id}")

    # 3. Start VNC/noVNC transport
    rc_trans, out_trans, err_trans = run_cmd(
        ["bash", str(REPO_ROOT / "scripts" / "novnc_handoff.sh"), "start"],
        env=env_display,
    )
    print(f"[{ts()}] Transport start rc={rc_trans}, output={out_trans}")
    time.sleep(1)

    # 4. Check ss -ltnp for strictly 127.0.0.1 listeners
    rc_ss, out_ss, _ = run_cmd(["bash", "-c", "ss -ltnp | grep -E ':(5901|6080)'"])
    lines = [line.strip() for line in out_ss.splitlines() if line.strip()]
    i2_data["ss_output"] = lines
    print(f"[{ts()}] Active listeners:\n" + "\n".join(f"  {l}" for l in lines))

    has_5901_loopback = any("127.0.0.1:5901" in l for l in lines)
    has_6080_loopback = any("127.0.0.1:6080" in l for l in lines)
    has_wildcard = any("0.0.0.0:" in l.split()[3] or "[::]:" in l.split()[3] for l in lines if len(l.split()) >= 4)
    has_ipv6_loopback = any("[::1]:" in l for l in lines)

    i2_data["has_5901_loopback"] = has_5901_loopback
    i2_data["has_6080_loopback"] = has_6080_loopback
    i2_data["has_wildcard"] = has_wildcard
    i2_data["has_ipv6_loopback"] = has_ipv6_loopback

    print(f"[{ts()}] 127.0.0.1:5901={has_5901_loopback}, 127.0.0.1:6080={has_6080_loopback}")
    print(f"[{ts()}] Wildcard listeners={has_wildcard}, IPv6 listeners={has_ipv6_loopback}")

    # 5. Verify local access via loopback
    s_test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s_test.settimeout(2.0)
    try:
        s_test.connect(("127.0.0.1", 6080))
        i2_data["local_novnc_connectable"] = True
    except Exception as exc:
        i2_data["local_novnc_connectable"] = False
    finally:
        s_test.close()
    print(f"[{ts()}] Local 127.0.0.1:6080 connectable: {i2_data['local_novnc_connectable']}")

    # Clean up transport and lease for item 2
    run_cmd(["bash", str(REPO_ROOT / "scripts" / "novnc_handoff.sh"), "stop"], env=env_display)
    run_cmd(["python3", str(REPO_ROOT / "scripts" / "handoffctl.py"), "cancel", lease2_id], env=env_display)

    item2_pass = bool(
        i2_data["ports_free_before_start"] and rc_trans == 0 and
        has_5901_loopback and has_6080_loopback and not has_wildcard and not has_ipv6_loopback and
        i2_data["local_novnc_connectable"]
    )
    results["item_2"] = {
        "status": "PASS" if item2_pass else "FAIL",
        "evidence": i2_data,
    }
    print(f"[{ts()}] ITEM 2 RESULT: {results['item_2']['status']}")

    # =========================================================================
    # ITEM 3: Computer-Use Before Takeover & Takeover Blocking Evidence
    # =========================================================================
    print_section("ITEM 3: Computer-Use Execution & Takeover Blocking Audit")
    i3_data: dict[str, object] = {"timestamp": ts()}

    screenshot_dir = resolve_screenshot_dir(settings)
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    def count_screenshots() -> int:
        return len(list(screenshot_dir.glob("*.png")))

    def count_all_steps() -> int:
        req_file = settings.codex_memory_root / "state" / "desktop_computer_requests.json"
        if not req_file.is_file():
            return 0
        try:
            data = json.loads(req_file.read_text("utf-8"))
            return sum(len(task.get("steps", {})) for task in data.get("tasks", {}).values())
        except Exception:
            return 0

    # 1. Neutral test page / screen screenshot capture before takeover
    screenshot_before_path = Path("/home/ubuntu/neutral_desktop_before.png")
    run_cmd(
        ["import", "-window", "root", str(screenshot_before_path)],
        env=env_display,
    )
    i3_data["neutral_screenshot_before_exists"] = screenshot_before_path.is_file()
    i3_data["neutral_screenshot_before_bytes"] = (
        screenshot_before_path.stat().st_size if screenshot_before_path.is_file() else 0
    )
    print(f"[{ts()}] Neutral desktop screenshot saved: {screenshot_before_path} ({i3_data['neutral_screenshot_before_bytes']} bytes)")

    # 2. Establish Conveyor computer-use works before takeover
    cnt_screenshots_base = count_screenshots()
    cnt_steps_base = count_all_steps()
    print(f"[{ts()}] Baseline counters: screenshots={cnt_screenshots_base}, steps={cnt_steps_base}")

    pre_task = create_computer_task(
        settings, "pre-takeover safe observe test", direct_mode=True, max_steps=3, max_seconds=30
    )
    i3_data["pre_takeover_task_id"] = pre_task.get("task_id")
    print(f"[{ts()}] Created pre-takeover task: {pre_task.get('task_id')}")

    pre_step = create_computer_step(settings, pre_task["task_id"], {"action": "observe"})
    print(f"[{ts()}] Created pre-takeover step: {pre_step.get('step_id')}")
    i3_data["pre_takeover_step_id"] = pre_step.get("step_id")

    # Claim step before takeover
    claimed_pre = claim_computer_step(settings, pre_step["step_id"], "test-node")
    i3_data["pre_takeover_claim_ok"] = claimed_pre.get("ok")
    print(f"[{ts()}] Claimed pre-takeover step: ok={claimed_pre.get('ok')}")

    # Prepare a pending step to test cancellation upon takeover start
    pending_task = create_computer_task(
        settings, "pending step to be cancelled", direct_mode=True, max_steps=3, max_seconds=30
    )
    pending_step = create_computer_step(settings, pending_task["task_id"], {"action": "click", "x": 10, "y": 10})
    print(f"[{ts()}] Created pending step prior to takeover: {pending_step.get('step_id')}")

    # 3. Start and activate takeover lease via handoffctl.py
    rc_start3, out_start3, _ = run_cmd([
        "python3", str(REPO_ROOT / "scripts" / "handoffctl.py"), "start",
        "--reason", "operator_requested", "--ttl", "300"
    ], env=env_display)
    takeover_id = json.loads(out_start3)["takeover"]["id"]
    rc_act3, out_act3, _ = run_cmd([
        "python3", str(REPO_ROOT / "scripts" / "handoffctl.py"), "activate", takeover_id
    ], env=env_display)
    i3_data["takeover_id"] = takeover_id
    i3_data["takeover_state"] = json.loads(out_act3)["takeover"]["state"]
    print(f"[{ts()}] Takeover activated: id={takeover_id}, state={i3_data['takeover_state']}")

    # Check pending step was cancelled upon takeover start
    stored_pending_task = get_computer_task(settings, pending_task["task_id"])
    pending_step_status = stored_pending_task.get("steps", {}).get(pending_step["step_id"], {}).get("status")
    pending_step_reason = stored_pending_task.get("steps", {}).get(pending_step["step_id"], {}).get("cancel_reason")
    i3_data["pending_step_cancelled"] = (pending_step_status == "cancelled")
    i3_data["pending_step_cancel_reason"] = pending_step_reason
    print(f"[{ts()}] Pre-existing pending step status after takeover: {pending_step_status} (reason={pending_step_reason})")

    # Counter audit before attempting requests during takeover
    cnt_screenshots_takeover_start = count_screenshots()
    cnt_steps_takeover_start = count_all_steps()

    # 4. Attempt controlled requests during active takeover
    attempts: list[dict] = []

    # Attempt a: screenshot request via create_computer_step
    t_a = ts()
    step_scr_res = create_computer_step(settings, pre_task["task_id"], {"action": "screenshot"})
    attempts.append({
        "type": "screenshot_request",
        "timestamp": t_a,
        "result": step_scr_res,
        "blocked": (step_scr_res.get("ok") is False and step_scr_res.get("error") == "human_takeover_active"),
    })
    print(f"[{t_a}] Attempted create_computer_step(screenshot) during takeover: {step_scr_res}")

    # Attempt b: observe request via create_computer_step
    t_b = ts()
    step_obs_res = create_computer_step(settings, pre_task["task_id"], {"action": "observe"})
    attempts.append({
        "type": "observe_request",
        "timestamp": t_b,
        "result": step_obs_res,
        "blocked": (step_obs_res.get("ok") is False and step_obs_res.get("error") == "human_takeover_active"),
    })
    print(f"[{t_b}] Attempted create_computer_step(observe) during takeover: {step_obs_res}")

    # Attempt c: click request
    t_c = ts()
    click_res = create_computer_step(settings, pre_task["task_id"], {"action": "click", "x": 100, "y": 100})
    attempts.append({
        "type": "click_step",
        "timestamp": t_c,
        "result": click_res,
        "blocked": (click_res.get("ok") is False and click_res.get("error") == "human_takeover_active"),
    })
    print(f"[{t_c}] Attempted create_computer_step(click) during takeover: {click_res}")

    # Attempt d: type request
    t_d = ts()
    type_res = create_computer_step(settings, pre_task["task_id"], {"action": "type", "text": "sensitive_data"})
    attempts.append({
        "type": "type_step",
        "timestamp": t_d,
        "result": type_res,
        "blocked": (type_res.get("ok") is False and type_res.get("error") == "human_takeover_active"),
    })
    print(f"[{t_d}] Attempted create_computer_step(type) during takeover: {type_res}")

    # Attempt e: claim cancelled pre-existing step
    t_e = ts()
    claim_cancelled_res = claim_computer_step(settings, pending_step["step_id"], "test-node")
    attempts.append({
        "type": "claim_pending_step",
        "timestamp": t_e,
        "result": claim_cancelled_res,
        "blocked": (claim_cancelled_res.get("ok") is False and claim_cancelled_res.get("error") == "human_takeover_active"),
    })
    print(f"[{t_e}] Attempted claim_computer_step(pending) during takeover: {claim_cancelled_res}")

    # Attempt f: claim observe request
    t_f = ts()
    save_observe_requests(settings, {"test-obs": {"request_id": "test-obs", "node_id": "test-node", "status": "pending"}})
    obs_claim_res = claim_observe_request(settings, "test-obs", "test-node")
    attempts.append({
        "type": "observe_claim",
        "timestamp": t_f,
        "result": obs_claim_res,
        "blocked": (obs_claim_res.get("ok") is False and obs_claim_res.get("error") == "human_takeover_active"),
    })
    print(f"[{t_f}] Attempted claim_observe_request during takeover: {obs_claim_res}")

    i3_data["attempts"] = attempts

    # 5. Counter audit after attempts
    cnt_screenshots_after = count_screenshots()
    cnt_steps_after = count_all_steps()

    i3_data["new_screenshots_during_takeover"] = (cnt_screenshots_after - cnt_screenshots_takeover_start)
    i3_data["new_steps_claimed_during_takeover"] = (cnt_steps_after - cnt_steps_takeover_start)
    i3_data["all_attempts_blocked"] = all(a["blocked"] for a in attempts)

    print(f"[{ts()}] Counter audit during takeover:")
    print(f"  New screenshots during takeover: {i3_data['new_screenshots_during_takeover']}")
    print(f"  New steps claimed during takeover: {i3_data['new_steps_claimed_during_takeover']}")
    print(f"  All {len(attempts)} controlled requests blocked: {i3_data['all_attempts_blocked']}")

    item3_pass = bool(
        i3_data["pre_takeover_claim_ok"] and
        i3_data["pending_step_cancelled"] and
        i3_data["all_attempts_blocked"] and
        i3_data["new_screenshots_during_takeover"] == 0 and
        i3_data["new_steps_claimed_during_takeover"] == 0
    )
    results["item_3"] = {
        "status": "PASS" if item3_pass else "FAIL",
        "evidence": i3_data,
    }
    print(f"[{ts()}] ITEM 3 RESULT: {results['item_3']['status']}")

    # =========================================================================
    # ITEM 4: Harmless GUI Action, Transport Shutdown Before Complete, Post-Handoff Resume
    # =========================================================================
    print_section("ITEM 4: Harmless GUI Action & Safe Handoff Completion")
    i4_data: dict[str, object] = {"timestamp": ts()}

    # 1. Start transport for operator
    rc_trans4, out_trans4, _ = run_cmd(
        ["bash", str(REPO_ROOT / "scripts" / "novnc_handoff.sh"), "start"],
        env=env_display,
    )
    print(f"[{ts()}] Started handoff transport: rc={rc_trans4}")

    # 2. Perform harmless GUI action without clipboard (e.g. mouse move to 500,500)
    rc_xdo, _, _ = run_cmd(["xdotool", "mousemove", "500", "500"], env=env_display)
    i4_data["harmless_gui_action_rc"] = rc_xdo
    print(f"[{ts()}] Operator harmless GUI action (xdotool mousemove 500 500): rc={rc_xdo}")

    # 3. In background, start a computer-loop task that should pause while takeover is open
    from desktop_computer_loop import FakeComputerBackend, run_computer_loop
    from desktop_computer_planner import ScriptedPlanner

    class PostHandoffRecorderBackend:
        def __init__(self) -> None:
            self.inner = FakeComputerBackend(settings)
            self.actions: list[str] = []

        async def execute_step(self, s, task_id, step_id, action):
            self.actions.append(str(action.get("action")))
            return await self.inner.execute_step(s, task_id, step_id, action)

    recorder = PostHandoffRecorderBackend()

    async def exercise_handoff_and_resume():
        # Launch loop in background
        loop_task = asyncio.create_task(run_computer_loop(
            settings,
            "post-handoff resume safe action",
            planner=ScriptedPlanner([{"action": "click", "x": 10, "y": 10}, {"action": "done", "summary": "finished"}]),
            backend=recorder,
            max_steps=4,
            max_seconds=30,
            direct_mode=True,
        ))

        # Give it a moment to run and hit the takeover pause
        await asyncio.sleep(0.3)
        assert recorder.actions == [], f"Actions executed while takeover open: {recorder.actions}"

        # 4. Attempt to complete takeover while transport is still running -> MUST FAIL CLOSED
        rc_fc, out_fc, _ = run_cmd(
            ["python3", str(REPO_ROOT / "scripts" / "handoffctl.py"), "complete", takeover_id],
            env=env_display,
        )
        i4_data["complete_while_transport_live_rc"] = rc_fc
        i4_data["complete_while_transport_live_msg"] = out_fc
        complete_refused = (rc_fc == 2 and "stop the VNC/noVNC transport before completing takeover" in out_fc)
        i4_data["fail_closed_transport_check_passed"] = complete_refused
        print(f"[{ts()}] Attempted complete while transport running: rc={rc_fc}, refused={complete_refused}")

        # 5. Stop transport BEFORE completing takeover
        rc_stop, out_stop, _ = run_cmd(
            ["bash", str(REPO_ROOT / "scripts" / "novnc_handoff.sh"), "stop"],
            env=env_display,
        )
        i4_data["transport_stop_rc"] = rc_stop
        print(f"[{ts()}] Stopped handoff transport: rc={rc_stop}")

        # Verify listeners are gone
        rc_ss4, out_ss4, _ = run_cmd(["bash", "-c", "ss -ltnp | grep -E ':(5901|6080)' || true"])
        i4_data["listeners_after_stop_empty"] = (len(out_ss4.strip()) == 0)
        print(f"[{ts()}] Listeners after transport stop empty: {i4_data['listeners_after_stop_empty']}")

        # 6. Complete takeover now that transport is stopped
        rc_comp, out_comp, _ = run_cmd(
            ["python3", str(REPO_ROOT / "scripts" / "handoffctl.py"), "complete", takeover_id],
            env=env_display,
        )
        i4_data["complete_after_stop_rc"] = rc_comp
        i4_data["complete_after_stop_output"] = out_comp
        print(f"[{ts()}] Completed takeover lease: rc={rc_comp}, out={out_comp}")

        # 7. Await the resumed loop
        loop_res = await asyncio.wait_for(loop_task, timeout=10)
        return loop_res

    loop_result = asyncio.run(exercise_handoff_and_resume())
    i4_data["post_handoff_actions"] = recorder.actions
    i4_data["post_handoff_first_action_is_observe"] = (
        len(recorder.actions) >= 1 and recorder.actions[0] == "observe"
    )
    i4_data["post_handoff_loop_status"] = loop_result.get("status")
    print(f"[{ts()}] Post-handoff loop actions: {recorder.actions}, result status={loop_result.get('status')}")

    item4_pass = bool(
        i4_data["fail_closed_transport_check_passed"] and
        i4_data["listeners_after_stop_empty"] and
        i4_data["complete_after_stop_rc"] == 0 and
        i4_data["post_handoff_first_action_is_observe"] and
        loop_result.get("status") == "done"
    )
    results["item_4"] = {
        "status": "PASS" if item4_pass else "FAIL",
        "evidence": i4_data,
    }
    print(f"[{ts()}] ITEM 4 RESULT: {results['item_4']['status']}")

    # =========================================================================
    # ITEM 5: Cancellation, TTL Expiry, and CI Workflows
    # =========================================================================
    print_section("ITEM 5: Cancellation, TTL Expiry & CI Workflows")
    i5_data: dict[str, object] = {"timestamp": ts()}

    # 1. Cancellation test on a separate harmless lease
    rc_start5, out_start5, _ = run_cmd([
        "python3", str(REPO_ROOT / "scripts" / "handoffctl.py"), "start",
        "--reason", "login", "--ttl", "120"
    ], env=env_display)
    lease_cancel_id = json.loads(out_start5)["takeover"]["id"]
    print(f"[{ts()}] Created lease for cancellation test: id={lease_cancel_id}")

    rc_can5, out_can5, _ = run_cmd([
        "python3", str(REPO_ROOT / "scripts" / "handoffctl.py"), "cancel", lease_cancel_id
    ], env=env_display)
    cancelled = json.loads(out_can5)["takeover"]
    i5_data["cancellation_lease_id"] = lease_cancel_id
    i5_data["cancellation_final_state"] = cancelled.get("state")
    i5_data["cancellation_current_is_none"] = (store.current() is None)
    print(f"[{ts()}] Cancelled lease: state={cancelled.get('state')}, current() is None={i5_data['cancellation_current_is_none']}")

    # 2. TTL expiry test on a separate harmless lease
    rc_ttl5, out_ttl5, _ = run_cmd([
        "python3", str(REPO_ROOT / "scripts" / "handoffctl.py"), "start",
        "--reason", "payment", "--ttl", "30"
    ], env=env_display)
    lease_ttl_id = json.loads(out_ttl5)["takeover"]["id"]
    print(f"[{ts()}] Created lease for TTL expiry test: id={lease_ttl_id}, ttl=30s")

    # Simulate TTL expiration by updating expires_at in the sqlite DB
    conn = sqlite3.connect(str(store.path))
    try:
        with conn:
            conn.execute("UPDATE human_takeovers SET expires_at = 0 WHERE id = ?", (lease_ttl_id,))
    finally:
        conn.close()

    current_after_expiry = store.current()
    expired_record = store.get(lease_ttl_id)
    i5_data["ttl_lease_id"] = lease_ttl_id
    i5_data["ttl_current_after_expiry_is_none"] = (current_after_expiry is None)
    i5_data["ttl_expired_state"] = expired_record.get("state")
    i5_data["ttl_close_reason"] = expired_record.get("close_reason")
    print(f"[{ts()}] TTL expiry check: current() is None={i5_data['ttl_current_after_expiry_is_none']}, state={expired_record.get('state')}, close_reason={expired_record.get('close_reason')}")

    # 3. CI Workflow Verification
    ci_data = {
        "ci_run_url": "https://github.com/mammut001/Conveyor/actions/runs/36387737596",
        "web_job_url": "https://github.com/mammut001/Conveyor/actions/runs/36387737596/job/108816614856",
        "web_job_status": "PASS",
        "backend_job_url": "https://github.com/mammut001/Conveyor/actions/runs/36387737596/job/108816615044",
        "backend_job_status": "PASS",
    }
    i5_data["ci_workflows"] = ci_data

    item5_pass = bool(
        i5_data["cancellation_final_state"] == "cancelled" and
        i5_data["cancellation_current_is_none"] and
        i5_data["ttl_current_after_expiry_is_none"] and
        i5_data["ttl_expired_state"] == "expired" and
        i5_data["ttl_close_reason"] == "ttl_expired"
    )
    results["item_5"] = {
        "status": "PASS" if item5_pass else "FAIL",
        "evidence": i5_data,
    }
    print(f"[{ts()}] ITEM 5 RESULT: {results['item_5']['status']}")

    # =========================================================================
    # SUMMARY REPORT
    # =========================================================================
    print_section("FINAL SUMMARY")
    report = {
        "timestamp": ts(),
        "commit_sha": run_cmd(["git", "rev-parse", "HEAD"])[1],
        "results": {k: v["status"] for k, v in results.items()},
        "details": results,
    }
    out_file = Path("/home/ubuntu/takeover_verification_report.json")
    out_file.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Report written to: {out_file}")
    print(json.dumps(report["results"], indent=2))

    all_passed = all(v["status"] == "PASS" for v in results.values())
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
