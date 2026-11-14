#!/usr/bin/env python3
"""End-to-end smoke for the Supervisor "pool" executor with FAKE CLIs.

Drives the real supervisor flow (queue_task -> claim_task -> run_supervisor_flow)
where every analyst/implementer/reviewer role uses executor "pool". The pool
delegates through the real ClaudeExecutor / AntigravityExecutor / PiExecutor and
the real ``_spawn_cli`` subprocess path. Only the binaries are fakes, written
into a temp dir and pointed at via CONVEYOR_CLAUDE_BIN / CONVEYOR_AGY_BIN /
CONVEYOR_PI_BIN:

- ``claude`` always hits a usage limit (stream-json error envelope on stdout,
  message on stderr, exit 1).
- ``agy`` emits valid ``--output-format stream-json`` result envelopes and
  behaves per role (analyst: plan, implementer: writes feature.txt, reviewer:
  returns a verdict for the Current SHA / Target fingerprint in its prompt).
- ``pi`` emits valid ``-p --mode json`` NDJSON events and verifies read-only vs
  write tool allowlists when the pool rotates to ``pi``.

Worker detection is stubbed in-process so no real ``claude``/``agy``/``cmd``/``pi``
binary is ever resolved from PATH. Nothing is committed or pushed.

Prints PASS/FAIL lines; exits non-zero on any failure.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Environment must be settled before the supervisor modules read it.
os.environ["CONVEYOR_AGENT_SUPERVISOR_ENABLED"] = "true"
# Coordinator uses the chat tier; "off" keeps it unavailable (no network).
os.environ["CONVEYOR_CHAT_MODE"] = "off"

from conveyor.agents import workers  # noqa: E402
from conveyor.agents import agent_supervisor as ags  # noqa: E402
from conveyor.core.config import Settings  # noqa: E402

POOL_ROLES = ("analyst", "implementer", "reviewer")
VALIDATION_NAME = "feature-present"
FEATURE_LINE = "pool smoke feature\n"

_FAKE_CLAUDE = r'''#!__PY__
import json, os, sys
LOG = __LOG__
prompt = sys.stdin.read()
role = "other"
if "INDEPENDENT code reviewer" in prompt:
    role = "reviewer"
elif "You are the implementer" in prompt:
    role = "implementer"
elif "You are the analyst" in prompt:
    role = "analyst"
with open(LOG, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"bin": "claude", "argv": sys.argv[1:], "cwd": os.getcwd(), "role": role}) + "\n")
MSG = "You've hit your usage limit. Resets in 2h0m0s"
sys.stdout.write(json.dumps({"type": "result", "subtype": "error_during_execution",
                             "is_error": True, "result": MSG}) + "\n")
sys.stdout.flush()
sys.stderr.write(MSG + "\n")
sys.stderr.flush()
sys.exit(1)
'''

_FAKE_AGY = r'''#!__PY__
import json, os, re, sys
LOG = __LOG__
argv = sys.argv[1:]
prompt = argv[argv.index("--print") + 1] if "--print" in argv else ""
role = "other"
if "INDEPENDENT code reviewer" in prompt:
    role = "reviewer"
elif "You are the implementer" in prompt:
    role = "implementer"
elif "You are the analyst" in prompt:
    role = "analyst"
with open(LOG, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"bin": "agy", "argv": argv, "cwd": os.getcwd(), "role": role}) + "\n")

def emit(result):
    print(json.dumps({"event": "progress", "text": "working"}))
    print(json.dumps({"event": "result", "result": result}))
    sys.stdout.flush()

base = {"status": "SUCCESS", "finished_reason": "STOP", "conversation_id": "fake-agy"}
if role == "implementer":
    with open("feature.txt", "w", encoding="utf-8") as fh:
        fh.write(__FEATURE__)
    emit(dict(base, response="Changed feature.txt: added the smoke feature line."))
elif role == "reviewer":
    cur = re.search(r"^Current SHA: (\S+)$", prompt, re.M).group(1)
    fp = re.search(r"^Target fingerprint: (\S+)$", prompt, re.M).group(1)
    verdict = {
        "passed": True,
        "target_sha": cur,
        "target_fingerprint": fp,
        "issues": [],
        "checks": [{"name": "feature-file-present", "command": "inspect feature.txt",
                    "passed": True, "output": "feature.txt present"}],
        "summary": "feature.txt carries the smoke feature line.",
    }
    emit(dict(base, structured_output=verdict, response=json.dumps(verdict)))
elif role == "analyst":
    emit(dict(base, response="Plan: create feature.txt containing the smoke feature line."))
else:
    emit(dict(base, response="ok"))
'''

_FAKE_PI = r'''#!__PY__
import json, os, sys
LOG = __LOG__
prompt = sys.stdin.read()
argv = sys.argv[1:]
tools = argv[argv.index("--tools") + 1] if "--tools" in argv else ""
role = "implementer" if "write" in tools.split(",") else "analyst"
with open(LOG, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"bin": "pi", "argv": argv, "cwd": os.getcwd(), "role": role}) + "\n")
text = f"pi {role} completed"
print(json.dumps({"type": "message_end", "message": {"role": "assistant",
    "usage": {"input": 8, "output": 4, "cacheRead": 0},
    "content": [{"type": "text", "text": text}]}}), flush=True)
print(json.dumps({"type": "agent_end", "messages": [
    {"role": "assistant", "content": [{"type": "text", "text": text}]}]}), flush=True)
'''


def _write_fake(path: Path, template: str, log: Path) -> None:
    body = (
        template.replace("__PY__", sys.executable)
        .replace("__LOG__", repr(str(log)))
        .replace("__FEATURE__", repr(FEATURE_LINE))
    )
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def run_git(cwd: Path | str, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return res.stdout.strip()


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            print(f"PASS: {name}")
        else:
            print(f"FAIL: {name}" + (f" -- {detail}" if detail else ""))
            self.failures.append(name)


def _opt(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="supervisor-pool-smoke-"))
    bins = tmp / "bin"
    bins.mkdir(parents=True)
    invocations = tmp / "invocations.jsonl"
    invocations.touch()
    claude_bin = bins / "claude"
    agy_bin = bins / "agy"
    pi_bin = bins / "pi"
    _write_fake(claude_bin, _FAKE_CLAUDE, invocations)
    _write_fake(agy_bin, _FAKE_AGY, invocations)
    _write_fake(pi_bin, _FAKE_PI, invocations)

    # Point the real executors at the fakes. ANTIGRAVITY_BIN wins over AGY_BIN,
    # so both are set to avoid a stray user override.
    os.environ["CONVEYOR_CLAUDE_BIN"] = str(claude_bin)
    os.environ["CONVEYOR_AGY_BIN"] = str(agy_bin)
    os.environ["CONVEYOR_ANTIGRAVITY_BIN"] = str(agy_bin)
    os.environ["CONVEYOR_PI_BIN"] = str(pi_bin)
    os.environ["CONVEYOR_PI_PROVIDER"] = "deepseek"
    os.environ["CONVEYOR_PI_MODEL"] = "deepseek-chat"
    from runner.pi_agent import pi_api_key_env
    os.environ.setdefault(pi_api_key_env("deepseek"), "smoke-fake-key")

    # Detection would otherwise resolve real binaries from PATH (and run them).
    original_detect = workers.detect_all_workers
    installed = {"claude-code": str(claude_bin), "agy": str(agy_bin), "pi": str(pi_bin)}

    def fake_detect(force: bool = False) -> dict:
        out = {}
        for item in workers.CATALOG:
            path = installed.get(item["id"])
            out[item["id"]] = {
                "installed": bool(path),
                "version": "fake-1.0" if path else None,
                "path": path,
                "auth": "ok" if path else "unknown",
            }
        return out

    workers.detect_all_workers = fake_detect
    checks = Checks()
    try:
        # 1. Disposable repo
        repo = tmp / "repo"
        repo.mkdir()
        run_git(repo, "init")
        run_git(repo, "config", "user.name", "Conveyor Smoke")
        run_git(repo, "config", "user.email", "smoke@conveyor.local")
        (repo / "README.md").write_text("# Pool smoke repo\n", encoding="utf-8")
        run_git(repo, "add", "README.md")
        run_git(repo, "commit", "-m", "initial commit")
        base_sha = run_git(repo, "rev-parse", "HEAD")

        # 2. Settings with an operator validation command that proves the change landed.
        settings = Settings(
            telegram_bot_token="",
            telegram_allowed_user_id=0,
            codex_workspace_root=tmp / "workspace",
            codex_bin="codex",
            codex_task_root=tmp / "tasks",
            codex_model=None,
            codex_timeout_seconds=60,
            telegram_progress_seconds=3,
            codex_retry_429_delays_seconds=(),
            codex_memory_root=tmp / "memory",
            user_timezone="UTC",
            agent_supervisor_enabled=True,
            agent_supervisor_max_repair_rounds=1,
            agent_supervisor_validation_commands=(
                (VALIDATION_NAME, (
                    sys.executable, "-c",
                    "import pathlib, sys; sys.exit(0 if pathlib.Path('feature.txt').is_file() else 1)",
                )),
            ),
            agent_supervisor_validation_timeout_seconds=60,
        )
        (settings.codex_memory_root / "state").mkdir(parents=True, exist_ok=True)

        # Pool order: claude-code first, then agy, then pi.
        state = workers.load_workers_state(settings)
        state["order"] = ["claude-code", "agy", "pi"] + [
            w for w in state["order"] if w not in ("claude-code", "agy", "pi")
        ]
        workers.save_workers_state(settings, state)

        # 3. Real lifecycle: create -> worktree -> queue (roles -> pool) -> claim -> flow.
        goal = "Add feature.txt with the smoke feature line"
        task = ags.create_task(settings, goal=goal, repo_path=str(repo), base_sha=base_sha)
        tid = task["id"]
        wt = ags.create_worktree(settings, str(repo), tid)
        ags.queue_task(
            settings,
            tid,
            worktree_path=wt,
            acceptance_criteria=f"feature.txt exists and validation '{VALIDATION_NAME}' passes",
            goal=goal,
            roles={role: "pool" for role in POOL_ROLES},
        )
        claimed = ags.claim_task(settings)
        checks.check("task claimed into dispatching", bool(claimed) and claimed["id"] == tid
                     and claimed["state"] == "dispatching",
                     f"claimed={claimed and claimed.get('state')}")

        final = asyncio.run(ags.run_supervisor_flow(
            settings, tid, goal=goal, worktree_path=wt,
            acceptance_criteria=f"feature.txt exists and validation '{VALIDATION_NAME}' passes",
        ))
        state_now = (ags.get_task(settings, tid) or {}).get("state")
        checks.check("task reaches completed (accepted)", final.get("state") == "completed" and state_now == "completed",
                     f"state={final.get('state')} error={final.get('error')}")
        acc = ags.effective_acceptance(settings, tid)
        checks.check("effective acceptance passed on current worktree",
                     bool(acc) and acc.get("passed") is True, f"acceptance={acc}")

        # 4. Execution rows: claude attempt failed, then antigravity, for the analyst role.
        rows = sorted(ags.list_executions(settings, task_id=tid, limit=500),
                      key=lambda r: r.get("started_at") or "")
        analyst_rows = [r for r in rows if r.get("role") == "analyst"]
        seq = [(r.get("executor"), r.get("status")) for r in analyst_rows]
        checks.check(
            "analyst execution rows: claude failed then antigravity finished",
            seq[:2] == [("claude", "failed"), ("antigravity", "finished")],
            f"analyst rows={seq}",
        )
        other_claude = [r for r in rows if r.get("executor") == "claude" and r.get("role") != "analyst"]
        checks.check("claude-code not retried for implementer/reviewer (cooling)", not other_claude,
                     f"claude rows={[(r.get('role'), r.get('status')) for r in other_claude]}")
        impl_agy = [r for r in rows if r.get("role") == "implementer" and r.get("executor") == "antigravity"]
        rev_agy = [r for r in rows if r.get("role") == "reviewer" and r.get("executor") == "antigravity"]
        checks.check("implementer and reviewer ran on antigravity",
                     len(impl_agy) >= 1 and len(rev_agy) >= 1,
                     f"implementer={len(impl_agy)} reviewer={len(rev_agy)}")

        # 5. Workers state: claude-code cooling, quota_error switch logged.
        state_after = workers.load_workers_state(settings)
        cool = state_after.get("cooling", {}).get("claude-code")
        checks.check("workers state marks claude-code cooling",
                     bool(cool) and cool.get("reset_at", 0) > 0, f"cooling={state_after.get('cooling')}")
        payload = workers.get_workers_payload(settings)
        cc = next((w for w in payload["workers"] if w["id"] == "claude-code"), {})
        checks.check("workers payload shows claude-code quota cooling",
                     cc.get("quota", {}).get("state") == "cooling", f"quota={cc.get('quota')}")
        switches = [e for e in state_after.get("switch_log", []) if e.get("reason") == "quota_error"]
        checks.check("switch_log has quota_error claude-code -> agy",
                     any(e.get("from") == "claude-code" and e.get("to") == "agy" for e in switches),
                     f"switch_log={state_after.get('switch_log')}")

        # 6. Implementer change is in the worktree (fake agy ran in the worktree cwd).
        feature = Path(wt) / "feature.txt"
        checks.check("implementer file change present in worktree",
                     feature.is_file() and feature.read_text(encoding="utf-8") == FEATURE_LINE,
                     f"exists={feature.exists()}")
        status = run_git(wt, "status", "--porcelain")
        checks.check("implementer change visible in worktree diff", "feature.txt" in status,
                     f"git status={status!r}")

        # 7. Argv evidence captured by the fakes.
        calls = [json.loads(line) for line in invocations.read_text(encoding="utf-8").splitlines() if line.strip()]
        claude_calls = [c for c in calls if c["bin"] == "claude"]
        agy_calls = [c for c in calls if c["bin"] == "agy"]
        checks.check("claude invoked exactly once (analyst, then cooled)",
                     len(claude_calls) == 1 and claude_calls[0]["role"] == "analyst",
                     f"claude calls={[(c['role']) for c in claude_calls]}")
        agy_roles = sorted({c["role"] for c in agy_calls})
        checks.check("agy served analyst, implementer and reviewer",
                     set(POOL_ROLES) <= set(agy_roles), f"agy roles={agy_roles}")

        for c in claude_calls:
            argv = c["argv"]
            tools = _opt(argv, "--tools") or ""
            checks.check("claude analyst argv is plan mode with read-only tools",
                         _opt(argv, "--permission-mode") == "plan" and tools == "Read,Glob,Grep",
                         f"argv={argv}")
            checks.check("claude analyst argv has no write mode or bypass",
                         "acceptEdits" not in argv and "--dangerously-skip-permissions" not in argv
                         and not any(t in tools.split(",") for t in ("Write", "Edit", "Bash")),
                         f"argv={argv}")

        for c in agy_calls:
            argv = c["argv"]
            mode = _opt(argv, "--mode")
            bypass = "--dangerously-skip-permissions" in argv
            if c["role"] in ("analyst", "reviewer"):
                settings_json = _opt(argv, "--settings") or ""
                deny = (json.loads(settings_json).get("permissions", {}).get("deny", [])
                        if settings_json else [])
                checks.check(f"agy {c['role']} argv is plan mode with read-only settings",
                             mode == "plan" and "write_file(*)" in deny and "command(*)" in deny,
                             f"mode={mode} settings={settings_json[:120]}")
                checks.check(f"agy {c['role']} argv has no write mode or bypass",
                             mode != "accept-edits" and not bypass, f"argv_mode={mode} bypass={bypass}")
            elif c["role"] == "implementer":
                checks.check("agy implementer argv uses accept-edits, no bypass",
                             mode == "accept-edits" and not bypass, f"mode={mode} bypass={bypass}")
                checks.check("agy implementer ran inside the worktree",
                             os.path.realpath(c["cwd"]) == os.path.realpath(wt),
                             f"cwd={c['cwd']} wt={wt}")

        # 8. Cool agy and verify PoolExecutor delegates read and write roles to PiExecutor.
        workers.mark_cooling(settings, "agy", 3600, reason="smoke")
        state_pi = workers.load_workers_state(settings)
        state_pi.setdefault("models", {})["pi"] = "pool-model-pi-cannot-use"
        workers.save_workers_state(settings, state_pi)
        pool_ex = ags.get_executor(settings, "pool")
        pi_read = asyncio.run(pool_ex.run(
            role=ags.RoleConfig(
                name="analyst", executor="pool", tools=["read"], location="worktree", model="role-model",
            ),
            prompt="You are the analyst",
            worktree_path=wt,
            timeout_seconds=30,
        ))
        pi_write = asyncio.run(pool_ex.run(
            role=ags.RoleConfig(
                name="implementer", executor="pool", tools=["read", "write"], location="worktree", model="role-model",
            ),
            prompt="You are the implementer",
            worktree_path=wt,
            timeout_seconds=30,
        ))
        checks.check("pool delegated read role to pi when claude-code and agy are cooling",
                     pi_read.success and pi_read.evidence.get("pool", {}).get("worker_id") == "pi",
                     f"pi_read={pi_read}")
        checks.check("pool delegated write role to pi when claude-code and agy are cooling",
                     pi_write.success and pi_write.evidence.get("pool", {}).get("worker_id") == "pi",
                     f"pi_write={pi_write}")
        all_calls = [json.loads(line) for line in invocations.read_text(encoding="utf-8").splitlines() if line.strip()]
        pi_calls = [c for c in all_calls if c["bin"] == "pi"]
        checks.check("pi invoked for both analyst (read) and implementer (write)",
                     [c["role"] for c in pi_calls] == ["analyst", "implementer"],
                     f"pi_calls={pi_calls}")
        if len(pi_calls) == 2:
            read_argv, write_argv = pi_calls[0]["argv"], pi_calls[1]["argv"]
            checks.check("pi read role uses read,grep,find,ls tools only",
                         _opt(read_argv, "--tools") == "read,grep,find,ls",
                         f"read_argv={read_argv}")
            checks.check("pi write role uses read,bash,edit,write,grep,find,ls tools",
                         _opt(write_argv, "--tools") == "read,bash,edit,write,grep,find,ls",
                         f"write_argv={write_argv}")
            checks.check("pool worker model is never passed to pi",
                         "pool-model-pi-cannot-use" not in read_argv and "pool-model-pi-cannot-use" not in write_argv,
                         f"read_argv={read_argv} write_argv={write_argv}")
    except Exception as exc:  # surface any flow crash as a failed check
        checks.check("smoke flow completed without exception", False, f"{type(exc).__name__}: {exc}")
    finally:
        workers.detect_all_workers = original_detect

    if checks.failures:
        print(f"supervisor_pool_smoke: FAIL ({len(checks.failures)} failed); kept {tmp}")
        return 1
    print("supervisor_pool_smoke: PASS (pool -> real _spawn_cli path with fake claude/agy)")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
