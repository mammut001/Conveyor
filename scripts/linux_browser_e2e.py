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
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PRODUCTION_ENV = Path("/opt/conveyor/.env")
PAGE_TITLE = "Conveyor E2E Test"
SNAP_TEST_XAUTHORITY = "/home/ubuntu/snap/firefox/common/conveyor-pr102-test/Xauthority"
_XAUTH_TEST_MARKER = "conveyor-pr102-test"
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


def _parse_display(value: object) -> int:
    """Accept ``109`` or the session string ``':109'``."""
    if isinstance(value, bool) or value is None:
        _refuse("manifest display missing")
    if isinstance(value, int):
        display = value
    else:
        text = str(value).strip()
        if text.startswith(":"):
            text = text[1:]
        if not text.isdigit():
            _refuse("manifest display missing")
        display = int(text)
    if display in {0, 1} or display < 2:
        _refuse("display 0 and 1 are refused")
    return display


def _accept_xauthority(authority: str, root: Path) -> None:
    """Private test cookie only. A generic ``~/.Xauthority`` is refused.

    The Snap Firefox test cookie is the exact path below. It is mode 0600,
    owned by this user, and lives under the ``conveyor-pr102-test`` marker.
    """
    if not authority or not os.path.isfile(authority):
        _refuse("private X authority missing")
    try:
        info = os.stat(authority)
    except OSError:
        _refuse("private X authority unreadable")
    if stat.S_IMODE(info.st_mode) != 0o600:
        _refuse("X authority mode is not 0600")
    if info.st_uid != os.getuid():
        _refuse("X authority owner mismatch")
    if authority == SNAP_TEST_XAUTHORITY:
        if _XAUTH_TEST_MARKER not in authority:
            _refuse("X authority test marker missing")
        return
    if authority.endswith("/.Xauthority"):
        _refuse("generic home X authority is refused")
    if not _under(Path(authority), root) and not authority.startswith("/tmp/"):
        _refuse("X authority is not a private test cookie")


def _load_manifest(path: Path, root: Path) -> tuple[int, str]:
    if not path.is_file():
        _refuse("manifest missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _refuse("manifest unreadable")
    display = _parse_display(data.get("display"))
    authority = str(data.get("xauthority") or data.get("Xauthority") or "")
    _accept_xauthority(authority, root)
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

    # None keeps the CLI's configured model. Screenshot vision already uses
    # the default DeepSeek Flash path when the operator has not pinned one.
    settings = replace(
        settings,
        codex_bin=str(wrapper),
        codex_model=settings.codex_model,
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
    """Disable known features that ``codex features list`` actually installs.

    ``exec --help`` does not list those names, so it must not be the probe.
    ``web_search="disabled"`` is a real CLI config override.
    """
    flags = ["-c", 'web_search="disabled"']
    try:
        probe = subprocess.run(
            [codex, "features", "list"], capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return flags
    names = set(re.findall(r"[A-Za-z][A-Za-z0-9_]*", f"{probe.stdout}\n{probe.stderr}"))
    for name in DISABLE_FEATURES:
        if name in names:
            flags.extend(["--disable", name])
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


def _proc_text(pid: int, name: str) -> str:
    try:
        raw = (Path("/proc") / str(pid) / name).read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("latin1", "replace")


def _owned_test_firefox(pid: int, display: int) -> bool:
    """Firefox on this exact DISPLAY whose command line uses a conveyor profile.

    A production PID constant is not a guard. Another display's Firefox,
    and a Firefox without the test profile, are left alone.
    """
    proc = Path("/proc") / str(pid)
    try:
        raw = (proc / "environ").read_bytes()
        comm = (proc / "comm").read_text(encoding="utf-8").strip()
        exe = os.path.basename(os.readlink(proc / "exe"))
    except OSError:
        return False
    env: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        env[key.decode("latin1", "replace")] = value.decode("latin1", "replace")
    if env.get("DISPLAY") != f":{display}":
        return False
    if comm not in {"firefox", "firefox-esr", "firefox-bin"} and exe not in {"firefox", "firefox-esr", "firefox-bin"}:
        return False
    command = _proc_text(pid, "cmdline")
    return "--profile" in command and "conveyor-" in command


def _firefox_pids(display: int) -> list[int]:
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    found: list[int] = []
    for entry in proc.iterdir():
        if entry.name.isdigit() and _owned_test_firefox(int(entry.name), display):
            found.append(int(entry.name))
    return found


def _close_own_firefox(display: int) -> list[int]:
    closed = []
    for pid in _firefox_pids(display):
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


def _foreground_evidence(env: dict[str, str]) -> dict:
    """Active window pid and process name. Titles are not copied here."""
    active = _run_x(env, "getactivewindow")
    wid = (active.stdout or "").strip()
    evidence: dict = {"rc": active.returncode}
    if active.returncode != 0 or not wid.isdigit():
        return evidence
    evidence["window_id"] = int(wid)
    pid = _run_x(env, "getwindowpid", wid)
    text = (pid.stdout or "").strip()
    if pid.returncode == 0 and text.isdigit():
        evidence["pid"] = int(text)
        evidence["comm"] = _proc_text(int(text), "comm").strip()
    return evidence


def _setup_scenario(name: str, display: int, authority: str, page: str) -> dict:
    """Verifier-side desktop state. Recorded separately from the task.

    Open, minimized, launcher, and otherpage all go through
    ``LinuxBrowserController.ensure`` on this private display. A raw
    Firefox command would use the default profile and can cross displays.
    """
    from desktop_linux_browser import LinuxBrowserController

    env = _display_env(display, authority)
    record: dict = {"scenario": name, "phase": "setup", "commands": [], "setup_ok": False}
    if name == "closed":
        record["closed_pids"] = _close_own_firefox(display)
        record["remaining_owned_pids"] = _firefox_pids(display)
        record["setup_ok"] = not record["remaining_owned_pids"]
        return record
    if name not in {"open", "minimized", "launcher", "otherpage"}:
        record["setup_error"] = "unknown_scenario"
        return record
    ensured = LinuxBrowserController(env).ensure("Firefox")
    record["ensure"] = {
        key: ensured.get(key) for key in ("ok", "error", "pid", "window_id", "name")
    }
    if not ensured.get("ok"):
        record["setup_error"] = str(ensured.get("error") or "ensure_failed")
        return record
    wid = str(int(ensured["window_id"]))
    record["verified_window_id"] = int(wid)
    if name == "open":
        evidence = _foreground_evidence(env)
        record["foreground"] = evidence
        record["setup_ok"] = evidence.get("window_id") == int(wid) and evidence.get("pid") == int(ensured["pid"])
        return record
    if name == "minimized":
        minimized = _run_x(env, "windowminimize", wid)
        record["commands"].append({"xdotool": "windowminimize", "window_id": int(wid), "rc": minimized.returncode})
        record["setup_ok"] = minimized.returncode == 0
        return record
    if name == "launcher":
        proc = subprocess.Popen(
            ["xfce4-appfinder", "--disable-server"],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        record["commands"].append({"setup_launch": "xfce4-appfinder", "pid": proc.pid})
        evidence = {}
        for _ in range(10):
            evidence = _foreground_evidence(env)
            comm = str(evidence.get("comm") or "")
            if "appfinder" in comm:
                break
            time.sleep(0.3)
        record["foreground"] = evidence
        record["setup_ok"] = "appfinder" in str(evidence.get("comm") or "")
        if not record["setup_ok"]:
            record["setup_error"] = "launcher_not_foreground"
        return record
    typed = _run_x(env, "key", "--window", wid, "ctrl+l")
    record["commands"].append({"xdotool": "key", "window_id": int(wid), "rc": typed.returncode})
    entered = _run_x(env, "type", "--window", wid, "--", page)
    record["commands"].append({"xdotool": "type", "window_id": int(wid), "rc": entered.returncode})
    submitted = _run_x(env, "key", "--window", wid, "Return")
    record["commands"].append({"xdotool": "key", "window_id": int(wid), "rc": submitted.returncode})
    time.sleep(1)
    evidence = _foreground_evidence(env)
    record["foreground"] = evidence
    record["setup_ok"] = (
        typed.returncode == 0 and entered.returncode == 0 and submitted.returncode == 0
        and evidence.get("pid") == int(ensured["pid"])
    )
    if not record["setup_ok"]:
        record["setup_error"] = "otherpage_not_established"
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
            "Using only the on-screen browser, open a real weather webpage and read "
            "today's weather for Montreal, Quebec. The done summary must include the "
            "place, today's temperature, the condition, precipitation when the page "
            "shows it, and the source shown on the page. Do not use a shell, an API, "
            "or a web search tool. If the page fails to load, report that load error "
            "instead of inventing a forecast."
        )
    if case == "stability":
        url = f"{FIXTURE_ORIGIN}/run-{round_index:02d}.html"
    else:
        url = f"{FIXTURE_ORIGIN}/index.html"
    return (
        f"Open {url} in Firefox. Read the current browser tab's document title "
        "(not the large H1 heading inside the page) and the Expected code shown in the page body. "
        "Report the exact tab title and code in the done summary. Use the GUI only. "
        "Read the latest settled screenshot; a window title alone is not the code."
    )


def _code_for(case: str, round_index: int, expected: dict) -> str | None:
    """Read the body code from the filename key. Missing codes are refused.

    The HTML title is the constant ``Conveyor E2E Test``. The code is only
    in the page body, so the key is ``index.html`` or ``run-NN.html``.
    """
    if case == "startup" or case == "weather":
        return None
    key = f"run-{round_index:02d}.html" if case == "stability" else "index.html"
    value = expected.get(key) if isinstance(expected, dict) else None
    if not isinstance(value, str) or not value.strip():
        _refuse(f"missing code for {key}")
    return value.strip()


def _redact_trajectory(trajectory: list) -> list[dict]:
    kept = []
    for entry in trajectory or []:
        if not isinstance(entry, dict):
            continue
        kept.append({
            key: entry.get(key)
            for key in (
                "action_type", "action_redacted", "result_ok", "error", "screenshot_id",
                "screenshot_hash", "pid", "window_id", "active_app", "duration_ms",
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
    if row.get("setup_ok") is False:
        return "setup_not_established"
    if row.get("gui_forbidden"):
        return "gui_tool_use"
    # An unreviewed weather summary is not a known false completion.
    if row.get("case") == "weather" and row.get("status") == "done":
        return "weather_needs_review"
    if row.get("status") == "done" and not row.get("success"):
        return "false_completion"
    if not row.get("browser_foreground"):
        return "browser_not_foreground"
    if not row.get("fresh_image"):
        return "stale_or_missing_image"
    if row.get("status") not in {"done", "error", "stopped", "blocked"}:
        return "incomplete"
    if row.get("success"):
        return "ok"
    return str(row.get("status") or "failed")


class _MeasuredPlanner:
    """Count repeated mutations in memory. The model action is returned unchanged.

    This is a transparent CodexPlanner subclass. The identity tuple is not
    written to the trail, the report, or a log.
    """

    def __init__(self, settings, **kwargs) -> None:
        from desktop_computer_planner import CodexPlanner

        class _Planner(CodexPlanner):
            def __init__(self) -> None:
                super().__init__(settings, **kwargs)
                self._seen: dict[tuple, int] = {}

            async def next_action(self, **call):
                action = await super().next_action(**call)
                self._note(action, call.get("observation") or {})
                return action

            def _note(self, action: dict, observation: dict) -> None:
                act = str((action or {}).get("action") or "")
                if act not in {"click", "type", "hotkey", "scroll"}:
                    return
                if not isinstance(observation, dict):
                    return
                if observation.get("browser_page_state") == "loading":
                    return
                from desktop_computer_loop import _focus_identity, _mutation_identity

                key = (
                    _mutation_identity(action),
                    observation.get("sha256"),
                    _focus_identity(observation),
                )
                self._seen[key] = self._seen.get(key, 0) + 1

            @property
            def repeated_unchanged_operations(self) -> int:
                return sum(count - 1 for count in self._seen.values() if count > 1)

        self._planner = _Planner()

    def __getattr__(self, name: str):
        return getattr(self._planner, name)

    async def next_action(self, **kwargs):
        return await self._planner.next_action(**kwargs)

    @property
    def repeated_unchanged_operations(self) -> int:
        return self._planner.repeated_unchanged_operations


async def _one(settings, *, case: str, round_index: int, scenario: str, display: int,
               authority: str, chat_id: str, expected: dict, artifact: Path, log_path: Path) -> dict:
    from desktop_computer_loop import build_backend, run_computer_loop
    from desktop_computer_requests import create_computer_task, get_computer_task

    goal = _goal(case, round_index)
    code = _code_for(case, round_index, expected)
    if code and code in goal:
        _refuse("expected code leaked into the goal")
    other = f"{FIXTURE_ORIGIN}/run-{((round_index % 20) + 1):02d}.html"
    setup = _setup_scenario(scenario, display, authority, other)
    if not setup.get("setup_ok"):
        return {
            "case": case, "round": round_index, "scenario": scenario, "setup": setup,
            "setup_ok": False, "status": "error", "success": False,
            "failure_class": "setup_not_established", "structural_ok": False,
            "summary_correct": False, "browser_foreground": False, "fresh_image": False,
            "gui_forbidden": 0, "steps": 0, "duration_seconds": 0,
        }
    before_pids = set(_firefox_pids(display))
    # A public weather site can require city selection and consent screens;
    # the controlled one-page fixture keeps its original, smaller budget.
    max_steps = 32 if case == "weather" else 16
    log_before = 0
    if log_path.is_file():
        log_before = len(log_path.read_text(encoding="utf-8", errors="replace").splitlines())
    created = create_computer_task(
        settings, goal, direct_mode=True, max_steps=max_steps, max_seconds=240,
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
    planner = _MeasuredPlanner(settings, screen_coordinates=True, sandbox="read-only")
    started = time.monotonic()
    result = await run_computer_loop(
        settings, goal, planner=planner, backend=backend, max_steps=max_steps, max_seconds=240,
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
    observed_title = str(focus.get("window_title") or "")
    forbidden = _forbidden_count(log_path, log_before)
    # The code lives in the page body. Requiring it in the window title
    # fails a valid page whose title is the constant fixture title.
    code_ok = True if code is None else (
        code in summary and PAGE_TITLE in summary and PAGE_TITLE in observed_title and code not in goal
    )
    structural = (
        result.get("status") == "done" and foreground and fresh and forbidden == 0 and bool(screenshot_id)
    )
    if case == "weather":
        success = False
        correct = None
    elif case == "startup":
        success = structural and app == "Firefox"
        correct = success
    else:
        success = structural and code_ok and code is not None
        correct = bool(code_ok and code is not None)
    row = {
        "case": case,
        "round": round_index,
        "scenario": scenario,
        "setup": setup,
        "setup_ok": True,
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
        "repeated_unchanged_operations": planner.repeated_unchanged_operations,
        "owned_launch_count": len(after_pids - before_pids),
        "event_types": _event_types(log_path, log_before),
        "gui_forbidden": forbidden,
        "browser_foreground": foreground,
        "fresh_image": fresh,
        "structural_ok": structural,
        "success": success,
        "requires_supervisor_screenshot_review": case == "weather",
        "observed_title": observed_title,
        "summary_sanitized": _sanitized_summary(summary) if case in {"local", "weather"} else None,
    }
    row["failure_class"] = _classify(row)
    return row


def _sanitized_summary(text: str) -> str:
    """Done-summary readback. No prompts and no raw tool arguments."""
    return " ".join(str(text or "").split())[:500]


def _repeat_count(rows: list[dict]) -> int | str:
    """Sum per-round counts. Zero is a measurement, not a missing probe.

    The count is identical mutating model actions against the same pre-action
    screenshot and focus. It is an indicator, not proof the GUI failed.
    Rows that never ran the measuring planner stay out of a zero report.
    """
    executed = [row for row in rows if row.get("task_id")]
    measured = [
        row.get("repeated_unchanged_operations")
        for row in executed
        if isinstance(row.get("repeated_unchanged_operations"), int)
    ]
    if len(measured) != len(executed):
        return "NOT_MEASURED"
    return sum(measured)


def _metrics(rows: list[dict]) -> dict:
    # Only the stability rounds are the success statistic. Startup, local,
    # and weather are separate single checks.
    pool = [row for row in rows if row.get("case") == "stability"]
    successes = [row for row in pool if row.get("success")]
    launches = [row for row in rows if row.get("scenario") == "closed"]
    launch_ok = [row for row in launches if row.get("browser_foreground")]
    recoveries = [
        row for row in rows
        if row.get("scenario") in {"minimized", "launcher", "otherpage"}
        and row.get("case") in {"local", "stability"}
    ]
    recovery_ok = [row for row in recoveries if row.get("success") is True]
    false_done = [row for row in rows if row.get("failure_class") == "false_completion"]
    steps = [row.get("steps") or 0 for row in pool or rows]
    seconds = [row.get("duration_seconds") or 0 for row in pool or rows]
    return {
        "success_percent": round(100.0 * len(successes) / len(pool), 1) if pool else None,
        "stability_rounds": len(pool),
        "total_rounds": len(rows),
        "avg_steps": round(sum(steps) / max(1, len(steps)), 2),
        "avg_seconds": round(sum(seconds) / max(1, len(seconds)), 2),
        "launch_success_percent": round(100.0 * len(launch_ok) / max(1, len(launches)), 1),
        "recovery_success_percent": round(100.0 * len(recovery_ok) / max(1, len(recoveries)), 1) if recoveries else None,
        "false_completion": len(false_done),
        "repeats": _repeat_count(rows),
        "repeats_definition": (
            "identical click/type/hotkey/scroll model actions retried against the same "
            "pre-action screenshot and focus; wait/observe/loading excluded; "
            "indicator, not proof of a GUI failure"
        ),
        "failure_classes": sorted({str(row.get("failure_class")) for row in rows}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Real isolated X11 browser loop")
    parser.add_argument("--root", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--rounds", type=int, default=None)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    if args.rounds is not None and (args.rounds < 1 or args.rounds > 20):
        _refuse("rounds must be 1..20")
    root = _check_root(Path(args.root))
    display, authority = _load_manifest(Path(args.manifest), root)
    cases = _parse_cases(args.cases)
    expected = _expected(root)
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    artifact = root / "e2e" / run_id
    artifact.mkdir(parents=True, exist_ok=True)
    log_path = artifact / "codex-event-types.jsonl"
    wrapper = _write_wrapper(root, log_path)
    settings = _settings(root, wrapper)
    _agent, chat_id = _assign_display(settings, display)
    rows: list[dict] = []

    def publish() -> None:
        report = {"run_id": run_id, "metrics": _metrics(rows), "runs": rows}
        (artifact / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    for case in cases:
        # Stability is the 20-round statistic. The other cases run once
        # unless --rounds was set explicitly.
        count = args.rounds if args.rounds is not None else (20 if case == "stability" else 1)
        for round_index in range(1, count + 1):
            scenario = SCENARIOS[(round_index - 1) % len(SCENARIOS)]
            try:
                row = asyncio.run(_one(
                    settings, case=case, round_index=round_index, scenario=scenario,
                    display=display, authority=authority, chat_id=chat_id, expected=expected,
                    artifact=artifact, log_path=log_path,
                ))
            except Exception as exc:
                row = {
                    "case": case, "round": round_index, "scenario": scenario,
                    "status": "error", "success": False, "failure_class": "incomplete",
                    "error_type": type(exc).__name__, "setup_ok": None,
                }
            rows.append(row)
            publish()
            print(json.dumps({
                "case": case, "round": round_index, "scenario": scenario,
                "status": row.get("status"), "success": row.get("success"),
                "failure_class": row.get("failure_class"), "run_id": run_id,
            }))
    publish()
    print(json.dumps(_metrics(rows)))


if __name__ == "__main__":
    main()
