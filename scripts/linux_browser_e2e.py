#!/usr/bin/env python3
"""Opt-in real X11 browser loop for a supervisor on an isolated display.

This process does not start X, does not serve HTTP, and does not edit
production config. It drives the production planner and X11 backend.
The supervisor runs it. Unit tests do not.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROTECTED_FIREFOX_PIDS = {24589}
PRODUCTION_ENV = Path("/opt/conveyor/.env")
FIXTURE_ORIGIN = "http://127.0.0.1:19202"
FORBIDDEN_ITEM_TYPES = {
    "command_execution", "local_shell", "shell_command", "shell",
    "mcp_tool_call", "mcp", "web_search", "web_search_call",
    "function_call", "tool_call", "custom_tool_call",
    "browser_use", "browser_use_external", "computer_use",
}
DISABLE_FEATURES = ("shell_tool", "browser_use", "browser_use_external", "computer_use")
CASES = ("startup", "local", "weather", "stability")
SCENARIOS = ("closed", "open", "minimized", "launcher", "otherpage")


def _refuse(message: str) -> None:
    print(f"refused: {message}", file=sys.stderr)
    raise SystemExit(2)


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _parse_cases(text: str) -> list[str]:
    parts = [item.strip().lower() for item in text.replace("/", ",").split(",") if item.strip()]
    unknown = [item for item in parts if item not in CASES]
    if unknown or not parts:
        _refuse("cases must be startup, local, weather, stability")
    return parts


def _load_manifest(path: Path, root: Path) -> tuple[int, str]:
    if not path.is_file():
        _refuse("manifest missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _refuse("manifest unreadable")
    try:
        display = int(data["display"])
    except (KeyError, TypeError, ValueError):
        _refuse("manifest display missing")
    if display in {0, 1} or display < 2:
        _refuse("display 0 and 1 are refused")
    authority = str(data.get("xauthority") or data.get("Xauthority") or "")
    if not authority or not os.path.isfile(authority):
        _refuse("private X authority missing")
    if not _under(Path(authority), root) and not authority.startswith("/tmp/"):
        _refuse("X authority is not a private test cookie")
    return display, authority


def _check_root(root: Path) -> Path:
    root = root.resolve()
    if root == Path("/") or root == Path.home().resolve():
        _refuse("root is not an owned test directory")
    text = str(root)
    if text.startswith("/opt/conveyor") or text in {"/opt", "/usr", "/etc"}:
        _refuse("root overlaps production")
    if not root.is_dir():
        _refuse("root does not exist")
    try:
        stat = root.stat()
    except OSError:
        _refuse("root unreadable")
    if stat.st_uid != os.getuid():
        _refuse("root is not owned by this user")
    return root


def _production_paths_rejected(settings, root: Path) -> None:
    from desktop_screenshot import resolve_screenshot_dir

    paths = [
        Path(settings.codex_workspace_root),
        Path(settings.codex_task_root),
        Path(settings.codex_memory_root),
        resolve_screenshot_dir(settings),
    ]
    for path in paths:
        resolved = path.resolve()
        if not _under(resolved, root):
            _refuse("a storage root is outside the test directory")
        if str(resolved).startswith("/opt/conveyor"):
            _refuse("a storage root points at production")


def _settings(root: Path, wrapper: Path):
    """Load provider settings, then force every storage root under ``root``."""
    if not PRODUCTION_ENV.is_file():
        _refuse("production env file missing")
    workspace = root / "workspace"
    tasks = root / "tasks"
    memory = root / "memory"
    shots = root / "screenshots"
    for path in (workspace, tasks, memory, shots):
        path.mkdir(parents=True, exist_ok=True)
    # Set before dotenv so load_dotenv does not replace these with production.
    os.environ["CODEX_WORKSPACE_ROOT"] = str(workspace)
    os.environ["CODEX_TASK_ROOT"] = str(tasks)
    os.environ["CODEX_MEMORY_ROOT"] = str(memory)
    os.environ["CONVEYOR_DESKTOP_SCREENSHOT_DIR"] = str(shots)
    os.environ["CONVEYOR_AGENTS_ENABLED"] = "true"
    os.environ["CONVEYOR_AGENT_DESKTOPS_ENABLED"] = "true"
    os.environ["CONVEYOR_COMPUTER_USE_ENABLED"] = "true"
    os.environ["CONVEYOR_COMPUTER_DIRECT_ENABLED"] = "true"
    os.environ["CONVEYOR_COMPUTER_BACKEND"] = "http"
    from config import load_runtime_settings

    settings = load_runtime_settings(PRODUCTION_ENV)
    from dataclasses import replace

    model = settings.codex_model or "deepseek-flash"
    settings = replace(
        settings,
        codex_bin=str(wrapper),
        codex_model=model,
        codex_workspace_root=workspace,
        codex_task_root=tasks,
        codex_memory_root=memory,
        conveyor_desktop_screenshot_dir=str(shots),
        agents_enabled=True,
        agent_desktops_enabled=True,
        conveyor_computer_use_enabled=True,
        conveyor_computer_direct_enabled=True,
        conveyor_computer_backend="http",
    )
    _production_paths_rejected(settings, root)
    if not (workspace / ".git").is_dir():
        subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=e2e@localhost", "-c", "user.name=e2e",
             "commit", "--allow-empty", "-m", "init"],
            cwd=workspace, check=True, capture_output=True,
        )
    return settings


def _supported_flags(codex: str) -> list[str]:
    try:
        probe = subprocess.run(
            [codex, "exec", "--help"], capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = f"{probe.stdout}\n{probe.stderr}"
    flags: list[str] = []
    if "--disable" in text:
        for name in DISABLE_FEATURES:
            if name in text:
                flags.extend(["--disable", name])
    if "web_search" in text and "-c" in text:
        flags.extend(["-c", 'web_search="disabled"'])
    return flags


def _write_wrapper(root: Path, log_path: Path) -> Path:
    flags = _supported_flags("/usr/bin/codex")
    path = root / "bin" / "codex"
    path.parent.mkdir(parents=True, exist_ok=True)
    forbidden = sorted(FORBIDDEN_ITEM_TYPES)
    path.write_text(
        "\n".join([
            "#!/usr/bin/env python3",
            "import json, os, subprocess, sys, threading",
            "REAL = '/usr/bin/codex'",
            f"FLAGS = {flags!r}",
            f"LOG = {str(log_path)!r}",
            f"FORBIDDEN = set({forbidden!r})",
            "args = [REAL, *sys.argv[1:]]",
            "if 'exec' in args:",
            "    args[args.index('exec') + 1:args.index('exec') + 1] = list(FLAGS)",
            "proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)",
            "os.makedirs(os.path.dirname(LOG), exist_ok=True)",
            "log = open(LOG, 'a', encoding='utf-8')",
            "def consume(pipe, which):",
            "    sink = sys.stdout.buffer if which == 'stdout' else sys.stderr.buffer",
            "    for raw in pipe:",
            "        line = raw.decode('utf-8', 'replace').strip()",
            "        record = {'stream': which, 'event_type': 'non_json', 'forbidden': False}",
            "        try:",
            "            event = json.loads(line)",
            "        except json.JSONDecodeError:",
            "            event = None",
            "        if isinstance(event, dict):",
            "            kind = str(event.get('type') or event.get('event') or 'unknown')",
            "            item = event.get('item') if isinstance(event.get('item'), dict) else {}",
            "            item_type = str(item.get('type') or item.get('name') or '')",
            "            record = {'stream': which, 'event_type': kind, 'item_type': item_type,",
            "                       'forbidden': item_type in FORBIDDEN or kind in FORBIDDEN}",
            "        log.write(json.dumps(record) + '\\n')",
            "        log.flush()",
            "        sink.write(raw)",
            "        sink.flush()",
            "threads = [threading.Thread(target=consume, args=(proc.stdout, 'stdout')),",
            "           threading.Thread(target=consume, args=(proc.stderr, 'stderr'))]",
            "for thread in threads:",
            "    thread.start()",
            "code = proc.wait()",
            "for thread in threads:",
            "    thread.join(timeout=5)",
            "log.close()",
            "raise SystemExit(code)",
            "",
        ]),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _display_env(display: int, authority: str) -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "DISPLAY": f":{display}",
        "XAUTHORITY": authority,
    }


def _firefox_pids(display: int) -> list[int]:
    found: list[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in PROTECTED_FIREFOX_PIDS:
            continue
        try:
            raw = (entry / "environ").read_bytes()
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
            exe = os.readlink(entry / "exe")
        except OSError:
            continue
        env: dict[str, str] = {}
        for item in raw.split(b"\0"):
            if b"=" not in item:
                continue
            key, value = item.split(b"=", 1)
            env[key.decode("latin1", "replace")] = value.decode("latin1", "replace")
        if env.get("DISPLAY") != f":{display}":
            continue
        base = os.path.basename(exe)
        if comm in {"firefox", "firefox-esr", "firefox-bin"} or base in {"firefox", "firefox-esr", "firefox-bin"}:
            found.append(pid)
    return found


def _close_own_firefox(display: int) -> list[int]:
    closed = []
    for pid in _firefox_pids(display):
        if pid in PROTECTED_FIREFOX_PIDS:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            closed.append(pid)
        except OSError:
            continue
    time.sleep(0.5)
    return closed


def _run_x(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["xdotool", *args], env=env, capture_output=True, text=True, timeout=15, check=False,
    )


def _setup_scenario(name: str, display: int, authority: str, page: str) -> dict:
    """Verifier-side desktop state. Recorded separately from the task."""
    env = _display_env(display, authority)
    record: dict = {"scenario": name, "phase": "setup", "commands": []}
    if name == "closed":
        record["closed_pids"] = _close_own_firefox(display)
        return record
    if name in {"open", "minimized", "otherpage"} and not _firefox_pids(display):
        binary = shutil.which("firefox") or "/usr/bin/firefox"
        proc = subprocess.Popen(
            [binary, "--no-remote", "--new-window", "about:blank"],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        record["commands"].append({"setup_launch_pid": proc.pid, "argv0": binary})
        time.sleep(2)
    if name == "minimized":
        listed = _run_x(env, "search", "--class", "firefox")
        record["commands"].append({"xdotool": "search", "rc": listed.returncode})
        for wid in (listed.stdout or "").split():
            if wid.isdigit():
                minimized = _run_x(env, "windowminimize", wid)
                record["commands"].append({"xdotool": "windowminimize", "rc": minimized.returncode})
                break
    if name == "launcher":
        proc = subprocess.Popen(
            ["xfce4-appfinder", "--disable-server"],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        record["commands"].append({"setup_launch": "xfce4-appfinder", "pid": proc.pid})
    if name == "otherpage":
        typed = _run_x(env, "key", "ctrl+l")
        record["commands"].append({"xdotool": "key", "rc": typed.returncode})
        entered = _run_x(env, "type", "--", page)
        record["commands"].append({"xdotool": "type", "rc": entered.returncode})
        submitted = _run_x(env, "key", "Return")
        record["commands"].append({"xdotool": "key", "rc": submitted.returncode})
        time.sleep(1)
    return record


def _focus(display: int, authority: str) -> dict:
    env = _display_env(display, authority)
    active = _run_x(env, "getactivewindow")
    wid = (active.stdout or "").strip()
    out: dict = {}
    if active.returncode == 0 and wid.isdigit():
        out["window_id"] = int(wid)
        pid = _run_x(env, "getwindowpid", wid)
        text = (pid.stdout or "").strip()
        if pid.returncode == 0 and text.isdigit():
            out["pid"] = int(text)
        named = _run_x(env, "getwindowname", wid)
        title = (named.stdout or "").strip()
        if named.returncode == 0 and title:
            out["window_title"] = title[:120]
    return out


def _expected(root: Path) -> dict:
    path = root / "expected.json"
    if not path.is_file():
        _refuse("expected.json missing outside the workspace")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _refuse("expected.json unreadable")
    if _under(path, root / "workspace"):
        _refuse("expected.json must stay outside the workspace")
    return data


def _goal(case: str, round_index: int) -> str:
    if case == "startup":
        return "Open Firefox and leave its window in the foreground. Use the GUI only."
    if case == "weather":
        return (
            "Using only the on-screen browser, open a real weather webpage and read the "
            "current conditions for Montreal. Leave the browser window open. "
            "Do not use a shell, an API, or a web search tool."
        )
    if case == "stability":
        url = f"{FIXTURE_ORIGIN}/run-{round_index:02d}.html"
    else:
        url = f"{FIXTURE_ORIGIN}/index.html"
    return (
        f"Open {url} in Firefox. Read the visible page title and the Expected code shown on the page. "
        "Report both in the done summary. Use the GUI only."
    )


def _code_for(case: str, round_index: int, expected: dict) -> str | None:
    if case == "startup" or case == "weather":
        return None
    if case == "stability":
        runs = expected.get("runs") if isinstance(expected.get("runs"), dict) else {}
        return str(runs.get(f"{round_index:02d}") or runs.get(str(round_index)) or "") or None
    return str(expected.get("index") or "") or None


def _redact_trajectory(trajectory: list) -> list[dict]:
    kept = []
    for entry in trajectory or []:
        if not isinstance(entry, dict):
            continue
        kept.append({
            key: entry.get(key)
            for key in (
                "action_type", "result_ok", "error", "screenshot_id", "screenshot_hash",
                "pid", "window_id", "active_app", "duration_ms",
            )
            if key in entry
        })
    return kept


def _forbidden_count(log_path: Path, before: int) -> int:
    if not log_path.is_file():
        return 0
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[before:]
    count = 0
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("forbidden") is True:
            count += 1
    return count


def _event_types(log_path: Path, before: int) -> list[str]:
    if not log_path.is_file():
        return []
    types: list[str] = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()[before:]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = str(record.get("event_type") or "")
        item = str(record.get("item_type") or "")
        label = f"{kind}:{item}" if item else kind
        if label and label not in types:
            types.append(label)
    return types


def _assign_display(settings, display: int) -> tuple[str, str]:
    import sqlite3

    import agents
    from agents import AgentStore

    store = AgentStore(settings)
    agent = store.create({"name": "linux-e2e", "workspace_path": str(settings.codex_workspace_root)})
    agent_id = str(agent["id"])
    conn = sqlite3.connect(store.path)
    try:
        conn.execute("UPDATE agents SET display = ? WHERE id = ?", (display, agent_id))
        conn.commit()
    finally:
        conn.close()
    return agent_id, agents.chat_id_for(agent_id)


def _save_screenshot(settings, screenshot_id: str, dest: Path) -> str | None:
    if not screenshot_id:
        return None
    from desktop_screenshot import resolve_screenshot_dir

    source = resolve_screenshot_dir(settings) / f"{screenshot_id}.png"
    if not source.is_file():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def _classify(row: dict) -> str:
    if row.get("gui_forbidden"):
        return "gui_tool_use"
    if row.get("status") == "done" and not row.get("success"):
        return "false_completion"
    if row.get("case") == "weather" and row.get("structural_ok") and not row.get("success"):
        return "weather_needs_review"
    if not row.get("browser_foreground"):
        return "browser_not_foreground"
    if not row.get("fresh_image"):
        return "stale_or_missing_image"
    if row.get("status") not in {"done", "error", "stopped", "blocked"}:
        return "incomplete"
    if row.get("success"):
        return "ok"
    return str(row.get("status") or "failed")


async def _one(settings, *, case: str, round_index: int, scenario: str, display: int,
               authority: str, chat_id: str, expected: dict, artifact: Path, log_path: Path) -> dict:
    from desktop_computer_loop import build_backend, run_computer_loop
    from desktop_computer_planner import CodexPlanner
    from desktop_computer_requests import create_computer_task, get_computer_task

    goal = _goal(case, round_index)
    code = _code_for(case, round_index, expected)
    other = f"{FIXTURE_ORIGIN}/run-{((round_index % 20) + 1):02d}.html"
    setup = _setup_scenario(scenario, display, authority, other)
    before_pids = set(_firefox_pids(display))
    log_before = 0
    if log_path.is_file():
        log_before = len(log_path.read_text(encoding="utf-8", errors="replace").splitlines())
    created = create_computer_task(
        settings, goal, direct_mode=True, max_steps=16, max_seconds=240,
        operator_id="linux-e2e", chat_id=chat_id, channel="web",
    )
    if not created.get("ok"):
        return {"case": case, "round": round_index, "scenario": scenario, "setup": setup,
                "status": "error", "error": created.get("error"), "success": False}
    task_id = created["task_id"]
    os.environ["XAUTHORITY"] = authority
    backend = build_backend(settings, task_id)
    desktop_env = getattr(getattr(backend, "desktop", None), "env", {})
    if desktop_env.get("XAUTHORITY") != authority or desktop_env.get("DISPLAY") != f":{display}":
        _refuse("backend desktop is not the private display")
    planner = CodexPlanner(settings, screen_coordinates=True, sandbox="read-only")
    started = time.monotonic()
    result = await run_computer_loop(
        settings, goal, planner=planner, backend=backend, max_steps=16, max_seconds=240,
        direct_mode=True, task_id=task_id, chat_id=chat_id, channel="web",
        operator_id="linux-e2e", open_with_observe=True,
    )
    duration = round(time.monotonic() - started, 3)
    task = get_computer_task(settings, task_id) or {}
    trajectory = _redact_trajectory(task.get("trajectory") or [])
    focus = _focus(display, authority)
    after_pids = set(_firefox_pids(display))
    screenshot_id = str(result.get("screenshot_id") or "")
    copied = _save_screenshot(settings, screenshot_id, artifact / f"{case}-r{round_index:02d}.png")
    summary = str(result.get("summary") or "")
    observes = [item for item in trajectory if item.get("action_type") == "observe" and item.get("screenshot_id")]
    fresh = bool(observes) and observes[-1].get("screenshot_id") == screenshot_id and bool(observes[-1].get("screenshot_hash") or copied)
    app = str(observes[-1].get("active_app") or "") if observes else ""
    foreground = app == "Firefox" or (focus.get("pid") in after_pids and bool(focus.get("window_id")))
    title = str(focus.get("window_title") or "")
    forbidden = _forbidden_count(log_path, log_before)
    code_ok = True if code is None else (code in summary and code in title and code not in goal)
    structural = (
        result.get("status") == "done" and foreground and fresh and forbidden == 0 and bool(screenshot_id)
    )
    if case == "weather":
        success = False
        correct = None
    elif case == "startup":
        success = structural and "Firefox" in (title + app)
        correct = success
    else:
        success = structural and code_ok and code is not None
        correct = bool(code_ok and code is not None)
    row = {
        "case": case,
        "round": round_index,
        "scenario": scenario,
        "setup": setup,
        "goal": goal,
        "task_id": task_id,
        "status": result.get("status"),
        "blocked_reason": result.get("blocked_reason"),
        "summary_correct": correct,
        "result_status": result.get("status"),
        "trajectory": trajectory,
        "screenshot_id": screenshot_id,
        "sha256": copied,
        "window_focus_pid": focus.get("pid"),
        "window_focus_id": focus.get("window_id"),
        "duration_seconds": duration,
        "steps": result.get("steps_used"),
        "owned_launch_count": len(after_pids - before_pids),
        "event_types": _event_types(log_path, log_before),
        "gui_forbidden": forbidden,
        "browser_foreground": foreground,
        "fresh_image": fresh,
        "structural_ok": structural,
        "success": success,
        "requires_supervisor_screenshot_review": case == "weather",
    }
    row["failure_class"] = _classify(row)
    return row


def _metrics(rows: list[dict]) -> dict:
    scored = [row for row in rows if row.get("case") != "weather"]
    pool = scored or rows
    successes = [row for row in pool if row.get("success")]
    launches = [row for row in rows if "closed" == row.get("scenario")]
    launch_ok = [row for row in launches if row.get("browser_foreground")]
    recoveries = [row for row in rows if row.get("scenario") in {"minimized", "launcher", "otherpage"}]
    recovery_ok = [row for row in recoveries if row.get("success") or row.get("structural_ok")]
    false_done = [row for row in rows if row.get("failure_class") == "false_completion"]
    steps = [row.get("steps") or 0 for row in rows]
    seconds = [row.get("duration_seconds") or 0 for row in rows]
    return {
        "success_percent": round(100.0 * len(successes) / max(1, len(pool)), 1),
        "avg_steps": round(sum(steps) / max(1, len(steps)), 2),
        "avg_seconds": round(sum(seconds) / max(1, len(seconds)), 2),
        "launch_success_percent": round(100.0 * len(launch_ok) / max(1, len(launches)), 1),
        "recovery_success_percent": round(100.0 * len(recovery_ok) / max(1, len(recoveries)), 1),
        "false_completion": len(false_done),
        "repeats": len(rows),
        "failure_classes": sorted({str(row.get("failure_class")) for row in rows}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Real isolated X11 browser loop")
    parser.add_argument("--root", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    if args.rounds < 1 or args.rounds > 20:
        _refuse("rounds must be 1..20")
    root = _check_root(Path(args.root))
    display, authority = _load_manifest(Path(args.manifest), root)
    cases = _parse_cases(args.cases)
    expected = _expected(root)
    artifact = root / "e2e"
    artifact.mkdir(parents=True, exist_ok=True)
    log_path = artifact / "codex-event-types.jsonl"
    wrapper = _write_wrapper(root, log_path)
    settings = _settings(root, wrapper)
    _agent, chat_id = _assign_display(settings, display)
    rows: list[dict] = []
    for case in cases:
        for round_index in range(1, args.rounds + 1):
            scenario = SCENARIOS[(round_index - 1) % len(SCENARIOS)]
            row = asyncio.run(_one(
                settings, case=case, round_index=round_index, scenario=scenario,
                display=display, authority=authority, chat_id=chat_id, expected=expected,
                artifact=artifact, log_path=log_path,
            ))
            rows.append(row)
            print(json.dumps({
                "case": case, "round": round_index, "scenario": scenario,
                "status": row.get("status"), "success": row.get("success"),
                "failure_class": row.get("failure_class"),
            }))
    report = {"metrics": _metrics(rows), "runs": rows}
    (artifact / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["metrics"]))


if __name__ == "__main__":
    main()
