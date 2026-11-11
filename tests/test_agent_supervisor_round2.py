"""Round 2 OS-process, lifecycle, lock, recovery, AGY policy, and provenance tests.

Uses disposable git repositories/worktrees, temporary SQLite databases, and
real executable subprocess helpers (honestly fake provider CLIs, NOT live LLM
network calls).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("CONVEYOR_AGENT_SUPERVISOR_ENABLED", "true")

from conveyor.agents import agent_supervisor as ags
from conveyor.core.config import Settings
from conveyor.agents.agent_supervisor import (
    AGY_READONLY_DENY_RULES,
    AntigravityExecutor,
    CommandCodeExecutor,
    ExecutionContext,
    RoleConfig,
    SupervisorLockError,
    SupervisorStateError,
    acquire_worktree_lock,
    build_agy_readonly_settings,
    build_reviewer_prompt,
    cancel_task,
    claim_task,
    create_task,
    dispatch_task,
    effective_acceptance,
    execution_context_scope,
    get_task,
    latest_acceptance,
    list_executions,
    list_tasks,
    load_roles,
    lock_release_safe,
    queue_task,
    recover_on_startup,
    release_worktree_lock,
    run_supervisor_flow,
    target_identity,
    transition,
)


def make_settings(
    tmp: Path,
    *,
    validation_commands: tuple[tuple[str, tuple[str, ...]], ...] = (),
    codex_model: str | None = None,
    antigravity_bin: str = "agy",
    timeout_seconds: int = 30,
) -> Settings:
    return Settings(
        telegram_bot_token="",
        telegram_allowed_user_id=0,
        codex_workspace_root=tmp,
        codex_bin="codex",
        codex_task_root=tmp,
        codex_model=codex_model,
        codex_timeout_seconds=60,
        telegram_progress_seconds=3,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=tmp / "memory",
        user_timezone="UTC",
        agent_supervisor_enabled=True,
        agent_supervisor_antigravity_bin=antigravity_bin,
        agent_supervisor_task_timeout_seconds=timeout_seconds,
        agent_supervisor_validation_commands=validation_commands,
        agent_supervisor_validation_timeout_seconds=15,
    )


def git(*argv: str, cwd: Path) -> str:
    r = subprocess.run(["git", *argv], cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {argv} failed: {r.stderr[:300]}")
    return (r.stdout or "").strip()


def make_repo_and_worktree(tmp: Path) -> tuple[Path, Path, str]:
    repo = tmp / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    git("init", "-q", cwd=repo)
    git("config", "user.email", "round2@test.local", cwd=repo)
    git("config", "user.name", "Round2", cwd=repo)
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git("add", "app.py", cwd=repo)
    git("commit", "-qm", "initial", cwd=repo)
    head = git("rev-parse", "HEAD", cwd=repo)
    wt = tmp / "wt"
    git("worktree", "add", "--detach", str(wt), head, cwd=repo)
    return repo, wt, head


def write_executable(path: Path, body: str) -> Path:
    script = f"#!{sys.executable}\n" + textwrap.dedent(body).lstrip("\n")
    path.write_text(script, encoding="utf-8")
    os.chmod(path, 0o755)
    return path


class Round2AntigravityPolicyAndFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sup-r2-agy-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo, self.wt, self.head = make_repo_and_worktree(self.tmp)

    def test_antigravity_flags_readonly_deny_settings_and_no_codex_model_leak(self) -> None:
        # Even when codex_model is populated in Settings, non-codex roles must NOT inherit it.
        settings = make_settings(self.tmp, codex_model="gpt-5.4-codex-only")
        s_agy = dataclasses.replace(settings, agent_supervisor_implementation_executor="antigravity")
        roles = load_roles(s_agy)
        self.assertEqual(roles["analyst"].executor, "antigravity")
        self.assertEqual(roles["implementer"].executor, "antigravity")
        self.assertEqual(roles["reviewer"].executor, "antigravity")
        self.assertIsNone(roles["analyst"].model)
        self.assertIsNone(roles["implementer"].model)
        self.assertIsNone(roles["reviewer"].model)

        fake_agy = write_executable(self.tmp / "fake-agy", "import sys; sys.exit(0)\n")
        s_agy = dataclasses.replace(s_agy, agent_supervisor_antigravity_bin=str(fake_agy))
        ex = AntigravityExecutor(s_agy)
        ok, reason = ex.available()
        self.assertTrue(ok, reason)

        # Empty / whitespace prompt for --print fails closed without spawning.
        empty_res = asyncio.run(
            ex.run(
                role=roles["analyst"],
                prompt="   ",
                worktree_path=str(self.wt),
                timeout_seconds=10,
            )
        )
        self.assertFalse(empty_res.success)
        self.assertIn("--print requires a non-empty prompt", empty_res.error)

        # Readonly reviewer argv must carry --mode plan, --print-timeout <N>s, --new-project,
        # --json-schema, and explicit per-session --settings deny rules, NEVER bypass.
        rev_role = RoleConfig(
            "reviewer",
            executor="antigravity",
            model="gemini-3.8-flash-high",
            tools=["read"],
            location="worktree",
        )
        rev_argv = ex._argv(rev_role, str(self.wt), prompt="audit candidate", timeout_seconds=90)
        self.assertNotIn("--dangerously-skip-permissions", rev_argv)
        self.assertIn("--new-project", rev_argv)
        self.assertEqual(rev_argv[rev_argv.index("--mode") + 1], "plan")
        self.assertEqual(rev_argv[rev_argv.index("--output-format") + 1], "stream-json")
        self.assertEqual(rev_argv[rev_argv.index("--print-timeout") + 1], "90s")
        self.assertEqual(rev_argv[rev_argv.index("--model") + 1], "gemini-3.8-flash-high")
        self.assertIn("--json-schema", rev_argv)
        self.assertIn("--settings", rev_argv)

        settings_payload = json.loads(rev_argv[rev_argv.index("--settings") + 1])
        self.assertIs(settings_payload["allowNonWorkspaceAccess"], False)
        self.assertEqual(settings_payload["permissions"]["ask"], [])
        for deny_rule in AGY_READONLY_DENY_RULES:
            self.assertIn(deny_rule, settings_payload["permissions"]["deny"])
        canon_wt = str(self.wt.resolve())
        self.assertEqual(settings_payload["permissions"]["allow"], [f"read_file({canon_wt})"])

        # Implementer argv uses accept-edits without readonly deny or bypass.
        impl_argv = ex._argv(roles["implementer"], str(self.wt), prompt="implement fix", timeout_seconds=45)
        self.assertEqual(impl_argv[impl_argv.index("--mode") + 1], "accept-edits")
        self.assertEqual(impl_argv[impl_argv.index("--print-timeout") + 1], "45s")
        self.assertNotIn("--settings", impl_argv)
        self.assertNotIn("--dangerously-skip-permissions", impl_argv)

        # Role policy rejects write capability on reviewer/analyst and rejects browser/metadata command overrides.
        bad_role = RoleConfig("reviewer", executor="antigravity", tools=["read", "write"], location="worktree")
        with self.assertRaises(SupervisorStateError):
            ex._argv(bad_role, str(self.wt), prompt="x", timeout_seconds=10)

        with self.assertRaises(SupervisorStateError):
            create_task(s_agy, goal="g", repo_path=str(self.repo), metadata={"validation_commands": [["rm", ["-rf"]]]})

        t = create_task(s_agy, goal="g", repo_path=str(self.repo), base_sha=self.head)
        with self.assertRaises(SupervisorStateError):
            queue_task(
                s_agy,
                t["id"],
                worktree_path=str(self.wt),
                roles={"reviewer": {"executor": "antigravity", "tools": ["read", "write"]}},
            )

    def test_parse_agy_stream_prefers_structured_output_and_surfaces_errors(self) -> None:
        # 1. Real probe logs if present on host.
        evidence_dir = Path("/tmp/conveyor-supervisor-evidence")
        schema_probe = evidence_dir / "agy-schema-probe.jsonl"
        if schema_probe.is_file():
            out, meta = ags._parse_agy_stream(schema_probe.read_text(encoding="utf-8"))
            self.assertFalse(meta["is_error"])
            self.assertTrue(meta["structured_output_used"])
            self.assertEqual(json.loads(out), {"ready": True})

        err_probe = evidence_dir / "implementation-antigravity.jsonl"
        if err_probe.is_file():
            out, meta = ags._parse_agy_stream(err_probe.read_text(encoding="utf-8", errors="replace"))
            self.assertEqual(out, "")
            self.assertTrue(meta["is_error"])
            self.assertIn("interrupted", meta["error"])

        # 2. Deterministic inline multi-turn structured_output vs repeated display response.
        stream = "\n".join([
            json.dumps({"event": "init", "conversation_id": "c-1", "init": {"model": "gemini-3.8-flash"}}),
            json.dumps({
                "event": "result",
                "result": {
                    "conversation_id": "c-1",
                    "status": "SUCCESS",
                    "response": "{\"passed\": true}\n{\"passed\": true}\n",
                    "structured_output": {"passed": True, "note": "clean"},
                },
            }),
        ])
        out, meta = ags._parse_agy_stream(stream)
        self.assertFalse(meta["is_error"])
        self.assertTrue(meta["structured_output_used"])
        self.assertEqual(json.loads(out), {"passed": True, "note": "clean"})

        # 3. Quota / auth ERROR envelopes surface clearly.
        quota_stream = json.dumps({
            "event": "result",
            "result": {
                "conversation_id": "c-2",
                "status": "ERROR",
                "response": "",
                "error": "Quota exhausted for gemini-3.8-pro until 08:00Z",
                "finished_reason": "quota_exceeded",
            },
        })
        out_q, meta_q = ags._parse_agy_stream(quota_stream)
        self.assertEqual(out_q, "")
        self.assertTrue(meta_q["is_error"])
        self.assertIn("Quota exhausted", meta_q["error"])

    def test_complete_flow_via_real_executable_helper_binds_task_locks_diff_and_provenance(self) -> None:
        state_file = self.tmp / "helper_state.json"
        db_path = ags._db_path(make_settings(self.tmp))
        evidence_root = ags._evidence_root(make_settings(self.tmp))

        fake_agy = write_executable(
            self.tmp / "fake-agy-flow",
            f"""
            import hashlib, json, os, sqlite3, subprocess, sys
            from pathlib import Path

            argv = sys.argv[1:]
            prompt = argv[argv.index("--print") + 1]
            mode = argv[argv.index("--mode") + 1]
            has_schema = "--json-schema" in argv
            has_settings = "--settings" in argv
            cwd = os.getcwd()
            my_pid = os.getpid()
            my_pgid = os.getpgrp()

            # Verify DB registration + lock writer_pgid + evidence dir ALREADY exist BEFORE we act!
            conn = sqlite3.connect({str(db_path)!r})
            conn.row_factory = sqlite3.Row
            ex_row = conn.execute(
                "SELECT * FROM supervisor_executions WHERE pid = ? AND status = 'running'",
                (my_pid,),
            ).fetchone()
            assert ex_row is not None, "execution row missing when child started"
            tid = ex_row["task_id"]
            assert tid and tid.startswith("sup-"), f"empty or invalid task_id on execution: {{tid!r}}"
            assert ex_row["process_identity"], "missing durable process_identity on execution row"
            ev_dir = Path({str(evidence_root)!r}) / tid
            assert ev_dir.is_dir(), "per-task evidence directory not created before child tools run"

            lock_row = conn.execute(
                "SELECT * FROM supervisor_locks WHERE task_id = ?",
                (tid,),
            ).fetchone()
            assert lock_row is not None, "worktree lock missing during child execution"
            assert int(lock_row["writer_pgid"]) == my_pgid, (
                f"lock writer_pgid {{lock_row['writer_pgid']}} != child pgid {{my_pgid}}"
            )
            assert lock_row["writer_identity"], "lock writer_identity empty during child execution"
            conn.close()

            state_path = Path({str(state_file)!r})
            st = json.loads(state_path.read_text()) if state_path.exists() else {{"calls": []}}

            if mode == "plan" and not has_schema:
                # Analyst (readonly)
                assert has_settings, "analyst missing --settings readonly policy"
                st["calls"].append({{"role": "analyst", "pid": my_pid, "task_id": tid}})
                state_path.write_text(json.dumps(st))
                print(json.dumps({{
                    "event": "result",
                    "result": {{
                        "conversation_id": "conv-analyst",
                        "status": "SUCCESS",
                        "response": "1. Update VALUE = 42 in app.py and commit.",
                    }},
                }}))
                sys.exit(0)

            if mode == "accept-edits":
                # Implementer (write): commit a real change so git diff HEAD becomes empty!
                st["calls"].append({{"role": "implementer", "pid": my_pid, "task_id": tid}})
                state_path.write_text(json.dumps(st))
                Path(cwd, "app.py").write_text("VALUE = 42\\n", encoding="utf-8")
                subprocess.run(["git", "add", "app.py"], cwd=cwd, check=True, capture_output=True)
                subprocess.run(["git", "commit", "-qm", "set VALUE=42"], cwd=cwd, check=True, capture_output=True)
                head_sha = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=cwd, check=True, capture_output=True, text=True
                ).stdout.strip()
                print(json.dumps({{
                    "event": "result",
                    "result": {{
                        "conversation_id": "conv-impl",
                        "status": "SUCCESS",
                        "response": f"Updated app.py to VALUE = 42 in commit {{head_sha}}",
                    }},
                }}))
                sys.exit(0)

            if mode == "plan" and has_schema:
                # Independent Reviewer (readonly): verify committed diff from base_sha is in prompt!
                assert has_settings, "reviewer missing --settings readonly policy"
                assert "+VALUE = 42" in prompt, "committed candidate diff missing from reviewer prompt!"
                assert "Supervisor-executed operator validation evidence:" in prompt
                assert "check-value-42" in prompt
                # Compute target_sha & target_fingerprint from prompt lines
                cur_sha, target_fp = "", ""
                for ln in prompt.splitlines():
                    if ln.startswith("Current SHA: "):
                        cur_sha = ln.split("Current SHA: ", 1)[1].strip()
                    elif ln.startswith("Target fingerprint: "):
                        target_fp = ln.split("Target fingerprint: ", 1)[1].strip()
                st["calls"].append({{
                    "role": "reviewer",
                    "pid": my_pid,
                    "task_id": tid,
                    "saw_committed_diff": "+VALUE = 42" in prompt,
                }})
                state_path.write_text(json.dumps(st))
                verdict = {{
                    "passed": True,
                    "target_sha": cur_sha,
                    "target_fingerprint": target_fp,
                    "issues": [],
                    "checks": [{{
                        "name": "reviewer-audit-of-operator-check",
                        "command": "audited operator check-value-42",
                        "passed": True,
                        "output": "VALUE == 42 confirmed",
                    }}],
                    "summary": "Committed diff and operator validation verified.",
                }}
                # Emit repeated response PLUS clean structured_output just like real agy --json-schema!
                print(json.dumps({{
                    "event": "result",
                    "result": {{
                        "conversation_id": "conv-rev",
                        "status": "SUCCESS",
                        "response": json.dumps(verdict) + "\\n" + json.dumps(verdict) + "\\n",
                        "structured_output": verdict,
                    }},
                }}))
                sys.exit(0)

            sys.exit(2)
            """,
        )

        settings = make_settings(
            self.tmp,
            antigravity_bin=str(fake_agy),
            validation_commands=(
                (
                    "check-value-42",
                    (
                        sys.executable,
                        "-c",
                        "import app; assert app.VALUE == 42, app.VALUE; print('VALIDATED_42_OK')",
                    ),
                ),
            ),
        )
        settings = dataclasses.replace(settings, agent_supervisor_implementation_executor="antigravity")

        secret_goal = "Set VALUE = 42 in app.py (SENSITIVE_GOAL_MARKER_DO_NOT_LOG_IN_ARGV)"
        t = create_task(settings, goal=secret_goal, repo_path=str(self.repo), base_sha=self.head)
        queue_task(settings, t["id"], worktree_path=str(self.wt))
        claimed = claim_task(settings)
        self.assertIsNotNone(claimed)

        final = asyncio.run(run_supervisor_flow(settings, t["id"]))
        self.assertEqual(final["state"], "completed", f"flow failed: {final.get('stage')} / {final.get('error')}")
        self.assertNotEqual(final["current_sha"], self.head)

        helper_data = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual([c["role"] for c in helper_data["calls"]], ["analyst", "implementer", "reviewer"])
        self.assertTrue(all(c["task_id"] == t["id"] for c in helper_data["calls"]))
        self.assertTrue(helper_data["calls"][-1]["saw_committed_diff"])

        # Verify all 4 real OS subprocess executions (analyst, implementer, validator, reviewer)
        exec_rows = list_executions(settings, task_id=t["id"])
        self.assertEqual(len(exec_rows), 4)
        self.assertEqual({r["role"] for r in exec_rows}, {"analyst", "implementer", "validator", "reviewer"})
        for r in exec_rows:
            self.assertEqual(r["task_id"], t["id"])
            self.assertEqual(r["status"], "finished")
            self.assertTrue(r["process_identity"])
            ev_path = Path(r["evidence_path"])
            self.assertTrue(ev_path.is_file())
            self.assertEqual(ev_path.parent.name, t["id"])
            ev_json = json.loads(ev_path.read_text(encoding="utf-8"))
            argv_dump = json.dumps(ev_json["argv"])
            self.assertNotIn("SENSITIVE_GOAL_MARKER_DO_NOT_LOG_IN_ARGV", argv_dump)
            if r["executor"] == "antigravity":
                self.assertIn("<prompt:sha256=", argv_dump)

        acc = effective_acceptance(settings, t["id"])
        self.assertIsNotNone(acc)
        self.assertTrue(acc["passed"])
        self.assertTrue(acc["provenance_verified"])
        self.assertEqual(acc["evidence_source"], "operator_validation+independent_review")
        self.assertEqual(len(acc["executed_checks"]), 1)
        self.assertEqual(acc["executed_checks"][0]["name"], "check-value-42")
        self.assertIn("VALIDATED_42_OK", acc["executed_checks"][0]["output"])
        self.assertEqual(len(acc["self_reported_checks"]), 1)

        # Lock is released cleanly after completion.
        conn = ags._connect_ro(settings)
        try:
            lock = conn.execute("SELECT * FROM supervisor_locks WHERE task_id = ?", (t["id"],)).fetchone()
        finally:
            conn.close()
        self.assertIsNone(lock)

    def test_validation_provenance_blocks_completion_when_unconfigured_or_failing(self) -> None:
        fake_agy = write_executable(
            self.tmp / "fake-agy-prov",
            """
            import json, os, subprocess, sys
            from pathlib import Path

            argv = sys.argv[1:]
            prompt = argv[argv.index("--print") + 1]
            mode = argv[argv.index("--mode") + 1]
            has_schema = "--json-schema" in argv
            cwd = os.getcwd()

            if mode == "plan" and not has_schema:
                print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "plan"}}))
                sys.exit(0)
            if mode == "accept-edits":
                Path(cwd, "app.py").write_text("VALUE = 7\\n", encoding="utf-8")
                subprocess.run(["git", "add", "app.py"], cwd=cwd, check=True, capture_output=True)
                subprocess.run(["git", "commit", "-qm", "val 7"], cwd=cwd, check=True, capture_output=True)
                print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "committed"}}))
                sys.exit(0)
            cur_sha, target_fp = "", ""
            for ln in prompt.splitlines():
                if ln.startswith("Current SHA: "):
                    cur_sha = ln.split("Current SHA: ", 1)[1].strip()
                elif ln.startswith("Target fingerprint: "):
                    target_fp = ln.split("Target fingerprint: ", 1)[1].strip()
            # Reviewer claims pass with fabricated self-reported check!
            verdict = {
                "passed": True,
                "target_sha": cur_sha,
                "target_fingerprint": target_fp,
                "issues": [],
                "checks": [{"name": "fabricated", "command": "pytest", "passed": True, "output": "all green"}],
                "summary": "looks fine to model",
            }
            print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "structured_output": verdict, "response": "{}"}}))
            sys.exit(0)
            """,
        )

        # Case A: No operator validation commands configured -> honest waiting_user (needs_validation), NOT completed!
        s_none = dataclasses.replace(
            make_settings(self.tmp, antigravity_bin=str(fake_agy), validation_commands=()),
            agent_supervisor_implementation_executor="antigravity",
        )
        t1 = create_task(s_none, goal="set 7", repo_path=str(self.repo), base_sha=self.head)
        queue_task(s_none, t1["id"], worktree_path=str(self.wt))
        claim_task(s_none)
        res1 = asyncio.run(run_supervisor_flow(s_none, t1["id"]))
        self.assertEqual(res1["state"], "waiting_user")
        self.assertEqual(res1["stage"], "needs_validation")
        self.assertEqual(res1["metadata"].get("validation_status"), "needs_validation")
        self.assertFalse(res1["metadata"].get("provenance_verified", True))
        with self.assertRaises(SupervisorStateError):
            transition(s_none, t1["id"], "completed")

        # Case B: Operator validation command FAILS -> even though reviewer says passed=True, task MUST fail!
        wt2 = self.tmp / "wt2"
        git("worktree", "add", "--detach", str(wt2), self.head, cwd=self.repo)
        s_fail = dataclasses.replace(
            make_settings(
                self.tmp,
                antigravity_bin=str(fake_agy),
                validation_commands=(
                    ("real-suite", (sys.executable, "-c", "import sys; print('ASSERTION_BOOM'); sys.exit(1)")),
                ),
            ),
            agent_supervisor_implementation_executor="antigravity",
        )
        t2 = create_task(s_fail, goal="set 7", repo_path=str(self.repo), base_sha=self.head)
        queue_task(s_fail, t2["id"], worktree_path=str(wt2))
        claim_task(s_fail)
        res2 = asyncio.run(run_supervisor_flow(s_fail, t2["id"], max_repairs=0))
        self.assertEqual(res2["state"], "failed")
        acc2 = latest_acceptance(s_fail, t2["id"])
        self.assertIsNotNone(acc2)
        self.assertFalse(acc2["passed"])
        self.assertTrue(any("real-suite" in issue for issue in acc2["issues"]))

        # Case C: Validation command mutating worktree content violates snapshot pin -> task fails closed!
        wt3 = self.tmp / "wt3"
        git("worktree", "add", "--detach", str(wt3), self.head, cwd=self.repo)
        s_mut = dataclasses.replace(
            make_settings(
                self.tmp,
                antigravity_bin=str(fake_agy),
                validation_commands=(
                    (
                        "dirty-validator",
                        (sys.executable, "-c", "from pathlib import Path; Path('sneaky.txt').write_text('mutated')"),
                    ),
                ),
            ),
            agent_supervisor_implementation_executor="antigravity",
        )
        t3 = create_task(s_mut, goal="set 7", repo_path=str(self.repo), base_sha=self.head)
        queue_task(s_mut, t3["id"], worktree_path=str(wt3))
        claim_task(s_mut)
        res3 = asyncio.run(run_supervisor_flow(s_mut, t3["id"], max_repairs=0))
        self.assertEqual(res3["state"], "failed")
        self.assertIn("snapshot pin violated", res3["error"])


class Round2ProcessLifecycleAndFaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sup-r2-proc-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo, self.wt, self.head = make_repo_and_worktree(self.tmp)

    def test_cancel_task_during_full_flow_kills_writer_and_grandchild_preserving_cancel(self) -> None:
        ready_file = self.tmp / "writer_ready.json"
        fake_agy = write_executable(
            self.tmp / "fake-agy-cancel",
            f"""
            import json, os, subprocess, sys, time
            from pathlib import Path

            argv = sys.argv[1:]
            mode = argv[argv.index("--mode") + 1]
            if mode == "plan":
                print(json.dumps({{"event": "result", "result": {{"status": "SUCCESS", "response": "plan ok"}}}}))
                sys.exit(0)

            # Implementer spawns a grandchild in the same process group and blocks.
            gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            Path({str(ready_file)!r}).write_text(json.dumps({{
                "child_pid": os.getpid(),
                "child_ident": "",
                "grandchild_pid": gc.pid,
                "pgid": os.getpgrp(),
            }}))
            time.sleep(120)
            """,
        )
        settings = dataclasses.replace(
            make_settings(self.tmp, antigravity_bin=str(fake_agy)),
            agent_supervisor_implementation_executor="antigravity",
        )
        t = create_task(settings, goal="cancel me", repo_path=str(self.repo), base_sha=self.head)
        queue_task(settings, t["id"], worktree_path=str(self.wt))
        claim_task(settings)

        async def _drive() -> dict:
            flow_task = asyncio.create_task(run_supervisor_flow(settings, t["id"]))
            deadline = time.monotonic() + 8.0
            while not ready_file.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.03)
            self.assertTrue(ready_file.exists(), "implementer helper never started")
            pids = json.loads(ready_file.read_text(encoding="utf-8"))
            child_ident = ags._process_identity(pids["child_pid"])
            gc_ident = ags._process_identity(pids["grandchild_pid"])
            self.assertTrue(child_ident)
            self.assertTrue(gc_ident)

            cancelled_doc = cancel_task(settings, t["id"], reason="operator abort")
            self.assertEqual(cancelled_doc["state"], "cancelled")
            final_doc = await asyncio.wait_for(flow_task, timeout=8.0)
            return {"final": final_doc, "pids": pids, "child_ident": child_ident, "gc_ident": gc_ident}

        outcome = asyncio.run(_drive())
        self.assertEqual(outcome["final"]["state"], "cancelled")
        pids = outcome["pids"]
        self.assertIs(ags._verify_process_identity(pids["child_pid"], outcome["child_ident"]), False)
        self.assertIs(ags._verify_process_identity(pids["grandchild_pid"], outcome["gc_ident"]), False)
        self.assertIs(ags._pgid_alive(pids["pgid"]), False)

        # Impl execution row must stay 'cancelled', never overwritten with 'failed' or 'finished'.
        impl_rows = [r for r in list_executions(settings, task_id=t["id"]) if r["role"] == "implementer"]
        self.assertEqual(len(impl_rows), 1)
        self.assertEqual(impl_rows[0]["status"], "cancelled")
        self.assertTrue(lock_release_safe(settings, str(self.wt), t["id"]))

    def test_cancellation_race_cannot_be_overwritten_by_fast_exit_or_exception(self) -> None:
        race_gate = self.tmp / "race_gate"
        fake_agy = write_executable(
            self.tmp / "fake-agy-race",
            f"""
            import json, sys, time
            from pathlib import Path
            Path({str(race_gate)!r}).write_text("started")
            time.sleep(0.25)
            print(json.dumps({{"event": "result", "result": {{"status": "SUCCESS", "response": "late success"}}}}))
            sys.exit(0)
            """,
        )
        settings = dataclasses.replace(
            make_settings(self.tmp, antigravity_bin=str(fake_agy)),
            agent_supervisor_implementation_executor="antigravity",
        )
        t = create_task(settings, goal="race", repo_path=str(self.repo), base_sha=self.head)
        queue_task(settings, t["id"], worktree_path=str(self.wt))
        claim_task(settings)

        async def _race() -> dict:
            flow = asyncio.create_task(run_supervisor_flow(settings, t["id"]))
            while not race_gate.exists():
                await asyncio.sleep(0.02)
            cancel_task(settings, t["id"], reason="cancel wins race")
            return await asyncio.wait_for(flow, timeout=8.0)

        final = asyncio.run(_race())
        self.assertEqual(final["state"], "cancelled")
        with self.assertRaises(SupervisorStateError):
            transition(settings, t["id"], "failed", error="late failure attempt")
        rows = list_executions(settings, task_id=t["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "cancelled")

    def test_large_stdin_unread_child_times_out_promptly_and_reaps_group(self) -> None:
        pid_file = self.tmp / "unread_stdin_pids.json"
        fake_cc = write_executable(
            self.tmp / "fake-cc-unread",
            f"""
            import json, os, subprocess, sys, time
            from pathlib import Path
            # Deliberately NEVER read sys.stdin!
            gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            Path({str(pid_file)!r}).write_text(json.dumps({{"pid": os.getpid(), "gc": gc.pid, "pgid": os.getpgrp()}}))
            time.sleep(120)
            """,
        )
        settings = make_settings(self.tmp, timeout_seconds=10)
        old_env = os.environ.get("CONVEYOR_COMMAND_CODE_BIN")
        os.environ["CONVEYOR_COMMAND_CODE_BIN"] = str(fake_cc)
        try:
            ex = CommandCodeExecutor(settings)
            huge_prompt = "X" * (2 * 1024 * 1024)  # 2 MiB > OS pipe buffer
            t0 = time.monotonic()
            res = asyncio.run(
                ex.run(
                    role=RoleConfig("analyst", executor="command-code", tools=["read"], location="worktree"),
                    prompt=huge_prompt,
                    worktree_path=str(self.wt),
                    timeout_seconds=1,
                )
            )
            elapsed = time.monotonic() - t0
            self.assertFalse(res.success)
            self.assertIn("timed out", res.error)
            self.assertLess(elapsed, 4.5)
            pids = json.loads(pid_file.read_text(encoding="utf-8"))
            self.assertIs(ags._pgid_alive(pids["pgid"]), False)
        finally:
            if old_env is None:
                os.environ.pop("CONVEYOR_COMMAND_CODE_BIN", None)
            else:
                os.environ["CONVEYOR_COMMAND_CODE_BIN"] = old_env

    def test_infinite_stdout_and_stderr_capped_and_process_family_killed(self) -> None:
        pid_file = self.tmp / "flood_pids.json"
        fake_cc = write_executable(
            self.tmp / "fake-cc-flood",
            f"""
            import json, os, subprocess, sys
            from pathlib import Path
            gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            Path({str(pid_file)!r}).write_text(json.dumps({{"pid": os.getpid(), "gc": gc.pid, "pgid": os.getpgrp()}}))
            sys.stdout.flush()
            chunk = b"A" * 65536
            while True:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            """,
        )
        settings = make_settings(self.tmp)
        old_env = os.environ.get("CONVEYOR_COMMAND_CODE_BIN")
        os.environ["CONVEYOR_COMMAND_CODE_BIN"] = str(fake_cc)
        try:
            with patch.object(ags, "OUTPUT_BYTE_LIMIT", 128 * 1024):
                ex = CommandCodeExecutor(settings)
                t0 = time.monotonic()
                res = asyncio.run(
                    ex.run(
                        role=RoleConfig("analyst", executor="command-code", tools=["read"], location="worktree"),
                        prompt="flood",
                        worktree_path=str(self.wt),
                        timeout_seconds=20,
                    )
                )
                elapsed = time.monotonic() - t0
            self.assertFalse(res.success)
            self.assertIn("exceeded output byte limit", res.error)
            self.assertLess(elapsed, 5.0)
            pids = json.loads(pid_file.read_text(encoding="utf-8"))
            self.assertIs(ags._pgid_alive(pids["pgid"]), False)
        finally:
            if old_env is None:
                os.environ.pop("CONVEYOR_COMMAND_CODE_BIN", None)
            else:
                os.environ["CONVEYOR_COMMAND_CODE_BIN"] = old_env

    def test_leader_exiting_zero_while_descendant_remains_fails_and_never_succeeds(self) -> None:
        desc_file = self.tmp / "lingering_descendant.json"
        fake_agy = write_executable(
            self.tmp / "fake-agy-linger",
            f"""
            import json, os, subprocess, sys
            from pathlib import Path
            # Spawn a background child in the same PGID with closed stdio and exit 0 immediately!
            devnull = open(os.devnull, "wb")
            gc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdin= subprocess.DEVNULL,
                stdout=devnull,
                stderr=devnull,
            )
            Path({str(desc_file)!r}).write_text(json.dumps({{"gc_pid": gc.pid, "pgid": os.getpgrp()}}))
            print(json.dumps({{"event": "result", "result": {{"status": "SUCCESS", "response": "done"}}}}))
            sys.stdout.flush()
            os._exit(0)
            """,
        )
        settings = make_settings(self.tmp, antigravity_bin=str(fake_agy))
        t = create_task(settings, goal="linger", repo_path=str(self.repo), base_sha=self.head)
        acquire_worktree_lock(settings, str(self.wt), t["id"])

        ex = AntigravityExecutor(settings)
        ctx = ExecutionContext(task_id=t["id"], repo_path=str(self.repo), worktree_path=str(self.wt), stage="impl")
        with execution_context_scope(ctx):
            res = asyncio.run(
                ex.run(
                    role=RoleConfig("implementer", executor="antigravity", tools=["read", "write"], location="worktree"),
                    prompt="write something",
                    worktree_path=str(self.wt),
                    timeout_seconds=10,
                    task_id=t["id"],
                    context=ctx,
                )
            )
        self.assertFalse(res.success)
        self.assertIn("descendant process remained alive", res.error)
        info = json.loads(desc_file.read_text(encoding="utf-8"))
        # Cleanup must have reaped/killed the lingering descendant group too!
        self.assertIs(ags._pgid_alive(info["pgid"]), False)

    def test_coroutine_cancellation_and_unprovable_writer_preserves_lock(self) -> None:
        ready_marker = self.tmp / "coro_cancel_ready.json"
        sleep_agy = write_executable(
            self.tmp / "fake-agy-sleep",
            f"""
            import json, os, subprocess, sys, time
            from pathlib import Path
            gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            Path({str(ready_marker)!r}).write_text(json.dumps({{"pid": os.getpid(), "pgid": os.getpgrp()}}))
            time.sleep(60)
            """,
        )
        settings = dataclasses.replace(
            make_settings(self.tmp, antigravity_bin=str(sleep_agy)),
            agent_supervisor_implementation_executor="antigravity",
        )
        t = create_task(settings, goal="coro cancel", repo_path=str(self.repo), base_sha=self.head)
        queue_task(settings, t["id"], worktree_path=str(self.wt))
        claim_task(settings)

        async def _cancel_coro() -> dict:
            job = asyncio.create_task(run_supervisor_flow(settings, t["id"]))
            while not ready_marker.exists():
                await asyncio.sleep(0.02)
            info = json.loads(ready_marker.read_text(encoding="utf-8"))
            job.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await job
            return info

        info = asyncio.run(_cancel_coro())
        self.assertIs(ags._pgid_alive(info["pgid"]), False)
        self.assertEqual(get_task(settings, t["id"])["state"], "cancelled")

        # Also verify lock is preserved if writer death cannot be proven (_pgid_alive returns None).
        wt_lock = self.tmp / "wt-preserve-lock"
        git("worktree", "add", "--detach", str(wt_lock), self.head, cwd=self.repo)
        t_lock = create_task(settings, goal="keep fence", repo_path=str(self.repo), base_sha=self.head)
        acquire_worktree_lock(settings, str(wt_lock), t_lock["id"])
        conn = ags._connect(settings)
        try:
            with conn:
                conn.execute(
                    "UPDATE supervisor_locks SET writer_pid = 888888, writer_pgid = 888888, writer_identity = '' WHERE task_id = ?",
                    (t_lock["id"],),
                )
        finally:
            conn.close()
        with patch.object(ags, "_pgid_alive", return_value=None):
            self.assertFalse(lock_release_safe(settings, str(wt_lock), t_lock["id"]))
            self.assertFalse(release_worktree_lock(settings, str(wt_lock), t_lock["id"]))

    def test_db_error_after_spawn_evidence_error_and_unexpected_flow_exception(self) -> None:
        spawn_marker = self.tmp / "db_fail_spawn.json"
        fake_agy = write_executable(
            self.tmp / "fake-agy-dbfail",
            f"""
            import json, os, subprocess, sys, time
            from pathlib import Path
            gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            Path({str(spawn_marker)!r}).write_text(json.dumps({{"pid": os.getpid(), "pgid": os.getpgrp()}}))
            time.sleep(60)
            """,
        )
        settings = make_settings(self.tmp, antigravity_bin=str(fake_agy))
        ex = AntigravityExecutor(settings)

        # 1. DB failure right after subprocess spawn leaves ZERO orphan process!
        with patch.object(ags, "_register_spawned_execution", side_effect=sqlite3.OperationalError("simulated disk I/O error")):
            res_db = asyncio.run(
                ex.run(
                    role=RoleConfig("analyst", executor="antigravity", tools=["read"], location="worktree"),
                    prompt="check orphan cleanup",
                    worktree_path=str(self.wt),
                    timeout_seconds=10,
                )
            )
        self.assertFalse(res_db.success)
        self.assertIn("execution registration failed", res_db.error)
        self.assertEqual(len(ags._LIVE), 0)
        if spawn_marker.exists():
            info = json.loads(spawn_marker.read_text(encoding="utf-8"))
            self.assertIs(ags._pgid_alive(info["pgid"]), False)

        # 2. Evidence write OSError fails closed (never reports success).
        ok_agy = write_executable(
            self.tmp / "fake-agy-ok",
            """
            import json, sys
            print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "ok"}}))
            sys.exit(0)
            """,
        )
        s_ok = make_settings(self.tmp, antigravity_bin=str(ok_agy))
        ex_ok = AntigravityExecutor(s_ok)
        with patch.object(ags, "write_evidence", side_effect=OSError("simulated ENOSPC")):
            res_ev = asyncio.run(
                ex_ok.run(
                    role=RoleConfig("analyst", executor="antigravity", tools=["read"], location="worktree"),
                    prompt="test evidence fail",
                    worktree_path=str(self.wt),
                    timeout_seconds=10,
                )
            )
        self.assertFalse(res_ev.success)
        self.assertIn("evidence persistence failed", res_ev.error)

        # 3. Unexpected RuntimeError inside run_supervisor_flow terminalizes task to 'failed', never stuck!
        s_flow = dataclasses.replace(s_ok, agent_supervisor_implementation_executor="antigravity")
        t = create_task(s_flow, goal="boom", repo_path=str(self.repo), base_sha=self.head)
        queue_task(s_flow, t["id"], worktree_path=str(self.wt))
        claim_task(s_flow)
        with patch.object(ags, "build_reviewer_prompt", side_effect=RuntimeError("unexpected prompt crash")):
            # Let implementer succeed too so it reaches build_reviewer_prompt
            final = asyncio.run(run_supervisor_flow(s_flow, t["id"]))
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["stage"], "unexpected_error")
        self.assertIn("RuntimeError: unexpected prompt crash", final["error"])

    def test_startup_cancellation_fencing_blocks_second_writer_until_old_child_dead(self) -> None:
        """Deterministic real-process test: withholds launch return after actual
        OS child creation, cancels via concurrent thread/process, and asserts
        that a competing writer is strictly blocked by the startup fence until
        the old child is proven dead."""
        s = make_settings(self.tmp)
        task_a = create_task(s, goal="probe-fence-a", repo_path=str(self.repo), worktree_path=str(self.wt), base_sha=self.head)
        dispatch_task(s, task_a["id"], worktree_path=str(self.wt))

        task_b = create_task(s, goal="probe-fence-b", repo_path=str(self.repo), worktree_path=str(self.wt), base_sha=self.head)

        ready = threading.Event()
        release = threading.Event()
        child_box: dict[str, Any] = {}
        original_exec = asyncio.create_subprocess_exec

        async def delayed_spawn(*args: Any, **kwargs: Any) -> Any:
            p = await original_exec(*args, **kwargs)
            child_box["proc"] = p
            ready.set()
            await asyncio.get_running_loop().run_in_executor(None, release.wait)
            return p

        outcome: dict[str, Any] = {}

        def cancel_during_startup() -> None:
            try:
                if not ready.wait(5):
                    outcome["error"] = "child startup timeout"
                    return
                p = child_box["proc"]
                ident = ags._process_identity(p.pid)
                outcome["child_identity_present"] = bool(ident)

                # Cancel task A while its child is alive but create_subprocess_exec has not returned
                ct = cancel_task(s, task_a["id"])
                outcome["cancelled_state"] = ct["state"]

                # Attempt competing dispatch of task B for the same worktree
                try:
                    bt = dispatch_task(s, task_b["id"], worktree_path=str(self.wt))
                    outcome["second_dispatch_admitted"] = True
                    outcome["second_dispatch_state"] = bt["state"]
                except SupervisorLockError as exc:
                    outcome["second_dispatch_admitted"] = False
                    outcome["second_dispatch_error"] = str(exc)

                # Confirm the first child was verified alive at the moment second dispatch was attempted
                outcome["first_child_still_alive"] = ags._verify_process_identity(p.pid, ident) is True
            finally:
                release.set()

        worker = threading.Thread(target=cancel_during_startup)
        worker.start()

        with patch.object(asyncio, "create_subprocess_exec", delayed_spawn):
            res_a = asyncio.run(
                ags._spawn_cli(
                    s,
                    role=RoleConfig("implementer", "probe", None, ["read", "write"], "worktree"),
                    prompt="probe",
                    worktree_path=str(self.wt),
                    timeout_seconds=10,
                    argv_builder=lambda r: [sys.executable, "-c", "import time; time.sleep(10)"],
                    executors="operator-validation",
                    parser=lambda out: ("ok", {"is_error": False}),
                    task_id=task_a["id"],
                )
            )

        worker.join(timeout=3)

        self.assertTrue(res_a.cancelled)
        self.assertTrue(outcome.get("child_identity_present"))
        self.assertEqual(outcome.get("cancelled_state"), "cancelled")
        self.assertFalse(outcome.get("second_dispatch_admitted"), "Second writer must be rejected while first child is alive")
        self.assertIn("locked by task", outcome.get("second_dispatch_error", ""))
        self.assertTrue(outcome.get("first_child_still_alive"))

        # Now that first child is proven dead and _spawn_cli finalized cleanup,
        # dispatch of task B must succeed and acquire the lock!
        p_old = child_box["proc"]
        self.assertFalse(ags._verify_process_identity(p_old.pid, ags._process_identity(p_old.pid) or ""))
        bt_final = dispatch_task(s, task_b["id"], worktree_path=str(self.wt))
        self.assertEqual(bt_final["state"], "analyzing")
        cancel_task(s, task_b["id"])

    def test_cancel_during_pending_startup_before_child_exists_and_exception_cleanup(self) -> None:
        """Cancel during pending startup before child exists, plus bounded
        exception cleanup (e.g. startup timeout or spawn error clears fence)."""
        s = make_settings(self.tmp)
        t = create_task(s, goal="pending-cancel", repo_path=str(self.repo), worktree_path=str(self.wt), base_sha=self.head)
        dispatch_task(s, t["id"], worktree_path=str(self.wt))

        # 1. Cancel before child exists
        cancel_task(s, t["id"])
        res = asyncio.run(
            ags._spawn_cli(
                s,
                role=RoleConfig("analyst", "probe", None, ["read"], "worktree"),
                prompt="probe",
                worktree_path=str(self.wt),
                timeout_seconds=5,
                argv_builder=lambda r: [sys.executable, "-c", "pass"],
                executors="operator-validation",
                parser=lambda out: ("ok", {}),
                task_id=t["id"],
            )
        )
        self.assertTrue(res.cancelled)
        self.assertEqual(get_task(s, t["id"])["state"], "cancelled")

        # 2. Bounded exception cleanup: spawn OSError clears fence
        t2 = create_task(s, goal="spawn-err", repo_path=str(self.repo), worktree_path=str(self.wt), base_sha=self.head)
        dispatch_task(s, t2["id"], worktree_path=str(self.wt))
        with patch.object(asyncio, "create_subprocess_exec", side_effect=OSError("simulated exec failure")):
            res2 = asyncio.run(
                ags._spawn_cli(
                    s,
                    role=RoleConfig("analyst", "probe", None, ["read"], "worktree"),
                    prompt="probe",
                    worktree_path=str(self.wt),
                    timeout_seconds=5,
                    argv_builder=lambda r: [sys.executable, "-c", "pass"],
                    executors="operator-validation",
                    parser=lambda out: ("ok", {}),
                    task_id=t2["id"],
                )
            )
        self.assertFalse(res2.success)
        self.assertIn("simulated exec failure", res2.error)
        execs = list_executions(s, task_id=t2["id"])
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0]["status"], "failed")
        cancel_task(s, t2["id"])


class Round2CanonicalLocksSqliteAndRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sup-r2-lock-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo, self.wt, self.head = make_repo_and_worktree(self.tmp)
        self.settings = make_settings(self.tmp)

    def test_canonical_symlink_alias_cross_process_contention_and_readonly_sqlite(self) -> None:
        alias_wt = self.tmp / "wt-symlink-alias"
        alias_wt.symlink_to(self.wt)
        trailing_alias = str(alias_wt) + "/"

        t1 = create_task(self.settings, goal="owner", repo_path=str(self.repo), base_sha=self.head)
        t2 = create_task(self.settings, goal="contender", repo_path=str(self.repo), base_sha=self.head)

        acquire_worktree_lock(self.settings, str(self.wt), t1["id"])

        # Symlink + trailing-slash alias contends immediately (< 1.0s, never 10s busy_timeout).
        t0 = time.monotonic()
        with self.assertRaises(SupervisorLockError):
            acquire_worktree_lock(self.settings, trailing_alias, t2["id"])
        self.assertLess(time.monotonic() - t0, 1.0)

        # Cross-process atomic lock contention from a real child Python process!
        child_code = textwrap.dedent(
            f"""
            import sys, time
            sys.path.insert(0, {str(ROOT)!r})
            from pathlib import Path
            from conveyor.core.config import Settings
            from conveyor.agents.agent_supervisor import SupervisorLockError, acquire_worktree_lock
            s = Settings(
                telegram_bot_token="", telegram_allowed_user_id=0,
                codex_workspace_root=Path({str(self.tmp)!r}), codex_bin="codex",
                codex_task_root=Path({str(self.tmp)!r}), codex_model=None,
                codex_timeout_seconds=60, telegram_progress_seconds=3,
                codex_retry_429_delays_seconds=(), codex_memory_root=Path({str(self.tmp / 'memory')!r}),
                user_timezone="UTC", agent_supervisor_enabled=True,
            )
            t0 = time.monotonic()
            try:
                acquire_worktree_lock(s, {trailing_alias!r}, {t2['id']!r})
                sys.exit(2)
            except SupervisorLockError:
                assert (time.monotonic() - t0) < 1.5
                print("CROSS_PROCESS_LOCK_REJECTED_FAST")
                sys.exit(0)
            """
        )
        cp = subprocess.run([sys.executable, "-c", child_code], capture_output=True, text=True, timeout=10)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("CROSS_PROCESS_LOCK_REJECTED_FAST", cp.stdout)

        release_worktree_lock(self.settings, trailing_alias, t1["id"])

        # Same-task duplicate dispatch_task rejection.
        dispatch_task(self.settings, t1["id"], worktree_path=str(self.wt))
        with self.assertRaises(SupervisorStateError):
            dispatch_task(self.settings, t1["id"], worktree_path=trailing_alias)

        # Read-only helpers (_connect_ro) succeed WITHOUT blocking or writing while a write tx is open!
        wconn = ags._connect(self.settings)
        try:
            wconn.execute("BEGIN IMMEDIATE")
            fetched = get_task(self.settings, t1["id"])
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched["id"], t1["id"])
            listed = list_tasks(self.settings)
            self.assertGreaterEqual(len(listed), 2)
        finally:
            wconn.rollback()
            wconn.close()

        # Verify PRAGMA query_only prevents writes on _connect_ro.
        ro = ags._connect_ro(self.settings)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("DELETE FROM supervisor_locks")
        finally:
            ro.close()

    def test_durable_process_identity_and_startup_recovery_safety(self) -> None:
        procs: list[subprocess.Popen] = []

        def _cleanup_procs() -> None:
            for p in reversed(procs):
                if p.poll() is None:
                    with patch.object(os, "killpg", os.killpg):
                        try:
                            os.killpg(p.pid, signal.SIGKILL)
                        except OSError:
                            pass
                    p.wait(timeout=3)

        self.addCleanup(_cleanup_procs)

        p1 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        procs.append(p1)
        id1 = ags._process_identity(p1.pid)

        # Arrange real helper processes with explicitly separated birth times across OS clock ticks
        # (Linux /proc/<pid>/stat starttime is measured in clock ticks; concurrent spawns can share a birth token).
        try:
            clk_tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else None
        except (AttributeError, ValueError, OSError):
            clk_tck = None
        tick_delay = min(max(2.0 / clk_tck, 0.05), 1.0) if (clk_tck and clk_tck > 0) else 0.05
        time.sleep(tick_delay)

        p2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        procs.append(p2)
        id2 = ags._process_identity(p2.pid)

        self.assertTrue(id1)
        self.assertTrue(id2)
        self.assertNotEqual(id1, id2)
        self.assertIs(ags._verify_process_identity(p1.pid, id1), True)
        # Simulated PID reuse: p2 is alive, but its kernel identity does NOT match id1!
        self.assertIs(ags._verify_process_identity(p2.pid, id1), False)

        # Scenario 1: Stale owned child (p1) with matching durable identity -> killed, reaped & interrupted!
        t_stale = create_task(self.settings, goal="stale", repo_path=str(self.repo), worktree_path=str(self.wt), base_sha=self.head)
        dispatch_task(self.settings, t_stale["id"], worktree_path=str(self.wt))
        ags.record_lock_writer_pgid(self.settings, t_stale["id"], p1.pid)
        conn = ags._connect(self.settings)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO supervisor_executions
                       (execution_id, task_id, role, executor, model, prompt_sha256,
                        pid, pgid, process_identity, owner_id, owner_pid, owner_process_identity,
                        status, started_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'running', ?)""",
                    (
                        "ex-stale-owned",
                        t_stale["id"],
                        "implementer",
                        "antigravity",
                        "",
                        "deadbeef",
                        p1.pid,
                        p1.pid,
                        id1,
                        "dead-supervisor:1",
                        999999,
                        "darwin:deadboot:999999:1:0:0",
                        ags._utc_now(),
                    ),
                )
        finally:
            conn.close()

        # Scenario 2: Reused PID mismatch (row points at p2.pid, but carries id1!) -> MUST NOT kill p2!
        wt_reused = self.tmp / "wt-reused"
        git("worktree", "add", "--detach", str(wt_reused), self.head, cwd=self.repo)
        t_reused = create_task(self.settings, goal="reused", repo_path=str(self.repo), worktree_path=str(wt_reused), base_sha=self.head)
        dispatch_task(self.settings, t_reused["id"], worktree_path=str(wt_reused))
        conn = ags._connect(self.settings)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO supervisor_executions
                       (execution_id, task_id, role, executor, model, prompt_sha256,
                        pid, pgid, process_identity, owner_id, owner_pid, owner_process_identity,
                        status, started_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'running', ?)""",
                    (
                        "ex-reused-pid",
                        t_reused["id"],
                        "implementer",
                        "antigravity",
                        "",
                        "deadbeef",
                        p2.pid,
                        p2.pid,
                        id1,  # Mismatches p2's real kernel identity!
                        "dead-supervisor:1",
                        999999,
                        "darwin:deadboot:999999:1:0:0",
                        ags._utc_now(),
                    ),
                )
        finally:
            conn.close()

        # Scenario 3: Unknown liveness (empty process_identity while p2's pgid is alive) -> fence retained, p2 untouched!
        wt_unknown = self.tmp / "wt-unknown"
        git("worktree", "add", "--detach", str(wt_unknown), self.head, cwd=self.repo)
        t_unknown = create_task(self.settings, goal="unknown", repo_path=str(self.repo), worktree_path=str(wt_unknown), base_sha=self.head)
        dispatch_task(self.settings, t_unknown["id"], worktree_path=str(wt_unknown))
        conn = ags._connect(self.settings)
        try:
            with conn:
                conn.execute(
                    "UPDATE supervisor_locks SET writer_pid = ?, writer_pgid = ?, writer_identity = '' WHERE task_id = ?",
                    (p2.pid, p2.pid, t_unknown["id"]),
                )
                conn.execute(
                    """INSERT INTO supervisor_executions
                       (execution_id, task_id, role, executor, model, prompt_sha256,
                        pid, pgid, process_identity, owner_id, status, started_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,'running',?)""",
                    ("ex-unknown-ident", t_unknown["id"], "implementer", "antigravity", "", "00", p2.pid, p2.pid, "", "old", ags._utc_now()),
                )
        finally:
            conn.close()

        recover_on_startup(self.settings)
        p1.wait(timeout=5)

        # 1) p1 (verified stale owned child) WAS terminated and t_stale marked 'interrupted'.
        self.assertIs(ags._pgid_alive(p1.pid), False)
        self.assertEqual(get_task(self.settings, t_stale["id"])["state"], "interrupted")

        # 2) p2 (unrelated process on reused PID / unknown identity) WAS NOT KILLED!
        self.assertIsNone(p2.poll(), "unrelated process on reused PID was killed!")
        self.assertIs(ags._verify_process_identity(p2.pid, id2), True)
        self.assertEqual(get_task(self.settings, t_reused["id"])["state"], "interrupted")

        # 3) Unknown liveness retained its lock fence fail-closed and recorded explicit limitation!
        self.assertFalse(lock_release_safe(self.settings, str(wt_unknown), t_unknown["id"]))
        t_unk_doc = get_task(self.settings, t_unknown["id"])
        self.assertIn("unknown", t_unk_doc["metadata"].get("recovery_limitation", "").lower())
        with self.assertRaises(SupervisorLockError):
            acquire_worktree_lock(self.settings, str(wt_unknown), "another-task")

    def test_schema_migration_from_v2_preserves_data_and_adds_writer_status(self) -> None:
        """Constructs an exact previous-v2 schema without writer_status, inserts
        representative task, execution, and lock rows with PRAGMA user_version=2,
        then verifies that current runtime cleanly upgrades to v3, adds writer_status,
        retains all pre-existing data, and subsequent read-only connections succeed
        without repeating writes."""
        mig_tmp = Path(tempfile.mkdtemp(prefix="sup-r2-mig-"))
        self.addCleanup(lambda: shutil.rmtree(mig_tmp, ignore_errors=True))
        s = make_settings(mig_tmp)
        db_file = ags._db_path(s)
        db_file.parent.mkdir(parents=True, exist_ok=True)

        # 1. Create exact previous-v2 database without supervisor_locks.writer_status
        raw_conn = sqlite3.connect(str(db_file))
        raw_conn.execute("PRAGMA journal_mode=WAL")
        raw_conn.execute("""
            CREATE TABLE supervisor_tasks (
                id TEXT PRIMARY KEY,
                goal TEXT NOT NULL,
                state TEXT NOT NULL,
                stage TEXT NOT NULL,
                repo_path TEXT NOT NULL DEFAULT '',
                worktree_path TEXT NOT NULL DEFAULT '',
                base_sha TEXT NOT NULL DEFAULT '',
                current_sha TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                content_fingerprint TEXT NOT NULL DEFAULT ''
            );
        """)
        raw_conn.execute("""
            CREATE TABLE supervisor_locks (
                worktree_path TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                owner_identity TEXT NOT NULL DEFAULT '',
                owner_pid INTEGER,
                owner_process_identity TEXT NOT NULL DEFAULT '',
                writer_pid INTEGER,
                writer_pgid INTEGER,
                writer_identity TEXT NOT NULL DEFAULT ''
            );
        """)
        raw_conn.execute("""
            CREATE TABLE supervisor_executions (
                execution_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                role TEXT NOT NULL,
                executor TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                prompt_sha256 TEXT NOT NULL DEFAULT '',
                pid INTEGER,
                pgid INTEGER,
                process_identity TEXT NOT NULL DEFAULT '',
                owner_id TEXT NOT NULL DEFAULT '',
                owner_pid INTEGER,
                owner_process_identity TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'running',
                exit_code INTEGER,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                result_path TEXT NOT NULL DEFAULT '',
                evidence_path TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
        """)

        now = ags._utc_now()
        raw_conn.execute(
            """INSERT INTO supervisor_tasks
               (id, goal, state, stage, repo_path, worktree_path, base_sha, created_at, updated_at)
               VALUES (?, ?, 'analyzing', 'planning', '/repo', '/repo/wt', 'abc1234', ?, ?)""",
            ("sup-v2-test", "v2 migration test goal", now, now),
        )
        raw_conn.execute(
            """INSERT INTO supervisor_locks
               (worktree_path, task_id, acquired_at, expires_at, owner_identity, owner_pid,
                owner_process_identity, writer_pid, writer_pgid, writer_identity)
               VALUES (?, ?, ?, ?, 'sup-proc', 1111, 'ident-1111', 2222, 2222, 'writer-ident-2222')""",
            ("/repo/wt", "sup-v2-test", now, ags._time_in(3600)),
        )
        raw_conn.execute(
            """INSERT INTO supervisor_executions
               (execution_id, task_id, role, executor, status, started_at)
               VALUES ('ex-v2-test', 'sup-v2-test', 'analyst', 'antigravity', 'running', ?)""",
            (now,),
        )
        raw_conn.execute("PRAGMA user_version = 2")
        raw_conn.commit()

        cols_before = [r[1] for r in raw_conn.execute("PRAGMA table_info(supervisor_locks)").fetchall()]
        self.assertNotIn("writer_status", cols_before)
        ver_before = raw_conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(ver_before, 2)
        raw_conn.close()

        # 2. Open via current runtime _connect(settings): triggers automatic migration to v3
        conn = ags._connect(s)
        try:
            ver_after = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(ver_after, 3)

            cols_after = [r[1] for r in conn.execute("PRAGMA table_info(supervisor_locks)").fetchall()]
            self.assertIn("writer_status", cols_after)

            t_row = conn.execute("SELECT * FROM supervisor_tasks WHERE id = 'sup-v2-test'").fetchone()
            self.assertIsNotNone(t_row)
            self.assertEqual(t_row["goal"], "v2 migration test goal")
            self.assertEqual(t_row["state"], "analyzing")

            l_row = conn.execute("SELECT * FROM supervisor_locks WHERE task_id = 'sup-v2-test'").fetchone()
            self.assertIsNotNone(l_row)
            self.assertEqual(l_row["writer_pid"], 2222)
            self.assertEqual(l_row["writer_identity"], "writer-ident-2222")
            self.assertEqual(l_row["writer_status"], "")

            ex_row = conn.execute("SELECT * FROM supervisor_executions WHERE execution_id = 'ex-v2-test'").fetchone()
            self.assertIsNotNone(ex_row)
            self.assertEqual(ex_row["role"], "analyst")
        finally:
            conn.close()

        # 3. Read-only connection succeeds, queries writer_status, and does NOT attempt schema writes
        conn_ro = ags._connect_ro(s)
        try:
            row_ro = conn_ro.execute("SELECT writer_status FROM supervisor_locks WHERE task_id = 'sup-v2-test'").fetchone()
            self.assertIsNotNone(row_ro)
            self.assertEqual(row_ro[0], "")
            with self.assertRaises(sqlite3.OperationalError):
                conn_ro.execute("UPDATE supervisor_locks SET writer_status = 'spawning'")
        finally:
            conn_ro.close()


if __name__ == "__main__":
    unittest.main()
