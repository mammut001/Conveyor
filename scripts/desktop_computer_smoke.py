#!/usr/bin/env python3
"""desktop_computer_smoke.py — env-free smokes for P5.6 Direct Computer Use.

Covers (all without real Cua / network):

  Config
  - All computer-use flags default to safe-off (disabled, http backend).
  - Cua driver probe handles a missing binary gracefully (no exception).

  Direct-mode gating
  - /computer_arm (TTL) enables direct mode.
  - An expired arm blocks direct mode.
  - CONVEYOR_COMPUTER_ALWAYS_DIRECT=true bypasses arm (only when the flag is set).

  Action schema + safety
  - is_action_allowed enforces the configured allow-list.
  - contains_blocked_keyword stops a task mid-loop.
  - run_computer_loop honors max_steps hard cap.
  - run_computer_loop honors a stop_check (the /computer_stop path).

  Execution (FakeComputerBackend)
  - A run completes, stores a redacted trajectory, and uses the fake
    Cua transport (no subprocess / network).
  - Pending-step listing is metadata-only; claim returns the executable
    action once and leaves only redacted data in the store.

  Kill switch
  - cancel_computer_task flips a running task to stopped.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Minimal env so importing config.py does not require a real .env.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/test_computer_workspace")
os.environ.setdefault("CODEX_MEMORY_ROOT", "/tmp/test_computer_memory")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "1")

from scripts.harness_common import CheckResult, print_results  # noqa: E402

FAILURES: list[str] = []


def _fail(name: str, detail: str) -> None:
    print(f"[fail] {name}: {detail}")
    FAILURES.append(name)


def _mk_settings(**over) -> "Settings":
    """Build a Settings with a fresh temp memory root + computer overrides.

    Defaults enable both USE and DIRECT so existing smokes exercise the
    armed/always-direct paths. Override either flag to test gating.
    """
    from config import Settings

    root = Path(tempfile.mkdtemp(prefix="conv_computer_"))
    kwargs = dict(
        telegram_bot_token="t",
        telegram_allowed_user_id=1,
        codex_workspace_root=root,
        codex_bin="codex",
        codex_task_root=root / "task",
        codex_model=None,
        codex_timeout_seconds=3600,
        codex_retry_429_delays_seconds=(),
        telegram_progress_seconds=3,
        codex_memory_root=root,
        user_timezone="UTC",
        conveyor_computer_use_enabled=True,
        conveyor_computer_direct_enabled=True,
        conveyor_computer_backend="fake",
        conveyor_computer_max_steps=20,
        conveyor_computer_max_seconds=600,
        conveyor_cua_driver_cmd="cua-driver mcp",
        conveyor_computer_allowed_actions=(
            "observe", "click", "type", "hotkey", "scroll", "wait",
        ),
        conveyor_computer_blocked_keywords=(
            "password", "passcode", "bank", "payment", "crypto",
            "keychain", "system settings", "delete account",
        ),
        conveyor_computer_blocked_apps=(
            "Keychain Access", "System Settings",
        ),
    )
    kwargs.update(over)
    return Settings(**kwargs)


# ---- Config defaults ------------------------------------------------------


def _test_config_defaults_disabled() -> None:
    from config import _load_codex_fields

    fields = _load_codex_fields("/dev/null")
    checks = {
        "conveyor_computer_use_enabled": False,
        "conveyor_computer_direct_enabled": False,
        "conveyor_computer_always_direct": False,
        "conveyor_computer_max_steps": 20,
        "conveyor_computer_max_seconds": 600,
        "conveyor_cua_driver_cmd": "cua-driver mcp",
        "conveyor_computer_backend": "http",
    }
    for key, expected in checks.items():
        if key not in fields:
            _fail("config_defaults_disabled", f"missing {key}")
            return
        if fields[key] != expected:
            _fail("config_defaults_disabled", f"{key}={fields[key]!r} expected {expected!r}")
            return
    # Defaults must be tuples (stable, hashable).
    if not isinstance(fields.get("conveyor_computer_allowed_actions"), tuple):
        _fail("config_defaults_disabled", "allowed_actions not tuple")
        return
    if not isinstance(fields.get("conveyor_computer_blocked_keywords"), tuple):
        _fail("config_defaults_disabled", "blocked_keywords not tuple")
        return
    print("[pass] config_defaults_disabled")


# ---- Driver probe ---------------------------------------------------------


def _test_missing_driver_graceful() -> None:
    from desktop_cua import build_driver, probe_cua_driver

    probe = probe_cua_driver("definitely-not-a-cua-binary-xyz")
    if probe.get("available") is not False:
        _fail("missing_driver_graceful", f"expected available=False, got {probe}")
        return
    if "error" not in probe:
        _fail("missing_driver_graceful", f"missing error field: {probe}")
        return
    # status() must still work (metadata-only) without raising.
    settings = _mk_settings(conveyor_cua_driver_cmd="definitely-not-a-cua-binary-xyz mcp")
    driver = build_driver(settings, fake=False, node_id="mac-1")
    status = driver.status()
    if status.get("available") is not False:
        _fail("missing_driver_graceful", f"status.available={status.get('available')}")
        return
    if status.get("mode") != "local_cua":
        _fail("missing_driver_graceful", f"mode={status.get('mode')}")
        return
    print("[pass] missing_driver_graceful")


def _test_local_cli_transport_maps_cua_tools() -> None:
    import json

    from desktop_cua import build_driver

    root = Path(tempfile.mkdtemp(prefix="conv_fake_cua_cli_"))
    log_path = root / "calls.jsonl"
    fake_driver = root / "cua-driver"
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
    fake_driver.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"LOG = {str(log_path)!r}\n"
        f"PNG = {png_b64!r}\n"
        "if len(sys.argv) > 1 and sys.argv[1] == '--version':\n"
        "    print('fake-cua-driver 1.0')\n"
        "    raise SystemExit(0)\n"
        "with open(LOG, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:], ensure_ascii=False) + '\\n')\n"
        "if len(sys.argv) >= 3 and sys.argv[1] == 'call':\n"
        "    tool = sys.argv[2]\n"
        "    if tool == 'screenshot':\n"
        "        print(json.dumps({'screenshot_png_b64': PNG}))\n"
        "    elif tool == 'set_config':\n"
        "        print(json.dumps({'capture_scope': 'desktop'}))\n"
        "    elif tool == 'click':\n"
        "        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}\n"
        "        print(json.dumps({'clicked': True, 'args': args}))\n"
        "    else:\n"
        "        print(json.dumps({'ok': True}))\n"
        "    raise SystemExit(0)\n"
        "print('bad invocation', file=sys.stderr)\n"
        "raise SystemExit(64)\n",
        encoding="utf-8",
    )
    fake_driver.chmod(fake_driver.stat().st_mode | stat.S_IXUSR)

    settings = _mk_settings(conveyor_cua_driver_cmd=f"{fake_driver} mcp")
    driver = build_driver(settings, fake=False, node_id="mac-test")
    obs = driver.execute({"action": "observe"})
    if not obs.get("result_ok") or not str(obs.get("screenshot_id", "")).endswith(obs.get("screenshot_id", "")):
        _fail("local_cli_transport_maps_cua_tools", f"bad observe result: {obs}")
        return
    if not obs.get("sha256") or obs.get("width") != 1 or obs.get("height") != 1:
        _fail("local_cli_transport_maps_cua_tools", f"bad screenshot metadata: {obs}")
        return
    click = driver.execute({"action": "click", "x": 12, "y": 34})
    if not click.get("result_ok"):
        _fail("local_cli_transport_maps_cua_tools", f"bad click result: {click}")
        return
    calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    if ["call", "screenshot"] not in calls:
        _fail("local_cli_transport_maps_cua_tools", f"screenshot call missing: {calls}")
        return
    click_calls = [c for c in calls if len(c) >= 3 and c[:2] == ["call", "click"]]
    if not click_calls:
        _fail("local_cli_transport_maps_cua_tools", f"click call missing: {calls}")
        return
    click_args = json.loads(click_calls[-1][2])
    if click_args.get("scope") != "desktop" or click_args.get("x") != 12.0 or click_args.get("y") != 34.0:
        _fail("local_cli_transport_maps_cua_tools", f"bad click args: {click_args}")
        return
    print("[pass] local_cli_transport_maps_cua_tools")


def _test_local_cli_transport_desktop_state_fallback() -> None:
    import json

    from desktop_cua import build_driver

    root = Path(tempfile.mkdtemp(prefix="conv_fake_cua_desktop_"))
    log_path = root / "calls.jsonl"
    fake_driver = root / "cua-driver"
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
    fake_driver.write_text(
        "#!/usr/bin/env python3\n"
        "import base64, json, sys\n"
        f"LOG = {str(log_path)!r}\n"
        f"PNG = {png_b64!r}\n"
        "if len(sys.argv) > 1 and sys.argv[1] == '--version':\n"
        "    print('fake-cua-driver 1.0')\n"
        "    raise SystemExit(0)\n"
        "with open(LOG, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:], ensure_ascii=False) + '\\n')\n"
        "if len(sys.argv) >= 3 and sys.argv[1] == 'call':\n"
        "    tool = sys.argv[2]\n"
        "    args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}\n"
        "    if tool == 'screenshot':\n"
        "        print('Unknown tool: screenshot', file=sys.stderr)\n"
        "        raise SystemExit(64)\n"
        "    if tool == 'set_config':\n"
        "        print(json.dumps({'capture_scope': 'desktop'}))\n"
        "        raise SystemExit(0)\n"
        "    if tool == 'get_desktop_state':\n"
        "        path = args['screenshot_out_file']\n"
        "        open(path, 'wb').write(base64.b64decode(PNG))\n"
        "        print(json.dumps({'screenshot_file_path': path, 'screenshot_width': 1, 'screenshot_height': 1}))\n"
        "        raise SystemExit(0)\n"
        "print('bad invocation', file=sys.stderr)\n"
        "raise SystemExit(64)\n",
        encoding="utf-8",
    )
    fake_driver.chmod(fake_driver.stat().st_mode | stat.S_IXUSR)
    settings = _mk_settings(conveyor_cua_driver_cmd=f"{fake_driver} mcp")
    driver = build_driver(settings, fake=False, node_id="mac-test")
    obs = driver.execute({"action": "observe"})
    if not obs.get("result_ok") or obs.get("width") != 1 or obs.get("height") != 1:
        _fail("local_cli_transport_desktop_state_fallback", f"bad observe result: {obs}")
        return
    calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    if not any(c[:2] == ["call", "get_desktop_state"] for c in calls):
        _fail("local_cli_transport_desktop_state_fallback", f"fallback missing: {calls}")
        return
    print("[pass] local_cli_transport_desktop_state_fallback")


# ---- Direct-mode gating ---------------------------------------------------


def _test_arm_ttl_enables_direct() -> None:
    from desktop_computer_requests import (
        arm_direct_mode,
        disarm_direct_mode,
        is_direct_mode_active,
    )

    settings = _mk_settings(conveyor_computer_always_direct=False)
    disarm_direct_mode(settings)
    if is_direct_mode_active(settings):
        _fail("arm_ttl_enables_direct", "active before arm")
        return
    arm_direct_mode(settings, 30)
    if not is_direct_mode_active(settings):
        _fail("arm_ttl_enables_direct", "not active after arm")
        return
    print("[pass] arm_ttl_enables_direct")


def _test_expired_arm_blocks() -> None:
    from datetime import datetime as _dt

    from desktop_computer_requests import (
        arm_direct_mode,
        is_direct_mode_active,
    )

    settings = _mk_settings(conveyor_computer_always_direct=False)
    arm_direct_mode(settings, 1)
    if not is_direct_mode_active(settings):
        _fail("expired_arm_blocks", "should be active immediately after 1-min arm")
        return
    # Simulate clock far in the future -> arm expired.
    future = _dt.now(timezone.utc) + timedelta(hours=2)
    if is_direct_mode_active(settings, now=future):
        _fail("expired_arm_blocks", "arm still active after expiry")
        return
    print("[pass] expired_arm_blocks")


def _test_always_direct_bypass() -> None:
    from desktop_computer_requests import (
        arm_direct_mode,
        direct_mode_source,
        disarm_direct_mode,
        is_direct_mode_active,
    )

    # always_direct=true + use + direct -> active without any arm.
    s_on = _mk_settings(
        conveyor_computer_always_direct=True,
        conveyor_computer_direct_enabled=True,
    )
    disarm_direct_mode(s_on)
    if not is_direct_mode_active(s_on):
        _fail("always_direct_bypass", "always_direct not active without arm")
        return
    if direct_mode_source(s_on) != "always":
        _fail("always_direct_bypass", f"source={direct_mode_source(s_on)}")
        return

    # always_direct=false, no arm -> inactive.
    s_off = _mk_settings(conveyor_computer_always_direct=False)
    disarm_direct_mode(s_off)
    if is_direct_mode_active(s_off):
        _fail("always_direct_bypass", "inactive expected without arm")
        return

    # always_direct=false, armed -> active via armed source.
    arm_direct_mode(s_off, 30)
    if not is_direct_mode_active(s_off):
        _fail("always_direct_bypass", "armed path not active")
        return
    if direct_mode_source(s_off) != "armed":
        _fail("always_direct_bypass", f"armed source={direct_mode_source(s_off)}")
        return
    print("[pass] always_direct_bypass")


# ---- Action schema + safety ---------------------------------------------


def _test_action_schema_allowlist() -> None:
    from desktop_computer_requests import is_action_allowed

    settings = _mk_settings(
        conveyor_computer_allowed_actions=("observe", "click"),
    )
    if not is_action_allowed(settings, {"action": "observe"}):
        _fail("action_schema_allowlist", "observe should be allowed")
        return
    if is_action_allowed(settings, {"action": "type"}):
        _fail("action_schema_allowlist", "type should be disallowed")
        return
    if is_action_allowed(settings, {"action": "explode"}):
        _fail("action_schema_allowlist", "explode should be disallowed")
        return
    print("[pass] action_schema_allowlist")


def _test_blocked_keyword_stops_task() -> None:
    import asyncio

    from desktop_computer_loop import FakeComputerBackend, run_computer_loop
    from desktop_computer_planner import ScriptedPlanner
    from desktop_computer_requests import get_computer_task

    settings = _mk_settings(
        conveyor_computer_blocked_keywords=("password",),
    )
    planner = ScriptedPlanner([
        {"action": "type", "text": "enter password to continue"},
        {"action": "done", "summary": "should not reach"},
    ])
    backend = FakeComputerBackend(settings)
    result = asyncio.run(run_computer_loop(
        settings, "fill the login form",
        planner=planner, backend=backend,
        max_steps=10, max_seconds=60, direct_mode=True,
    ))
    if result.get("status") != "blocked":
        _fail("blocked_keyword_stops_task", f"status={result.get('status')} reason={result.get('blocked_reason')}")
        return
    if "password" not in (result.get("blocked_reason") or ""):
        _fail("blocked_keyword_stops_task", f"reason={result.get('blocked_reason')}")
        return
    task = get_computer_task(settings, result["task_id"]) or {}
    if task.get("status") != "blocked":
        _fail("blocked_keyword_stops_task", f"stored status={task.get('status')}")
        return
    print("[pass] blocked_keyword_stops_task")


def _test_max_steps_stops_task() -> None:
    import asyncio

    from desktop_computer_loop import FakeComputerBackend, run_computer_loop
    from desktop_computer_planner import ScriptedPlanner

    settings = _mk_settings()
    planner = ScriptedPlanner([
        {"action": "observe"},
        {"action": "observe"},
        {"action": "observe"},
        {"action": "observe"},
    ])
    backend = FakeComputerBackend(settings)
    result = asyncio.run(run_computer_loop(
        settings, "keep watching",
        planner=planner, backend=backend,
        max_steps=2, max_seconds=60, direct_mode=True,
    ))
    if result.get("steps_used") != 2:
        _fail("max_steps_stops_task", f"steps_used={result.get('steps_used')}")
        return
    if result.get("status") != "done":
        _fail("max_steps_stops_task", f"status={result.get('status')}")
        return
    print("[pass] max_steps_stops_task")


def _test_stop_check_cancels() -> None:
    import asyncio

    from desktop_computer_loop import FakeComputerBackend, run_computer_loop
    from desktop_computer_planner import ScriptedPlanner

    settings = _mk_settings()
    planner = ScriptedPlanner([
        {"action": "observe"},
        {"action": "observe"},
    ])
    backend = FakeComputerBackend(settings)
    result = asyncio.run(run_computer_loop(
        settings, "long task",
        planner=planner, backend=backend,
        max_steps=10, max_seconds=60, direct_mode=True,
        stop_check=lambda: True,
    ))
    if result.get("status") != "stopped":
        _fail("stop_check_cancels", f"status={result.get('status')} reason={result.get('blocked_reason')}")
        return
    print("[pass] stop_check_cancels")


# ---- Execution: fake backend, trajectory, redaction ----------------------


def _test_fake_backend_run_and_redaction() -> None:
    import asyncio

    from desktop_computer_loop import FakeComputerBackend, run_computer_loop
    from desktop_computer_planner import ScriptedPlanner
    from desktop_computer_requests import get_computer_task

    settings = _mk_settings()
    planner = ScriptedPlanner([
        {"action": "observe"},
        {"action": "type", "text": "this-is-a-secret-secret"},
        {"action": "click", "x": 10, "y": 20},
        {"action": "done", "summary": "finished"},
    ])
    backend = FakeComputerBackend(settings)
    # Ensure we are on the fake transport (no real driver / network).
    if not _is_fake(backend):
        _fail("fake_backend_run_and_redaction", "backend not using FakeCuaTransport")
        return
    result = asyncio.run(run_computer_loop(
        settings, "demo goal",
        planner=planner, backend=backend,
        max_steps=10, max_seconds=60, direct_mode=True,
    ))
    if result.get("status") != "done":
        _fail("fake_backend_run_and_redaction", f"status={result.get('status')}")
        return
    if result.get("trajectory_len", 0) < 3:
        _fail("fake_backend_run_and_redaction", f"traj_len={result.get('trajectory_len')}")
        return
    task = get_computer_task(settings, result["task_id"]) or {}
    # Find the type step and verify redaction.
    typed = [e for e in task.get("trajectory", []) if e.get("action_type") == "type"]
    if not typed:
        _fail("fake_backend_run_and_redaction", "no type step recorded")
        return
    redacted = typed[0].get("action_redacted") or {}
    if "text" in redacted:
        _fail("fake_backend_run_and_redaction", f"raw text leaked: {redacted}")
        return
    if "text_len" not in redacted:
        _fail("fake_backend_run_and_redaction", f"missing text_len: {redacted}")
        return
    # Screenshot ids must be fake (no real Cua).
    obs = [e for e in task.get("trajectory", []) if e.get("screenshot_id")]
    if obs and not str(obs[0].get("screenshot_id", "")).startswith("fake_obs_"):
        _fail("fake_backend_run_and_redaction", f"unexpected screenshot id: {obs[0]}")
        return
    print("[pass] fake_backend_run_and_redaction")


def _test_claim_action_redaction_boundary() -> None:
    from desktop_computer_requests import (
        create_computer_step,
        create_computer_task,
        get_computer_task,
        claim_computer_step,
        list_pending_computer_steps,
    )

    settings = _mk_settings()
    created = create_computer_task(
        settings, "type hello",
        direct_mode=True, max_steps=3, max_seconds=60,
    )
    task_id = created["task_id"]
    step = create_computer_step(settings, task_id, {"action": "type", "text": "hello-secret"})
    step_id = step["step_id"]

    pending = list_pending_computer_steps(settings, limit=1)
    if not pending or pending[0].get("step_id") != step_id:
        _fail("claim_action_redaction_boundary", f"bad pending list: {pending}")
        return
    if (pending[0].get("action") or {}).get("text"):
        _fail("claim_action_redaction_boundary", f"pending leaked action: {pending[0]}")
        return
    if (pending[0].get("action_redacted") or {}).get("text"):
        _fail("claim_action_redaction_boundary", f"pending leaked text: {pending[0]}")
        return

    claimed = claim_computer_step(settings, step_id, settings.conveyor_desktop_node_id or "macbook-payton")
    action = (claimed.get("step") or {}).get("action") or {}
    if action.get("text") != "hello-secret":
        _fail("claim_action_redaction_boundary", f"claim did not return executable action: {action}")
        return

    stored = get_computer_task(settings, task_id) or {}
    stored_action = ((stored.get("steps") or {}).get(step_id) or {}).get("action") or {}
    if stored_action.get("text"):
        _fail("claim_action_redaction_boundary", f"stored raw action after claim: {stored_action}")
        return
    if stored_action.get("text_redacted") != "***" or stored_action.get("text_len") != len("hello-secret"):
        _fail("claim_action_redaction_boundary", f"stored action not redacted: {stored_action}")
        return
    print("[pass] claim_action_redaction_boundary")


def _is_fake(backend) -> bool:
    from desktop_cua import FakeCuaTransport

    return isinstance(backend.driver.transport, FakeCuaTransport)


# ---- Kill switch ----------------------------------------------------------


def _test_stop_command_cancels_active() -> None:
    import asyncio

    from desktop_computer_requests import (
        cancel_computer_task,
        create_computer_task,
        get_active_task,
    )

    settings = _mk_settings()
    created = create_computer_task(
        settings, "active task",
        direct_mode=True, max_steps=10, max_seconds=60,
    )
    if not created.get("ok"):
        _fail("stop_command_cancels_active", f"create failed: {created}")
        return
    active = get_active_task(settings)
    if active is None or active.get("status") != "running":
        _fail("stop_command_cancels_active", f"active={active}")
        return
    res = cancel_computer_task(settings, active["task_id"], reason="operator_stop")
    if not res.get("ok"):
        _fail("stop_command_cancels_active", f"cancel failed: {res}")
        return
    after = get_active_task(settings)
    if after is not None:
        _fail("stop_command_cancels_active", f"still active: {after}")
        return
    print("[pass] stop_command_cancels_active")


# ---- Hardening P5.6.1 Smokes ----------------------------------------------

def _test_cua_status_fields() -> None:
    from handlers.tools.executors import exec_computer_status
    from nodes.state import record_heartbeat, register_desktop_node
    import asyncio
    
    settings = _mk_settings()
    node_id = settings.conveyor_desktop_node_id or "macbook-payton"
    register_desktop_node(settings, node_id, display_name="Test MacBook", agent_version="1.0", host_info={"hostname": "localhost"})
    record_heartbeat(settings, node_id, "idle", "heartbeat", poll_computer=True)
    
    status_text = asyncio.run(exec_computer_status(settings, ""))
    if "Allowed apps:" not in status_text:
        _fail("cua_status_fields", "missing Allowed apps")
        return
    if "Blocked apps:" not in status_text:
        _fail("cua_status_fields", "missing Blocked apps")
        return
    if "poll-computer: enabled" not in status_text:
        _fail("cua_status_fields", f"missing poll-computer status: {status_text}")
        return
    if "Always-Direct:" not in status_text:
        _fail("cua_status_fields", "missing Always-Direct status")
        return
    print("[pass] cua_status_fields")


def _test_trajectory_jsonl_writing_and_redaction() -> None:
    import asyncio
    import json
    from desktop_computer_loop import FakeComputerBackend, run_computer_loop
    from desktop_computer_planner import ScriptedPlanner
    
    settings = _mk_settings()
    planner = ScriptedPlanner([
        {"action": "observe"},
        {"action": "type", "text": "secret1234"},
        {"action": "done", "summary": "finished"},
    ])
    backend = FakeComputerBackend(settings)
    result = asyncio.run(run_computer_loop(
        settings, "do a type test",
        planner=planner, backend=backend,
        max_steps=10, max_seconds=60, direct_mode=True,
    ))
    
    task_id = result["task_id"]
    jsonl_path = Path(settings.codex_memory_root) / "computer" / "trajectories" / f"{task_id}.jsonl"
    if not jsonl_path.exists():
        _fail("trajectory_jsonl_writing", f"JSONL file not found at {jsonl_path}")
        return
        
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        _fail("trajectory_jsonl_writing", f"JSONL too short: {len(lines)} lines")
        return
        
    for line in lines:
        data = json.loads(line)
        if data.get("action_type") == "type":
            redacted_args = data.get("redacted_args") or {}
            if "text" in redacted_args:
                _fail("trajectory_jsonl_redaction", f"leaked raw text: {line}")
                return
            if redacted_args.get("text_redacted") != "***":
                _fail("trajectory_jsonl_redaction", f"missing text_redacted marker: {line}")
                return
    print("[pass] trajectory_jsonl_writing_and_redaction")


def _test_failure_report_concise() -> None:
    import asyncio
    from desktop_computer_loop import FakeComputerBackend, run_computer_loop
    from desktop_computer_planner import ScriptedPlanner
    from handlers.tools.executors import _format_loop_result
    
    settings = _mk_settings()
    planner = ScriptedPlanner([
        {"action": "observe"},
        {"action": "observe"},
    ])
    backend = FakeComputerBackend(settings)
    result = asyncio.run(run_computer_loop(
        settings, "fail test",
        planner=planner, backend=backend,
        max_steps=1, max_seconds=60, direct_mode=True,
    ))
    
    report = _format_loop_result(settings, result)
    if "❌ Task failed/stopped" not in report:
        _fail("failure_report_concise", f"missing fail header: {report}")
        return
    if f"Task ID: {result['task_id']}" not in report:
        _fail("failure_report_concise", "missing Task ID")
        return
    if "Last Action: observe" not in report:
        _fail("failure_report_concise", f"missing/incorrect Last Action: {report}")
        return
    if "Last Screenshot: fake_obs_" not in report:
        _fail("failure_report_concise", f"missing Last Screenshot: {report}")
        return
    print("[pass] failure_report_concise")


def _test_ax_click_preference_and_fallback() -> None:
    import asyncio
    from desktop_cua import build_driver
    
    settings = _mk_settings()
    driver = build_driver(settings, fake=True)
    
    action = {"action": "click", "pid": 123, "window_id": 456, "element_index": 7, "x": 10, "y": 20}
    res = driver.execute(action)
    if res.get("click_method") != "ax_click":
        _fail("ax_click_preference", f"expected click_method=ax_click, got {res.get('click_method')}")
        return
        
    action_xy = {"action": "click", "x": 10, "y": 20}
    res_xy = driver.execute(action_xy)
    if res_xy.get("click_method") != "xy_click":
        _fail("ax_click_preference", f"expected click_method=xy_click, got {res_xy.get('click_method')}")
        return
        
    print("[pass] ax_click_preference_and_fallback")


def _test_app_allowlist_and_blocklist() -> None:
    import asyncio
    from desktop_computer_loop import FakeComputerBackend, run_computer_loop
    from desktop_computer_planner import ScriptedPlanner
    
    settings = _mk_settings(
        conveyor_computer_blocked_apps=("Keychain Access", "System Settings", "Terminal")
    )
    planner = ScriptedPlanner([
        {"action": "observe", "_mock_active_app": "System Settings"},
        {"action": "done", "summary": "completed"},
    ])
    backend = FakeComputerBackend(settings)
    result = asyncio.run(run_computer_loop(
        settings, "blocked app test",
        planner=planner, backend=backend,
        max_steps=10, max_seconds=60, direct_mode=True,
    ))
    if result.get("status") != "stopped" or "blocked_app:System Settings" not in (result.get("blocked_reason") or ""):
        _fail("app_blocklist", f"expected stopped due to blocked_app:System Settings, got {result}")
        return
        
    # Mutating action without pid: frontmost Chrome not in allowlist → stop.
    settings_allow = _mk_settings(
        conveyor_computer_allowed_apps=("Finder", "Safari")
    )
    planner_allow = ScriptedPlanner([
        {"action": "click", "x": 10, "y": 20, "_mock_active_app": "Chrome"},
        {"action": "done", "summary": "completed"},
    ])
    backend_allow = FakeComputerBackend(settings_allow)
    result_allow = asyncio.run(run_computer_loop(
        settings_allow, "allowed app test",
        planner=planner_allow, backend=backend_allow,
        max_steps=10, max_seconds=60, direct_mode=True,
    ))
    if result_allow.get("status") != "stopped" or "app_not_in_allowlist:Chrome" not in (result_allow.get("blocked_reason") or ""):
        _fail("app_allowlist", f"expected stopped due to app_not_in_allowlist:Chrome, got {result_allow}")
        return
        
    print("[pass] app_allowlist_and_blocklist")


def _test_ax_target_app_allowlist_not_frontmost() -> None:
    """AX click with pid uses target app for allowlist, not frontmost."""
    from desktop_cua import build_driver

    settings = _mk_settings(
        conveyor_computer_allowed_apps=("Calculator",),
        conveyor_computer_blocked_apps=(),
    )
    driver = build_driver(settings, fake=True)
    # Frontmost-like mock is Codex (not allowed); target is Calculator.
    action = {
        "action": "click",
        "pid": 59084,
        "window_id": 0,
        "element_index": 13,
        "_mock_active_app": "Codex",
        "_mock_target_app": "Calculator",
    }
    res = driver.execute(action)
    if not res.get("result_ok"):
        _fail(
            "ax_target_app_allowlist_not_frontmost",
            f"AX click on allowed target rejected: {res}",
        )
        return
    if res.get("active_app") != "Calculator":
        _fail(
            "ax_target_app_allowlist_not_frontmost",
            f"expected active_app=Calculator, got {res.get('active_app')}",
        )
        return
    if res.get("click_method") != "ax_click":
        _fail(
            "ax_target_app_allowlist_not_frontmost",
            f"expected ax_click, got {res.get('click_method')}",
        )
        return

    # Target app not in allowlist must still block.
    bad = {
        "action": "click",
        "pid": 1,
        "window_id": 0,
        "element_index": 0,
        "_mock_target_app": "Codex",
        "_mock_active_app": "Calculator",
    }
    res_bad = driver.execute(bad)
    if res_bad.get("result_ok") or "app_not_in_allowlist:Codex" not in (res_bad.get("error") or ""):
        _fail(
            "ax_target_app_allowlist_not_frontmost",
            f"expected target Codex blocked, got {res_bad}",
        )
        return
    print("[pass] ax_target_app_allowlist_not_frontmost")


def _test_xy_click_blocked_when_app_allowlist_set() -> None:
    """Bare x/y click must fail under app allowlist (no target app proof)."""
    from desktop_cua import build_driver

    settings = _mk_settings(
        conveyor_computer_allowed_apps=("Calculator",),
        conveyor_computer_blocked_apps=(),
    )
    driver = build_driver(settings, fake=True)
    res = driver.execute({
        "action": "click",
        "x": 100,
        "y": 200,
        "_mock_active_app": "Codex",
    })
    if res.get("result_ok") or res.get("error") != "ax_required_when_app_allowlist_set":
        _fail("xy_click_blocked_when_app_allowlist_set", f"expected ax_required error, got {res}")
        return
    # AX click still allowed.
    ok = driver.execute({
        "action": "click",
        "pid": 1,
        "window_id": 0,
        "element_index": 1,
        "_mock_target_app": "Calculator",
    })
    if not ok.get("result_ok"):
        _fail("xy_click_blocked_when_app_allowlist_set", f"AX click should pass: {ok}")
        return
    print("[pass] xy_click_blocked_when_app_allowlist_set")


def _test_observe_injects_ax_hints_for_planner() -> None:
    """Observe must return pid/window_id/element_hints so planner can AX-click."""
    from desktop_computer_planner import CodexPlanner
    from desktop_computer_requests import (
        claim_computer_step,
        complete_computer_step,
        create_computer_step,
        create_computer_task,
        validate_computer_result,
    )
    from desktop_cua import build_driver

    settings = _mk_settings(
        conveyor_computer_allowed_apps=("Calculator",),
        conveyor_computer_blocked_apps=(),
    )
    driver = build_driver(settings, fake=True)
    obs = driver.execute({
        "action": "observe",
        "_mock_active_app": "Codex",
    })
    if not obs.get("result_ok"):
        _fail("observe_injects_ax_hints", f"observe failed: {obs}")
        return
    if obs.get("pid") is None or obs.get("window_id") is None:
        _fail("observe_injects_ax_hints", f"missing pid/window_id: {obs}")
        return
    hints = obs.get("element_hints") or []
    if not any(str(h.get("label")) == "1" for h in hints if isinstance(h, dict)):
        _fail("observe_injects_ax_hints", f"no label=1 in hints: {hints}")
        return

    cleaned = validate_computer_result(obs)
    if cleaned is None:
        _fail("observe_injects_ax_hints", "validate rejected observe with AX hints")
        return
    if not cleaned.get("element_hints") or cleaned.get("pid") is None:
        _fail("observe_injects_ax_hints", f"cleaned dropped AX fields: {cleaned}")
        return

    # complete_computer_step must accept the enriched observe result.
    created = create_computer_task(
        settings, "calc", direct_mode=True, max_steps=3, max_seconds=60,
    )
    step = create_computer_step(settings, created["task_id"], {"action": "observe"})
    claim_computer_step(settings, step["step_id"], "mac-test")
    done = complete_computer_step(settings, step["step_id"], "mac-test", obs)
    if not done.get("ok"):
        _fail("observe_injects_ax_hints", f"complete rejected: {done}")
        return

    planner = CodexPlanner(settings)
    prompt = planner._build_prompt(
        goal="在当前 Calculator 窗口里点击数字 1，然后完成",
        observation=cleaned,
        trajectory=[],
        steps_used=1,
        max_steps=10,
    )
    if "硬性规则" not in prompt or "element_hints" not in prompt:
        _fail("observe_injects_ax_hints", "planner prompt missing hard AX rule")
        return
    if "禁止只输出 x/y" not in prompt:
        _fail("observe_injects_ax_hints", "planner prompt missing xy ban")
        return
    print("[pass] observe_injects_ax_hints_for_planner")


def _test_observe_skips_allowlist_under_restricted_apps() -> None:
    """Bare observe must work when frontmost is not in allowlist (NL task path)."""
    import asyncio

    from desktop_computer_loop import FakeComputerBackend, run_computer_loop
    from desktop_computer_planner import ScriptedPlanner
    from desktop_cua import build_driver

    settings = _mk_settings(
        conveyor_computer_allowed_apps=("Calculator",),
        conveyor_computer_blocked_apps=("Keychain Access", "System Settings", "Terminal"),
    )
    driver = build_driver(settings, fake=True)

    obs = driver.execute({
        "action": "observe",
        "_mock_active_app": "Codex",
    })
    if not obs.get("result_ok"):
        _fail("observe_skips_allowlist", f"observe blocked by allowlist: {obs}")
        return
    if obs.get("active_app") != "Codex":
        _fail("observe_skips_allowlist", f"active_app={obs.get('active_app')}")
        return

    # Blocklist still applies to observe (safety).
    blocked_obs = driver.execute({
        "action": "observe",
        "_mock_active_app": "System Settings",
    })
    if blocked_obs.get("result_ok") or "blocked_app:System Settings" not in (blocked_obs.get("error") or ""):
        _fail("observe_skips_allowlist", f"blocklist should still apply: {blocked_obs}")
        return

    # Full loop: observe (Codex frontmost) then AX click Calculator → done.
    planner = ScriptedPlanner([
        {"action": "observe", "_mock_active_app": "Codex"},
        {
            "action": "click",
            "pid": 59084,
            "window_id": 0,
            "element_index": 13,
            "_mock_active_app": "Codex",
            "_mock_target_app": "Calculator",
        },
        {"action": "done", "summary": "clicked 1"},
    ])
    backend = FakeComputerBackend(settings)
    result = asyncio.run(run_computer_loop(
        settings, "在当前 Calculator 窗口里点击数字 1，然后完成",
        planner=planner, backend=backend,
        max_steps=10, max_seconds=60, direct_mode=True,
    ))
    if result.get("status") != "done":
        _fail(
            "observe_skips_allowlist",
            f"loop should complete under Calculator-only allowlist, got {result}",
        )
        return
    print("[pass] observe_skips_allowlist_under_restricted_apps")


def _test_stop_fast_path() -> None:
    from handlers.dispatch import dispatch
    from channel import InboundMessage
    from channel.types import OutboundPort
    from runner import CodexRunner
    import asyncio
    
    class FakeOutbound(OutboundPort):
        def __init__(self):
            self.replies = []
        async def reply(self, msg, text):
            self.replies.append(text)
            
    settings = _mk_settings()
    runner = CodexRunner(settings)
    port = FakeOutbound()
    
    msg = InboundMessage(
        message_id="msg-1",
        chat_id="chat-1",
        operator_id="1",
        text="停下",
        channel="telegram",
    )
    asyncio.run(dispatch(msg, port, settings, runner))
    
    if not port.replies or "没有正在运行的" not in port.replies[0]:
        _fail("stop_fast_path", f"expected '没有正在运行的' in reply, got {port.replies}")
        return
        
    print("[pass] stop_fast_path")


# ---- P5.6.2 Fix Pack Smokes -----------------------------------------------


def _test_result_allows_active_app_click_method() -> None:
    """Successful Cua results with active_app/click_method are accepted."""
    from desktop_computer_requests import (
        claim_computer_step,
        complete_computer_step,
        create_computer_step,
        create_computer_task,
        validate_computer_result,
    )

    settings = _mk_settings()
    created = create_computer_task(
        settings, "click test", direct_mode=True, max_steps=3, max_seconds=60,
    )
    task_id = created["task_id"]
    step = create_computer_step(
        settings, task_id, {"action": "click", "x": 10, "y": 20},
    )
    step_id = step["step_id"]
    node_id = "mac-test"
    claim_computer_step(settings, step_id, node_id)

    result = {
        "result_ok": True,
        "action_type": "click",
        "node_id": node_id,
        "active_app": "Finder",
        "click_method": "ax_click",
    }
    cleaned = validate_computer_result(result)
    if cleaned is None:
        _fail("result_allows_active_app_click_method", "validate rejected good result")
        return
    if cleaned.get("active_app") != "Finder" or cleaned.get("click_method") != "ax_click":
        _fail("result_allows_active_app_click_method", f"cleaned={cleaned}")
        return

    done = complete_computer_step(settings, step_id, node_id, result)
    if not done.get("ok"):
        _fail("result_allows_active_app_click_method", f"complete failed: {done}")
        return
    stored = (done.get("step") or {}).get("result") or {}
    if stored.get("active_app") != "Finder" or stored.get("click_method") != "ax_click":
        _fail("result_allows_active_app_click_method", f"stored result={stored}")
        return
    print("[pass] result_allows_active_app_click_method")


def _test_result_rejects_unknown_fields() -> None:
    from desktop_computer_requests import (
        claim_computer_step,
        complete_computer_step,
        create_computer_step,
        create_computer_task,
        validate_computer_result,
    )

    bad = {"result_ok": True, "window_title": "Secret Window", "active_app": "Safari"}
    if validate_computer_result(bad) is not None:
        _fail("result_rejects_unknown_fields", "forbidden field accepted")
        return
    unknown = {"result_ok": True, "raw_ocr": "leaked text"}
    if validate_computer_result(unknown) is not None:
        _fail("result_rejects_unknown_fields", "unknown field accepted")
        return

    settings = _mk_settings()
    created = create_computer_task(
        settings, "reject test", direct_mode=True, max_steps=3, max_seconds=60,
    )
    step = create_computer_step(
        settings, created["task_id"], {"action": "observe"},
    )
    claim_computer_step(settings, step["step_id"], "mac-test")
    res = complete_computer_step(
        settings, step["step_id"], "mac-test",
        {"result_ok": True, "mystery_field": "nope"},
    )
    if res.get("ok") or res.get("error") != "invalid_result":
        _fail("result_rejects_unknown_fields", f"complete should reject: {res}")
        return
    print("[pass] result_rejects_unknown_fields")


def _test_direct_enabled_false_blocks_arm_task_action() -> None:
    import asyncio

    from desktop_computer_requests import (
        arm_direct_mode,
        disarm_direct_mode,
        is_direct_mode_active,
    )
    from handlers.tools.executors import (
        exec_computer_action,
        exec_computer_task,
    )

    settings = _mk_settings(
        conveyor_computer_use_enabled=True,
        conveyor_computer_direct_enabled=False,
        conveyor_computer_always_direct=False,
    )
    disarm_direct_mode(settings)
    if is_direct_mode_active(settings):
        _fail("direct_enabled_false_blocks", "active with direct_enabled=false")
        return
    arm_res = arm_direct_mode(settings, 30)
    if arm_res.get("ok") or arm_res.get("error") != "computer_direct_disabled":
        _fail("direct_enabled_false_blocks", f"arm should fail: {arm_res}")
        return
    if is_direct_mode_active(settings):
        _fail("direct_enabled_false_blocks", "active after failed arm")
        return

    task_text = asyncio.run(exec_computer_task(settings, "open Chrome"))
    if "Direct" not in task_text and "未启用" not in task_text:
        _fail("direct_enabled_false_blocks", f"task not blocked: {task_text}")
        return
    action_text = asyncio.run(
        exec_computer_action(settings, '{"action":"observe"}')
    )
    if "Direct" not in action_text and "未启用" not in action_text:
        _fail("direct_enabled_false_blocks", f"action not blocked: {action_text}")
        return
    print("[pass] direct_enabled_false_blocks_arm_task_action")


def _test_direct_enabled_true_plus_arm() -> None:
    from desktop_computer_requests import (
        arm_direct_mode,
        disarm_direct_mode,
        is_direct_mode_active,
    )

    settings = _mk_settings(
        conveyor_computer_use_enabled=True,
        conveyor_computer_direct_enabled=True,
        conveyor_computer_always_direct=False,
    )
    disarm_direct_mode(settings)
    if is_direct_mode_active(settings):
        _fail("direct_enabled_true_plus_arm", "active before arm")
        return
    res = arm_direct_mode(settings, 15)
    if not res.get("ok"):
        _fail("direct_enabled_true_plus_arm", f"arm failed: {res}")
        return
    if not is_direct_mode_active(settings):
        _fail("direct_enabled_true_plus_arm", "not active after arm")
        return
    print("[pass] direct_enabled_true_plus_arm")


def _test_always_direct_requires_direct_enabled() -> None:
    from desktop_computer_requests import (
        disarm_direct_mode,
        is_direct_mode_active,
    )

    # always_direct=true but direct_enabled=false must NOT enable direct mode.
    settings = _mk_settings(
        conveyor_computer_use_enabled=True,
        conveyor_computer_direct_enabled=False,
        conveyor_computer_always_direct=True,
    )
    disarm_direct_mode(settings)
    if is_direct_mode_active(settings):
        _fail(
            "always_direct_requires_direct_enabled",
            "always_direct bypassed missing DIRECT_ENABLED",
        )
        return
    print("[pass] always_direct_requires_direct_enabled")


def _test_command_table_computer_observe_action() -> None:
    from handlers.commands import COMMAND_TABLE

    for name in ("computer_observe", "computer_action"):
        if name not in COMMAND_TABLE:
            _fail("command_table_computer_aliases", f"missing {name}")
            return
    print("[pass] command_table_computer_observe_action")


def _test_planner_ax_first_prompt() -> None:
    from desktop_computer_planner import CodexPlanner

    settings = _mk_settings()
    planner = CodexPlanner(settings)
    obs = {
        "screenshot_id": "obs_abc",
        "width": 100,
        "height": 100,
        "pid": 123,
        "window_id": 0,
        "element_index": 5,
        "element_hints": [{"label": "OK", "element_index": 5}],
        "active_app": "Safari",
    }
    prompt = planner._build_prompt(
        goal="click OK",
        observation=obs,
        trajectory=[],
        steps_used=0,
        max_steps=10,
    )
    needed = (
        "AX-first",
        '"pid":123',
        "element_index",
        "仅当观察中没有 AX",
        "element_hints",
        "active_app=Safari",
    )
    for token in needed:
        if token not in prompt:
            _fail("planner_ax_first_prompt", f"missing {token!r} in prompt")
            return
    print("[pass] planner_ax_first_prompt")


def _test_trajectory_permissions_and_redaction() -> None:
    import asyncio
    import json
    import stat as statmod

    from desktop_computer_loop import FakeComputerBackend, run_computer_loop
    from desktop_computer_planner import ScriptedPlanner

    settings = _mk_settings()
    secret_text = "super-secret-token-xyz"
    planner = ScriptedPlanner([
        {"action": "type", "text": secret_text},
        {"action": "hotkey", "keys": ["cmd", "v"]},
        {"action": "done", "summary": "ok"},
    ])
    backend = FakeComputerBackend(settings)
    result = asyncio.run(run_computer_loop(
        settings, "type a note",
        planner=planner, backend=backend,
        max_steps=10, max_seconds=60, direct_mode=True,
    ))
    if result.get("status") not in ("done", "stopped", "error", "blocked"):
        _fail("trajectory_permissions", f"unexpected status: {result}")
        return
    task_id = result["task_id"]
    computer_dir = Path(settings.codex_memory_root) / "computer"
    traj_dir = computer_dir / "trajectories"
    jsonl_path = traj_dir / f"{task_id}.jsonl"
    if not jsonl_path.exists():
        _fail("trajectory_permissions", f"missing {jsonl_path}; status={result}")
        return

    raw = jsonl_path.read_text(encoding="utf-8")
    if secret_text in raw:
        _fail("trajectory_permissions", "raw typed text leaked into JSONL")
        return
    for line in raw.splitlines():
        data = json.loads(line)
        args = data.get("redacted_args") or {}
        if data.get("action_type") == "type" and "text" in args:
            _fail("trajectory_permissions", f"type text not redacted: {args}")
            return
        if data.get("action_type") == "hotkey" and "keys" in args:
            _fail("trajectory_permissions", f"hotkey keys not redacted: {args}")
            return

    # Permission check is best-effort (some FS ignore chmod).
    try:
        for d in (computer_dir, traj_dir):
            mode = statmod.S_IMODE(d.stat().st_mode)
            if mode != 0o700:
                print(
                    f"[warn] trajectory_permissions: {d} mode={oct(mode)} "
                    f"(expected 0o700; best-effort)"
                )
        fmode = statmod.S_IMODE(jsonl_path.stat().st_mode)
        if fmode != 0o600:
            print(
                f"[warn] trajectory_permissions: file mode={oct(fmode)} "
                f"(expected 0o600; best-effort)"
            )
    except OSError as exc:
        print(f"[warn] trajectory_permissions: chmod check skipped: {exc}")

    print("[pass] trajectory_permissions_and_redaction")


# ---- Run -----------------------------------------------------------------


def main() -> int:
    _test_config_defaults_disabled()
    _test_missing_driver_graceful()
    _test_local_cli_transport_maps_cua_tools()
    _test_local_cli_transport_desktop_state_fallback()
    _test_arm_ttl_enables_direct()
    _test_expired_arm_blocks()
    _test_always_direct_bypass()
    _test_action_schema_allowlist()
    _test_blocked_keyword_stops_task()
    _test_max_steps_stops_task()
    _test_stop_check_cancels()
    _test_fake_backend_run_and_redaction()
    _test_claim_action_redaction_boundary()
    _test_stop_command_cancels_active()

    # P5.6.1 Hardening Smokes
    _test_cua_status_fields()
    _test_trajectory_jsonl_writing_and_redaction()
    _test_failure_report_concise()
    _test_ax_click_preference_and_fallback()
    _test_app_allowlist_and_blocklist()
    _test_ax_target_app_allowlist_not_frontmost()
    _test_xy_click_blocked_when_app_allowlist_set()
    _test_observe_injects_ax_hints_for_planner()
    _test_observe_skips_allowlist_under_restricted_apps()
    _test_stop_fast_path()

    # P5.6.2 Fix Pack Smokes
    _test_result_allows_active_app_click_method()
    _test_result_rejects_unknown_fields()
    _test_direct_enabled_false_blocks_arm_task_action()
    _test_direct_enabled_true_plus_arm()
    _test_always_direct_requires_direct_enabled()
    _test_command_table_computer_observe_action()
    _test_planner_ax_first_prompt()
    _test_trajectory_permissions_and_redaction()

    total = 32
    failed = len(FAILURES)
    passed = total - failed
    print(f"\n{'=' * 60}")
    print(f"Desktop Computer Use smoke (P5.6.2): {passed}/{total} passed")
    if FAILURES:
        print(f"FAILURES: {', '.join(FAILURES)}")
        return 1
    print("All tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
