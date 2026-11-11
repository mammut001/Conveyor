#!/usr/bin/env python3
"""scripts/agent_supervisor_live.py

Opt-in real-model integration harness for Conveyor Agent Supervisor with AGY.

Features:
- Standalone disposable git repo, managed worktree, and isolated DB/memory roots.
- External frozen validator outside implementer worktree testing all clamp() edge cases:
  normal, inclusive boundaries, negative values/intervals, floats, out of range,
  and ValueError on invalid lower > upper.
- Frozen validator SHA-256 integrity check (asserted before and after flow execution).
- Isolated Settings with host-configured operator validation command (sys.executable -B).
- Controlled fault-injection proxy wrapping get_executor: appends a single explicit
  instruction to the first implementer invocation to commit an incomplete clamp
  (returning value unchanged), verifying validation rejection and repair handling.
- Subsequent repair rounds run with unmodified supervisor prompts.
- Public supervisor lifecycle: create_task -> create_worktree -> queue_task -> claim_task -> run_supervisor_flow.
- Independent post-completion validation run against candidate worktree.
- Optional --test-invalidation post-completion phase mutating a worktree sentinel.
- Machine-readable redacted JSON report and clean preflight / dry-run mode when
  --run-real-models is not supplied.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

# Ensure conveyor root is importable
CONVEYOR_ROOT = Path(__file__).resolve().parents[1]
if str(CONVEYOR_ROOT) not in sys.path:
    sys.path.insert(0, str(CONVEYOR_ROOT))

from conveyor.core.config import Settings
from conveyor.agents import agent_supervisor as ags
from conveyor.core.redaction import redact_obj, redact_text
from conveyor.core import subprocess_env

log = logging.getLogger("agent_supervisor_live")

# Source code of the external frozen validator placed outside the worktree.
FROZEN_VALIDATOR_SOURCE = '''#!/usr/bin/env python3
"""External frozen validator for clamp(value, lower, upper).

Audited edge-case suites:
1. Normal: value within [lower, upper] returns value unchanged
2. Inclusive boundaries: clamp(lower, lower, upper) == lower, clamp(upper, lower, upper) == upper
3. Negative values and negative intervals: clamp(-5, -10, -1) == -5, clamp(-15, -10, -1) == -10, clamp(0, -10, -1) == -1
4. Floating point numbers: clamp(3.5, 1.0, 5.0) == 3.5, clamp(0.5, 1.0, 5.0) == 1.0, clamp(5.5, 1.0, 5.0) == 5.0
5. Out of range: clamp(-100, 0, 10) == 0, clamp(100, 0, 10) == 10
6. Invalid bounds: lower > upper raises ValueError
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

# Enforce no bytecode generation in validator execution
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def test_clamp_function(clamp_fn) -> None:
    # 1. Normal cases
    assert clamp_fn(5, 0, 10) == 5, f"clamp(5, 0, 10) expected 5, got {clamp_fn(5, 0, 10)}"
    assert clamp_fn(0, -5, 5) == 0, f"clamp(0, -5, 5) expected 0, got {clamp_fn(0, -5, 5)}"
    assert clamp_fn(7, 7, 7) == 7, f"clamp(7, 7, 7) expected 7, got {clamp_fn(7, 7, 7)}"

    # 2. Inclusive boundaries
    assert clamp_fn(0, 0, 10) == 0, f"clamp(0, 0, 10) expected 0, got {clamp_fn(0, 0, 10)}"
    assert clamp_fn(10, 0, 10) == 10, f"clamp(10, 0, 10) expected 10, got {clamp_fn(10, 0, 10)}"
    assert clamp_fn(-10, -10, 10) == -10, f"clamp(-10, -10, 10) expected -10, got {clamp_fn(-10, -10, 10)}"

    # 3. Negative values and negative intervals
    assert clamp_fn(-5, -10, -1) == -5, f"clamp(-5, -10, -1) expected -5, got {clamp_fn(-5, -10, -1)}"
    assert clamp_fn(-15, -10, -1) == -10, f"clamp(-15, -10, -1) expected -10, got {clamp_fn(-15, -10, -1)}"
    assert clamp_fn(0, -10, -1) == -1, f"clamp(0, -10, -1) expected -1, got {clamp_fn(0, -10, -1)}"

    # 4. Floating point numbers
    assert clamp_fn(3.5, 1.0, 5.0) == 3.5, f"clamp(3.5, 1.0, 5.0) expected 3.5, got {clamp_fn(3.5, 1.0, 5.0)}"
    assert clamp_fn(0.5, 1.0, 5.0) == 1.0, f"clamp(0.5, 1.0, 5.0) expected 1.0, got {clamp_fn(0.5, 1.0, 5.0)}"
    assert clamp_fn(5.5, 1.0, 5.0) == 5.0, f"clamp(5.5, 1.0, 5.0) expected 5.0, got {clamp_fn(5.5, 1.0, 5.0)}"
    assert clamp_fn(2.0, 1.5, 2.5) == 2.0, f"clamp(2.0, 1.5, 2.5) expected 2.0, got {clamp_fn(2.0, 1.5, 2.5)}"

    # 5. Out of range values
    assert clamp_fn(-100, 0, 10) == 0, f"clamp(-100, 0, 10) expected 0, got {clamp_fn(-100, 0, 10)}"
    assert clamp_fn(100, 0, 10) == 10, f"clamp(100, 0, 10) expected 10, got {clamp_fn(100, 0, 10)}"
    assert clamp_fn(-999.9, -50.0, 50.0) == -50.0, f"clamp(-999.9, -50.0, 50.0) expected -50.0, got {clamp_fn(-999.9, -50.0, 50.0)}"

    # 6. Invalid bounds (lower > upper) must raise ValueError
    try:
        clamp_fn(5, 10, 0)
    except ValueError:
        pass
    except Exception as exc:
        raise AssertionError(f"clamp(5, 10, 0) raised {type(exc).__name__} instead of ValueError: {exc}")
    else:
        raise AssertionError("clamp(5, 10, 0) did not raise ValueError for lower > upper (10 > 0)")

    try:
        clamp_fn(0, 1.0, 0.0)
    except ValueError:
        pass
    except Exception as exc:
        raise AssertionError(f"clamp(0, 1.0, 0.0) raised {type(exc).__name__} instead of ValueError: {exc}")
    else:
        raise AssertionError("clamp(0, 1.0, 0.0) did not raise ValueError for lower > upper (1.0 > 0.0)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Frozen external validator for clamp(value, lower, upper)")
    parser.add_argument("worktree_pos", nargs="?", default="", help="Optional positional worktree path")
    parser.add_argument("--worktree", default="", help="Optional worktree path")
    args = parser.parse_args()

    target_dir_str = args.worktree or args.worktree_pos or "."
    target_dir = Path(target_dir_str).resolve()
    clamp_py = target_dir / "clamp.py"

    if not clamp_py.is_file():
        print(f"VALIDATION FAILED: clamp.py not found in {target_dir}", file=sys.stderr)
        return 1

    spec = importlib.util.spec_from_file_location("clamp_test_module", str(clamp_py))
    if spec is None or spec.loader is None:
        print(f"VALIDATION FAILED: could not load module spec for {clamp_py}", file=sys.stderr)
        return 1

    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:
        print(f"VALIDATION FAILED: error importing clamp.py: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    clamp_fn = getattr(mod, "clamp", None)
    if not callable(clamp_fn):
        print("VALIDATION FAILED: clamp function not found or not callable in clamp.py", file=sys.stderr)
        return 1

    try:
        test_clamp_function(clamp_fn)
    except AssertionError as exc:
        print(f"VALIDATION FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"VALIDATION ERROR: unexpected exception {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("VALIDATION PASSED: all clamp edge cases verified (normal, inclusive, negative, float, out-of-range, ValueError on invalid bounds).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

CLAMP_STUB_SOURCE = '''"""clamp implementation stub."""
from __future__ import annotations


def clamp(value: int | float, lower: int | float, upper: int | float) -> int | float:
    """Return value clamped between lower and upper bounds.

    Raises:
        ValueError: if lower > upper.
    """
    # Stub: unexercised initial repository state
    raise NotImplementedError("clamp is not yet implemented")
'''


class ImplementationCounter:
    """Thread-safe persistent counter for implementation attempts across proxy instances."""

    def __init__(self) -> None:
        self.count = 0
        self.lock = threading.Lock()

    def next_attempt(self) -> int:
        with self.lock:
            val = self.count
            self.count += 1
            return val


class FaultInjectionProxyExecutor:
    """Proxy delegating available(), cancel(), and run() to the actual AGY executor adapter.

    Appends an explicit controlled fault-injection instruction to the prompt ONLY on
    the first implementer invocation (attempt 0). Never synthesizes FakeExecutor results;
    all child executions, evidence files, and verdicts are produced by the real adapter.
    """

    def __init__(self, actual_executor: Any, counter: ImplementationCounter) -> None:
        self._actual = actual_executor
        self._counter = counter
        self.name = getattr(actual_executor, "name", "antigravity")

    def available(self) -> tuple[bool, str]:
        return self._actual.available()

    async def cancel(self, execution_id: str) -> bool:
        return await self._actual.cancel(execution_id)

    async def run(
        self,
        *,
        role: Any,
        prompt: str,
        worktree_path: str,
        timeout_seconds: int,
        task_id: str = "",
        context: Any = None,
        json_schema: dict[str, Any] | None = None,
    ) -> Any:
        effective_prompt = prompt
        role_name = getattr(role, "name", "")
        if role_name == "implementer":
            attempt = self._counter.next_attempt()
            if attempt == 0:
                fault_instruction = (
                    "\n\n[CONTROLLED FAULT INJECTION SCENARIO - ROUND 1 INTEGRATION TEST]:\n"
                    "For this FIRST implementation attempt only, intentionally implement `clamp(value, lower, upper)`\n"
                    "in `clamp.py` to simply return `value` unchanged:\n"
                    "```python\n"
                    "def clamp(value, lower, upper):\n"
                    "    return value\n"
                    "```\n"
                    "Commit this incomplete implementation to git with git commit (do not amend unrelated history).\n"
                    "Do NOT implement range clamping or ValueError validation on this attempt.\n"
                    "This controlled incomplete implementation is required by the test harness to exercise\n"
                    "operator validation failure, rejection recording, and the supervisor repair loop."
                )
                effective_prompt = prompt + fault_instruction

        kwargs: dict[str, Any] = {
            "role": role,
            "prompt": effective_prompt,
            "worktree_path": worktree_path,
            "timeout_seconds": timeout_seconds,
            "task_id": task_id,
            "context": context,
        }
        if json_schema is not None:
            kwargs["json_schema"] = json_schema

        return await self._actual.run(**kwargs)


def resolve_agy_binary(candidate: str | None = None) -> str:
    """Resolve and validate the AGY binary path.

    Checks:
    1. Explicit candidate (if passed) - resolved via PATH if bare command, or expanded if path.
    2. CONVEYOR_ANTIGRAVITY_BIN or CONVEYOR_AGY_BIN environment variables.
    3. 'agy' via shutil.which(PATH).
    4. Fallback to Path.home() / '.local' / 'bin' / 'agy'.
    """
    raw = (
        candidate
        or os.getenv("CONVEYOR_ANTIGRAVITY_BIN")
        or os.getenv("CONVEYOR_AGY_BIN")
        or "agy"
    )
    raw_str = str(raw).strip()

    target_path: Path | None = None
    if "/" in raw_str or "\\" in raw_str:
        p = Path(raw_str).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            target_path = p.resolve()
    else:
        which_path = shutil.which(raw_str)
        if which_path:
            p = Path(which_path).resolve()
            if p.is_file() and os.access(p, os.X_OK):
                target_path = p

    if target_path is None:
        home_fallback = (Path.home() / ".local" / "bin" / "agy").resolve()
        if home_fallback.is_file() and os.access(home_fallback, os.X_OK):
            target_path = home_fallback

    if target_path is None:
        raise FileNotFoundError(
            f"AGY binary not found or not executable (checked {raw_str!r}, PATH, and {Path.home() / '.local/bin/agy'})"
        )
    return str(target_path)


def prepare_root_directory(candidate: str | None = None) -> Path:
    """Prepare and validate isolated root directory.

    If candidate is provided:
      - If path exists and is not a directory: refuse.
      - If directory exists and contains any files/subdirectories: refuse immediately to prevent clobbering.
      - If directory exists and is empty: accept it.
      - If directory does not exist: create it cleanly.
    If candidate is not provided:
      - Create a fresh unique temporary directory with mkdtemp.
    Never automatically clean up or delete failed directories or evidence.
    """
    if candidate:
        root = Path(candidate).expanduser().resolve()
        if root.exists():
            if not root.is_dir():
                raise RuntimeError(f"Specified --root exists but is not a directory: {root}")
            entries = list(root.iterdir())
            if entries:
                raise RuntimeError(
                    f"Specified --root directory is not empty ({len(entries)} items found: "
                    f"{', '.join(e.name for e in entries[:5])}). Refusing to clobber existing workspace: {root}"
                )
        else:
            root.mkdir(parents=True, exist_ok=False)
        return root

    return Path(tempfile.mkdtemp(prefix="conveyor-supervisor-live-")).resolve()


def resolve_report_path(root: Path, custom_path: str | None = None) -> Path:
    """Resolve report output path and guard against clobbering unrelated files."""
    if not custom_path:
        return root / "agent-supervisor-live-report.json"
    p = Path(custom_path).expanduser().resolve()
    if p.exists():
        if p.is_dir():
            raise RuntimeError(f"Specified report path is a directory: {p}")
        raise RuntimeError(f"Specified report path already exists; refusing to overwrite: {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def write_frozen_validator(dest_path: Path) -> str:
    """Write the external frozen validator script and return its SHA-256 digest."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(FROZEN_VALIDATOR_SOURCE, encoding="utf-8")
    os.chmod(dest_path, 0o755)
    digest = hashlib.sha256(dest_path.read_bytes()).hexdigest()
    return digest


def setup_disposable_repo(repo_dir: Path) -> str:
    """Initialize a clean git repository with initial commit containing clamp stub."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Conveyor Supervisor Live"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "supervisor-live@example.com"], cwd=repo_dir, check=True, capture_output=True)

    gitignore = repo_dir / ".gitignore"
    gitignore.write_text("__pycache__/\n*.pyc\n.DS_Store\n", encoding="utf-8")

    clamp_file = repo_dir / "clamp.py"
    clamp_file.write_text(CLAMP_STUB_SOURCE, encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit with clamp stub"], cwd=repo_dir, check=True, capture_output=True)

    base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
    return base_sha


def run_frozen_validator_cmd(
    validator_path: Path,
    worktree_path: Path,
    *,
    timeout_seconds: float = 30.0,
    max_bytes: int = 128 * 1024,
) -> tuple[int, str]:
    """Execute external frozen validator with credential-free environment and bounded I/O.

    - Uses subprocess_env.build_child_env(grants=()) so no provider keys/secrets are inherited.
    - Sets PYTHONDONTWRITEBYTECODE=1 to prevent bytecode artifact creation.
    - Spawns child in its own process group (start_new_session=True).
    - Actively reads and caps combined stdout/stderr bytes using a selector loop.
    - Kills and reaps the entire process group immediately on deadline expiry, byte overflow, or errors.
    - Returns (returncode, combined_output).
    """
    child_env = subprocess_env.build_child_env(
        grants=(),
        extra=("PYTHONDONTWRITEBYTECODE",),
        overrides={"PYTHONDONTWRITEBYTECODE": "1"},
    )
    # Guarantee PYTHONDONTWRITEBYTECODE is set
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"

    cmd = [sys.executable, "-B", str(validator_path), "--worktree", str(worktree_path)]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(worktree_path),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except Exception as exc:
        return 1, f"VALIDATION ERROR: failed to spawn validator process: {type(exc).__name__}: {exc}"

    pgid = proc.pid
    sel = selectors.DefaultSelector()
    if proc.stdout:
        os.set_blocking(proc.stdout.fileno(), False)
        sel.register(proc.stdout, selectors.EVENT_READ, data="stdout")
    if proc.stderr:
        os.set_blocking(proc.stderr.fileno(), False)
        sel.register(proc.stderr, selectors.EVENT_READ, data="stderr")

    deadline = time.monotonic() + max(0.05, float(timeout_seconds))
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    total_bytes = 0
    kill_reason = ""
    rc: int | None = None

    def _kill_and_reap():
        ags.kill_process_group(pgid, graceful=False, reap_children=True)
        try:
            proc.wait(timeout=0.5)
        except Exception:
            pass
        sel.close()

    def _group_is_provably_dead() -> bool:
        if ags._pgid_alive(pgid) is False:
            return True
        # Bounded grace (up to 50ms) in case kernel process table is finalizing leader exit
        grace_end = time.monotonic() + 0.05
        while time.monotonic() < grace_end:
            time.sleep(0.01)
            if ags._pgid_alive(pgid) is False:
                return True
        return False

    try:
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                kill_reason = f"VALIDATION FAILED: execution timed out after {timeout_seconds}s"
                rc = 124
                _kill_and_reap()
                break

            events = sel.select(timeout=max(0.01, min(remaining, 0.1)))
            overflow = False
            for key, mask in events:
                stream = key.fileobj
                label = key.data
                try:
                    chunk = os.read(stream.fileno(), 4096)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    chunk = b""

                if chunk:
                    total_bytes += len(chunk)
                    if label == "stdout":
                        out_chunks.append(chunk)
                    else:
                        err_chunks.append(chunk)
                    if total_bytes > max_bytes:
                        kill_reason = f"VALIDATION FAILED: output limit exceeded ({max_bytes} bytes cap)"
                        rc = 125
                        _kill_and_reap()
                        overflow = True
                        break
                else:
                    sel.unregister(stream)

            if overflow:
                break

            # Handle leader exit (including when descendants inherit open stdio pipes)
            leader_status = proc.poll()
            if leader_status is not None:
                # Drain remaining buffered data from any open streams without blocking
                for key in list(sel.get_map().values()):
                    stream = key.fileobj
                    try:
                        while True:
                            c = os.read(stream.fileno(), 4096)
                            if not c:
                                break
                            total_bytes += len(c)
                            if key.data == "stdout":
                                out_chunks.append(c)
                            else:
                                err_chunks.append(c)
                            if total_bytes > max_bytes:
                                kill_reason = f"VALIDATION FAILED: output limit exceeded ({max_bytes} bytes cap)"
                                rc = 125
                                _kill_and_reap()
                                overflow = True
                                break
                    except (BlockingIOError, OSError):
                        pass
                    if not overflow:
                        sel.unregister(stream)
                if overflow:
                    break

                rc = proc.wait()
                sel.close()

                # Guard: Verify that no background descendant processes remain in the process group
                if not _group_is_provably_dead():
                    kill_reason = (
                        f"VALIDATION FAILED: lingering background descendant detected in process group {pgid} after leader exit"
                    )
                    rc = 126
                    _kill_and_reap()
                break

            if not sel.get_map():
                try:
                    rc = proc.wait(timeout=max(0.01, remaining))
                except subprocess.TimeoutExpired:
                    kill_reason = f"VALIDATION FAILED: execution timed out after {timeout_seconds}s"
                    rc = 124
                    _kill_and_reap()
                else:
                    sel.close()
                    if not _group_is_provably_dead():
                        kill_reason = (
                            f"VALIDATION FAILED: lingering background descendant detected in process group {pgid} after leader exit"
                        )
                        rc = 126
                        _kill_and_reap()
                break
    except Exception as exc:
        _kill_and_reap()
        return 1, f"VALIDATION ERROR: unexpected runner failure: {type(exc).__name__}: {exc}"
    finally:
        try:
            sel.close()
        except Exception:
            pass
        if proc.stdout and not proc.stdout.closed:
            try:
                proc.stdout.close()
            except Exception:
                pass
        if proc.stderr and not proc.stderr.closed:
            try:
                proc.stderr.close()
            except Exception:
                pass

    combined = (b"".join(out_chunks) + b"\n" + b"".join(err_chunks)).decode("utf-8", "replace").strip()
    if kill_reason:
        combined = f"{kill_reason}\n{combined}".strip()
    return rc if rc is not None else 1, combined


def run_preflight_checks(
    root: Path,
    agy_bin: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Perform preflight checks without executing real models."""
    preflight_dir = root / "preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)
    validator_path = preflight_dir / "validate_clamp.py"
    val_sha = write_frozen_validator(validator_path)

    # 1. Test validator against stub (expected to fail with NotImplementedError)
    stub_dir = preflight_dir / "stub_test"
    stub_dir.mkdir(parents=True, exist_ok=True)
    (stub_dir / "clamp.py").write_text(CLAMP_STUB_SOURCE, encoding="utf-8")
    stub_rc, stub_out = run_frozen_validator_cmd(validator_path, stub_dir)
    assert stub_rc != 0, f"Validator unexpectedly passed against stub: {stub_out}"

    # 2. Test validator against reference passing implementation
    good_dir = preflight_dir / "good_test"
    good_dir.mkdir(parents=True, exist_ok=True)
    good_clamp = (
        "def clamp(value, lower, upper):\n"
        "    if lower > upper:\n"
        "        raise ValueError('lower must not exceed upper')\n"
        "    if value < lower:\n"
        "        return lower\n"
        "    if value > upper:\n"
        "        return upper\n"
        "    return value\n"
    )
    (good_dir / "clamp.py").write_text(good_clamp, encoding="utf-8")
    good_rc, good_out = run_frozen_validator_cmd(validator_path, good_dir)
    assert good_rc == 0, f"Validator failed against reference clamp: {good_out}"

    # 3. Test git repo creation
    test_repo = preflight_dir / "repo"
    base_sha = setup_disposable_repo(test_repo)
    assert len(base_sha) == 40, f"Invalid base SHA: {base_sha}"

    # 4. Verify agy binary is present
    agy_ok = Path(agy_bin).is_file() and os.access(agy_bin, os.X_OK)

    # 5. Verify isolated Settings construction
    settings = Settings(
        telegram_bot_token="",
        telegram_allowed_user_id=0,
        codex_workspace_root=preflight_dir,
        codex_bin="codex",
        codex_task_root=preflight_dir / "tasks",
        codex_model=None,
        codex_timeout_seconds=args.timeout,
        telegram_progress_seconds=3,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=preflight_dir / "memory",
        user_timezone="UTC",
        agent_supervisor_enabled=True,
        agent_supervisor_antigravity_bin=agy_bin,
    )

    # 6. Verify public lifecycle up to dispatching without running models
    task = ags.create_task(settings, goal="Preflight clamp validation", repo_path=str(test_repo), base_sha=base_sha)
    task_id = task["id"]
    wp = ags.create_worktree(settings, str(test_repo), task_id)
    queued = ags.queue_task(
        settings,
        task_id,
        worktree_path=wp,
        acceptance_criteria="Preflight criteria",
        goal="Preflight clamp validation",
    )
    claimed = ags.claim_task(settings)
    assert claimed is not None and claimed["id"] == task_id and claimed["state"] == "dispatching"

    # Clean up lock/task safely
    ags.cancel_task(settings, task_id, reason="preflight complete")

    report = {
        "status": "PREFLIGHT_PASS",
        "run_real_models_optin": False,
        "agy_bin": agy_bin,
        "agy_binary_available": agy_ok,
        "validator_sha256": val_sha,
        "validator_stub_rejected": bool(stub_rc != 0),
        "validator_reference_accepted": bool(good_rc == 0),
        "git_repo_initialized": bool(base_sha),
        "lifecycle_check": "create_task -> create_worktree -> queue_task -> claim_task verified",
        "message": "Preflight checks passed. To execute live AGY model flow, invoke with --run-real-models.",
        "root": str(root),
    }
    return report


async def run_live_flow(
    root: Path,
    agy_bin: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Execute the full live integration test with real AGY models."""
    workspace_dir = root / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = root / "repo"
    tasks_dir = root / "tasks"
    memory_dir = root / "memory"
    validators_dir = root / "validators"

    # 1. Setup git repo and external frozen validator
    base_sha = setup_disposable_repo(repo_dir)
    validator_path = validators_dir / "validate_clamp.py"
    validator_sha_before = write_frozen_validator(validator_path)

    # 2. Build isolated Settings
    settings = Settings(
        telegram_bot_token="",
        telegram_allowed_user_id=0,
        codex_workspace_root=workspace_dir,
        codex_bin="codex",
        codex_task_root=tasks_dir,
        codex_model=None,
        codex_timeout_seconds=args.timeout,
        telegram_progress_seconds=3,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=memory_dir,
        user_timezone="UTC",
        agent_supervisor_enabled=True,
        agent_supervisor_antigravity_bin=agy_bin,
        agent_supervisor_analyst_executor="antigravity",
        agent_supervisor_implementer_executor="antigravity",
        agent_supervisor_reviewer_executor="antigravity",
        agent_supervisor_analyst_model=args.analyst_model,
        agent_supervisor_implementer_model=args.implementer_model,
        agent_supervisor_reviewer_model=args.reviewer_model,
        agent_supervisor_task_timeout_seconds=args.timeout,
        agent_supervisor_max_repair_rounds=args.repair_rounds,
    )

    goal = (
        "Implement the function clamp(value, lower, upper) in clamp.py so that it clamps value "
        "to the inclusive range [lower, upper]. If lower > upper, raise ValueError. Support integers and floats."
    )
    acceptance_criteria = (
        "1. Normal cases: within range returns value unchanged (e.g. clamp(5, 0, 10) == 5).\n"
        "2. Inclusive boundaries: clamp(lower, lower, upper) == lower, clamp(upper, lower, upper) == upper.\n"
        "3. Negative numbers: clamp(-5, -10, -1) == -5, clamp(-15, -10, -1) == -10, clamp(0, -10, -1) == -1.\n"
        "4. Float values: clamp(3.5, 1.0, 5.0) == 3.5, clamp(0.5, 1.0, 5.0) == 1.0, clamp(5.5, 1.0, 5.0) == 5.0.\n"
        "5. Out of range: clamp(-100, 0, 10) == 0, clamp(100, 0, 10) == 10.\n"
        "6. Invalid bounds: lower > upper raises ValueError.\n"
        "7. Clean commit in clamp.py; external frozen validation must pass."
    )

    # 3. Create task and managed worktree
    task = ags.create_task(settings, goal=goal, repo_path=str(repo_dir), base_sha=base_sha)
    task_id = task["id"]
    worktree_path = ags.create_worktree(settings, str(repo_dir), task_id)

    # 4. Configure operator-side validation commands in Settings
    val_cmd = (sys.executable, "-B", str(validator_path), "--worktree", str(worktree_path))
    settings = replace(
        settings,
        agent_supervisor_validation_commands=(("clamp_frozen_validation", val_cmd),),
    )

    # 5. Queue and claim task
    roles_override = {
        "coordinator": {"executor": "chat"},
        "analyst": {"executor": "antigravity", "model": args.analyst_model},
        "implementer": {"executor": "antigravity", "model": args.implementer_model},
        "reviewer": {"executor": "antigravity", "model": args.reviewer_model},
    }
    ags.queue_task(
        settings,
        task_id,
        worktree_path=worktree_path,
        acceptance_criteria=acceptance_criteria,
        goal=goal,
        roles=roles_override,
    )
    claimed_task = ags.claim_task(settings)
    if claimed_task is None or claimed_task.get("state") != "dispatching":
        raise RuntimeError(f"Failed to claim task {task_id} into dispatching state")

    # 6. Hook get_executor with controlled fault-injection proxy
    counter = ImplementationCounter()
    original_get_executor = ags.get_executor

    def proxied_get_executor(st: Any, name: str) -> Any:
        ex = original_get_executor(st, name)
        if str(name).strip().lower() == "antigravity":
            return FaultInjectionProxyExecutor(ex, counter)
        return ex

    flow_error: str | None = None
    final_task: dict[str, Any] = {}
    ags.get_executor = proxied_get_executor
    try:
        final_task = await asyncio.wait_for(
            ags.run_supervisor_flow(
                settings,
                task_id,
                goal=goal,
                worktree_path=worktree_path,
                acceptance_criteria=acceptance_criteria,
                max_repairs=args.repair_rounds,
            ),
            timeout=float(args.timeout),
        )
    except Exception as exc:
        flow_error = f"{type(exc).__name__}: {exc}"
        log.exception("Supervisor flow failed")
    finally:
        ags.get_executor = original_get_executor

    # 7. Post-flow verification
    validator_sha_after = hashlib.sha256(validator_path.read_bytes()).hexdigest()
    validator_unmodified = (validator_sha_before == validator_sha_after)

    independent_rc, independent_output = run_frozen_validator_cmd(validator_path, Path(worktree_path))
    independent_validation_passed = (independent_rc == 0)

    executions = ags.list_executions(settings, task_id=task_id)
    roles_observed: dict[str, int] = {}
    for ex_row in executions:
        r = ex_row.get("role", "unknown")
        roles_observed[r] = roles_observed.get(r, 0) + 1

    # Query all acceptance records for task
    conn = ags._connect_ro(settings)
    acceptance_rows: list[dict[str, Any]] = []
    try:
        acceptance_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM supervisor_acceptance WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        ]
    finally:
        conn.close()

    eff_acc = ags.effective_acceptance(settings, task_id)
    effective_acceptance_passed = bool(eff_acc and eff_acc.get("passed") is True)
    task_state = final_task.get("state", "unknown")

    # Fault injection and repair verification:
    # We expect at least one initial rejection followed by a passing acceptance record.
    first_acceptance_rejected = bool(acceptance_rows and not acceptance_rows[0].get("passed"))
    later_acceptance_passed = any(r.get("passed") for r in acceptance_rows[1:]) if len(acceptance_rows) > 1 else False
    repair_rounds_observed = int(final_task.get("repair_round", 0))

    target_completed_before_mutation = bool(
        task_state == "completed"
        and effective_acceptance_passed
        and independent_validation_passed
        and validator_unmodified
        and not flow_error
        and first_acceptance_rejected
        and later_acceptance_passed
    )

    # 8. Optional post-completion invalidation test phase
    invalidation_report: dict[str, Any] = {
        "requested": bool(args.test_invalidation),
        "tested": False,
    }
    invalidation_passed = True
    if args.test_invalidation:
        if not target_completed_before_mutation:
            invalidation_report["tested"] = False
            invalidation_report["reason"] = (
                "Skipped invalidation phase: Phase 1 target acceptance failed."
            )
            invalidation_passed = False
        else:
            sentinel_path = Path(worktree_path) / ".invalidation_sentinel_test"
            sentinel_path.write_text("invalidation_sentinel_mutation\n", encoding="utf-8")
            eff_after_mutation = ags.effective_acceptance(settings, task_id)
            invalidation_succeeded = (eff_after_mutation is None)
            invalidation_passed = invalidation_succeeded
            invalidation_report = {
                "requested": True,
                "tested": True,
                "phase_1_observed_effective_before_mutation": True,
                "mutation_applied": str(sentinel_path),
                "effective_acceptance_after_mutation": eff_after_mutation,
                "invalidated_as_expected": invalidation_succeeded,
                "final_target_current_effective_acceptance": None,
            }

    if args.test_invalidation:
        overall_passed = bool(target_completed_before_mutation and invalidation_passed)
    else:
        overall_passed = bool(target_completed_before_mutation)

    report = {
        "status": "PASS" if overall_passed else "FAIL",
        "task_id": task_id,
        "task_state": task_state,
        "phase_1_target_acceptance": {
            "passed": target_completed_before_mutation,
            "task_state": task_state,
            "effective_acceptance_passed_before_mutation": effective_acceptance_passed,
            "independent_validation_passed": independent_validation_passed,
            "validator_sha256_unmodified": validator_unmodified,
            "first_acceptance_rejected": first_acceptance_rejected,
            "later_acceptance_passed": later_acceptance_passed,
            "repair_rounds_observed": repair_rounds_observed,
        },
        "phase_2_invalidation_test": invalidation_report,
        "effective_acceptance_passed": effective_acceptance_passed,
        "final_target_current_effective_acceptance": (
            None if (args.test_invalidation and invalidation_report.get("tested")) else eff_acc
        ),
        "independent_validation_passed": independent_validation_passed,
        "validator_sha256_unmodified": validator_unmodified,
        "validator_sha256": validator_sha_after,
        "repair_rounds_observed": repair_rounds_observed,
        "first_acceptance_rejected": first_acceptance_rejected,
        "later_acceptance_passed": later_acceptance_passed,
        "roles_observed": roles_observed,
        "controlled_fault_injection": {
            "first_round_incomplete_injection_applied": True,
            "first_round_validation_failed_as_expected": first_acceptance_rejected,
            "repair_round_unmodified_prompt": True,
            "repaired_and_accepted": later_acceptance_passed,
            "explanation": (
                "Round 1 implementer prompt was appended with explicit controlled fault injection "
                "to commit an incomplete clamp returning value unchanged. Supervisor detected validation "
                "failure and rejected acceptance. Round 2 implementer repaired the function under "
                "unmodified prompt, and reviewer + validator verified acceptance."
            ),
        },
        "flow_error": flow_error,
        "independent_validation_output": independent_output[:1000],
        "test_invalidation": invalidation_report,
        "executions_count": len(executions),
        "executions": [
            {
                "execution_id": e.get("execution_id"),
                "role": e.get("role"),
                "executor": e.get("executor"),
                "model": e.get("model"),
                "status": e.get("status"),
                "started_at": e.get("started_at"),
                "finished_at": e.get("finished_at"),
                "evidence_path": e.get("evidence_path"),
            }
            for e in executions
        ],
        "root": str(root),
        "worktree": str(worktree_path),
    }
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Opt-in real AGY model integration harness for conveyor agent supervisor."
    )
    parser.add_argument(
        "--run-real-models",
        action="store_true",
        help="Explicit opt-in required to execute actual AGY models (default: dry-run preflight only).",
    )
    parser.add_argument(
        "--analyst-model",
        default=os.getenv("CONVEYOR_AGY_ANALYST_MODEL", "gemini-3.8-flash-medium"),
        help="Model identifier for analyst role (default: gemini-3.8-flash-medium).",
    )
    parser.add_argument(
        "--implementer-model",
        default=os.getenv("CONVEYOR_AGY_IMPLEMENTER_MODEL", "gemini-3.8-flash-medium"),
        help="Model identifier for implementer role (default: gemini-3.8-flash-medium).",
    )
    parser.add_argument(
        "--reviewer-model",
        default=os.getenv("CONVEYOR_AGY_REVIEWER_MODEL", "gemini-3.8-flash-medium"),
        help="Model identifier for reviewer role (default: gemini-3.8-flash-medium).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Overall task execution timeout in seconds (default: 1800s).",
    )
    parser.add_argument(
        "--repair-rounds",
        type=int,
        default=3,
        help="Maximum allowed repair rounds (default: 3).",
    )
    parser.add_argument(
        "--root",
        type=str,
        default="",
        help="Root directory for isolated repo, worktree, DB, and evidence (default: creates new mkdtemp).",
    )
    parser.add_argument(
        "--agy-bin",
        type=str,
        default="",
        help="Path to agy binary (default: searches CONVEYOR_ANTIGRAVITY_BIN, PATH, or ~/.local/bin/agy).",
    )
    parser.add_argument(
        "--report-path",
        type=str,
        default="",
        help="Optional destination path for the JSON report (default: <root>/agent-supervisor-live-report.json).",
    )
    parser.add_argument(
        "--test-invalidation",
        action="store_true",
        help="Run optional post-completion worktree mutation test to verify effective_acceptance invalidation.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform preflight checks and fixture setup without running models.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        agy_bin = resolve_agy_binary(args.agy_bin)
    except FileNotFoundError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, indent=2))
        return 1

    try:
        root = prepare_root_directory(args.root)
        report_path = resolve_report_path(root, args.report_path)
    except RuntimeError as exc:
        err_report = {"status": "ROOT_ERROR", "error": redact_text(str(exc))}
        print(json.dumps(err_report, indent=2))
        return 1

    if not args.run_real_models or args.dry_run:
        try:
            report = run_preflight_checks(root, agy_bin, args)
            clean_report = redact_obj(report)
            report_path.write_text(json.dumps(clean_report, indent=2), encoding="utf-8")
            print(json.dumps(clean_report, indent=2))
            return 0
        except Exception as exc:
            err_report = {"status": "PREFLIGHT_FAIL", "error": redact_text(f"{type(exc).__name__}: {exc}")}
            print(json.dumps(err_report, indent=2))
            return 1

    # Live flow with real models
    try:
        report = asyncio.run(run_live_flow(root, agy_bin, args))
        clean_report = redact_obj(report)
        report_path.write_text(json.dumps(clean_report, indent=2), encoding="utf-8")
        print(json.dumps(clean_report, indent=2))
        return 0 if clean_report.get("status") == "PASS" else 1
    except KeyboardInterrupt:
        print("\n[ABORT] Received KeyboardInterrupt during supervisor flow execution.", file=sys.stderr)
        err_report = {"status": "INTERRUPTED", "error": "KeyboardInterrupt received by operator"}
        clean_report = redact_obj(err_report)
        report_path.write_text(json.dumps(clean_report, indent=2), encoding="utf-8")
        print(json.dumps(clean_report, indent=2))
        return 130
    except Exception as exc:
        log.exception("Unexpected error during live flow execution")
        err_report = {"status": "ERROR", "error": redact_text(f"{type(exc).__name__}: {exc}")}
        clean_report = redact_obj(err_report)
        report_path.write_text(json.dumps(clean_report, indent=2), encoding="utf-8")
        print(json.dumps(clean_report, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
