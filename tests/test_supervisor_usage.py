"""tests/test_supervisor_usage.py — per-run model usage persisted and aggregated (I1).

Owners start long goals but cannot see what they cost. These tests pin the two
halves of the fix: (1) every supervisor executor run that reports usage stores
it once, next to its execution row, and runs that report nothing store nothing;
(2) the task dict and the goal dict both expose the summed usage.

No real model API or CLI is run: the Pi/Claude executors are driven through the
real ``_spawn_cli`` subprocess path with fake binaries, exactly like
tests/test_supervisor_pi_executor.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("CONVEYOR_AGENT_SUPERVISOR_ENABLED", "true")

from conveyor.agents import agent_supervisor as ags
from conveyor.agents import chat_intents
from conveyor.agents import supervisor_goals as goals
from conveyor.agents import supervisor_service as svc
from conveyor.agents.agent_supervisor import PiExecutor, RoleConfig
from conveyor.core.config import Settings

ANALYST = RoleConfig(name="analyst", executor="pi", tools=["read"], location="worktree")
IMPLEMENTER = RoleConfig(name="implementer", executor="pi", tools=["read", "write"], location="worktree")
CLAUDE_ANALYST = RoleConfig(name="analyst", executor="claude", tools=["read"], location="worktree")


def _fake_pi_source(usage: dict[str, int] | None, text: str) -> str:
    """A fake ``pi`` that emits one assistant turn with (or without) usage."""
    message: dict[str, object] = {"role": "assistant",
                                  "content": [{"type": "text", "text": text}]}
    if usage is not None:
        message["usage"] = usage
    message_end = json.dumps({"type": "message_end", "message": message})
    agent_end = json.dumps({"type": "agent_end", "messages": [
        {"role": "assistant", "content": [{"type": "text", "text": text}]}]})
    return (
        "#!__PY__\n"
        "import sys\n"
        "sys.stdin.read()\n"
        f"print({message_end!r}, flush=True)\n"
        f"print({agent_end!r}, flush=True)\n"
    )


def _fake_claude_source() -> str:
    """A fake ``claude`` whose result envelope carries no usage at all."""
    return (
        "#!__PY__\n"
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'result', 'result': 'claude done', 'is_error': False}), flush=True)\n"
    )


def _clean_env(**extra: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp"}
    env.update(extra)
    return env


def _make_settings(tmp: Path) -> Settings:
    return Settings(
        telegram_bot_token="",
        telegram_allowed_user_id=0,
        codex_workspace_root=tmp,
        codex_bin="codex",
        codex_task_root=tmp / "tasks",
        codex_model=None,
        codex_timeout_seconds=60,
        telegram_progress_seconds=3,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=tmp / "memory",
        user_timezone="UTC",
        conveyor_web_enabled=True,
        conveyor_web_host="127.0.0.1",
        conveyor_web_port=0,
        conveyor_web_token="usage-test-token-0123456789-abcdefghijklmnop",
        agent_supervisor_enabled=True,
        agent_supervisor_task_timeout_seconds=30,
        agent_supervisor_validation_commands=(),
    )


def _git(*argv: str, cwd: Path) -> str:
    r = subprocess.run(["git", *argv], cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {argv} failed: {r.stderr[:300]}")
    return (r.stdout or "").strip()


def _init_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=repo_dir)
    _git("config", "user.email", "usage@test.local", cwd=repo_dir)
    _git("config", "user.name", "Usage Test", cwd=repo_dir)
    (repo_dir / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git("add", "app.py", cwd=repo_dir)
    _git("commit", "-qm", "initial", cwd=repo_dir)
    return _git("rev-parse", "HEAD", cwd=repo_dir)


def _write_fake(path: Path, template: str) -> Path:
    path.write_text(template.replace("__PY__", sys.executable), encoding="utf-8")
    path.chmod(0o755)
    return path


class _UsageCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="usage-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.settings = _make_settings(self.tmp)
        self.repo = self.tmp / "repo"
        self.head = _init_repo(self.repo)
        self.wt = self.tmp / "wt"
        _git("worktree", "add", "--detach", str(self.wt), self.head, cwd=self.repo)
        self._task_ids: list[str] = []

    def _create_goal(self) -> dict[str, object]:
        repo, _created = svc.register_repository(self.settings, path=str(self.repo))
        wt = svc.create_managed_worktree(self.settings, repo["id"], branch="feat/usage")
        return goals.create_goal(None, self.settings, {
            "outcome": "在 managed worktree 里新增 feature.txt，内容为 hello-usage，并干净提交",
            "repo_id": repo["id"],
            "worktree_id": wt["id"],
        })

    def _new_task(self) -> dict[str, object]:
        # One active supervisor task per worktree: park the previous one in a
        # terminal state before dispatching the next (the lock is correct).
        for prev in self._task_ids:
            ags.transition(self.settings, prev, "failed", stage="failed")
        task = ags.create_task(self.settings, goal="ship the thing",
                               repo_path=str(self.repo), worktree_path=str(self.wt),
                               base_sha=self.head)
        self._task_ids.append(str(task["id"]))
        ags.dispatch_task(self.settings, task["id"], worktree_path=str(self.wt))
        return task

    def _run_pi(self, task_id: str, role: RoleConfig, usage: dict[str, int] | None) -> ags.ExecutionResult:
        fake_pi = _write_fake(self.tmp / f"fake-pi-{os.urandom(4).hex()}",
                              _fake_pi_source(usage, "did the work"))
        with patch.dict(os.environ, _clean_env(CONVEYOR_PI_BIN=str(fake_pi)), clear=True):
            return asyncio.run(PiExecutor(self.settings).run(
                role=role, prompt="do it", worktree_path=str(self.wt),
                timeout_seconds=30, task_id=task_id))


class NormalizeUsageTests(unittest.TestCase):
    def test_only_real_non_negative_int_counts_survive(self) -> None:
        self.assertEqual(ags._normalize_usage({"input_tokens": 10, "output_tokens": 2,
                                               "cached_input_tokens": 3}), {
            "input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 3})
        # Zero is a real count and is kept; junk is dropped, never coerced.
        self.assertEqual(ags._normalize_usage({"input_tokens": 0, "output_tokens": "5",
                                               "cached_input_tokens": True}), {"input_tokens": 0})
        self.assertEqual(ags._normalize_usage({"input_tokens": -1}), {})
        self.assertEqual(ags._normalize_usage({"input_tokens": 1.5}), {})
        self.assertEqual(ags._normalize_usage(None), {})
        self.assertEqual(ags._normalize_usage("nope"), {})

    def test_empty_usage_shape_is_stable(self) -> None:
        self.assertEqual(ags.empty_usage(), {
            "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0,
            "runs_with_usage": 0, "runs_without_usage": 0, "runs_before_recording": 0,
        })


class TaskUsagePersistenceTests(_UsageCase):
    def test_pi_run_persists_usage_for_the_task(self) -> None:
        task = self._new_task()
        result = self._run_pi(str(task["id"]), ANALYST,
                              {"input": 100, "output": 10, "cacheRead": 20})
        self.assertTrue(result.success, result.error)

        usage = ags.get_task(self.settings, str(task["id"]))["usage"]
        # cacheRead folds into input_tokens, exactly like _parse_pi_stream reports.
        self.assertEqual(usage, {
            "input_tokens": 120, "output_tokens": 10, "cached_input_tokens": 20,
            "runs_with_usage": 1, "runs_without_usage": 0, "runs_before_recording": 0,
        })
        rows = ags.list_execution_usage(self.settings, task_id=str(task["id"]))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["role"], "analyst")
        self.assertEqual(row["executor"], "pi")
        self.assertEqual(row["input_tokens"], 120)
        self.assertEqual(row["output_tokens"], 10)
        self.assertEqual(row["cached_input_tokens"], 20)
        self.assertEqual(row["execution_id"], result.execution_id)

    def test_executor_without_usage_stores_nothing(self) -> None:
        task = self._new_task()
        fake_claude = _write_fake(self.tmp / "fake-claude-no-usage", _fake_claude_source())
        with patch.dict(os.environ, _clean_env(CONVEYOR_CLAUDE_BIN=str(fake_claude)), clear=True):
            result = asyncio.run(ags.ClaudeExecutor(self.settings).run(
                role=CLAUDE_ANALYST, prompt="analyze", worktree_path=str(self.wt),
                timeout_seconds=30, task_id=str(task["id"])))
        self.assertTrue(result.success, result.error)
        self.assertNotIn("usage", result.evidence.get("meta", {}))

        # No row, no invented numbers: the run is counted as "without usage".
        self.assertEqual(ags.list_execution_usage(self.settings, task_id=str(task["id"])), [])
        self.assertEqual(ags.get_task(self.settings, str(task["id"]))["usage"], {
            "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0,
            "runs_with_usage": 0, "runs_without_usage": 1, "runs_before_recording": 0,
        })

    def test_usage_accumulates_across_runs_of_one_task(self) -> None:
        task = self._new_task()
        task_id = str(task["id"])
        self.assertTrue(self._run_pi(task_id, ANALYST,
                                     {"input": 100, "output": 10, "cacheRead": 20}).success)
        self.assertTrue(self._run_pi(task_id, IMPLEMENTER,
                                     {"input": 5, "output": 7, "cacheRead": 0}).success)
        # A run with no usage neither adds tokens nor hides the runs that did.
        fake_claude = _write_fake(self.tmp / "fake-claude-third", _fake_claude_source())
        with patch.dict(os.environ, _clean_env(CONVEYOR_CLAUDE_BIN=str(fake_claude)), clear=True):
            third = asyncio.run(ags.ClaudeExecutor(self.settings).run(
                role=CLAUDE_ANALYST, prompt="analyze", worktree_path=str(self.wt),
                timeout_seconds=30, task_id=task_id))
        self.assertTrue(third.success, third.error)

        self.assertEqual(ags.get_task(self.settings, task_id)["usage"], {
            "input_tokens": 125, "output_tokens": 17, "cached_input_tokens": 20,
            "runs_with_usage": 2, "runs_without_usage": 1, "runs_before_recording": 0,
        })

    def test_operator_validation_runs_are_not_usage_unknown(self) -> None:
        # Operator validation commands are shell checks, not model runs: they must
        # not make a task look like it has runs whose cost we could not read.
        task = self._new_task()
        conn = ags._connect(self.settings)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO supervisor_executions
                   (execution_id, task_id, role, executor, status, started_at)
                   VALUES ('ex-val-1', ?, 'validator', 'operator-validation', 'finished', ?)""",
                (str(task["id"]), ags._utc_now()),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(ags.get_task(self.settings, str(task["id"]))["usage"],
                         ags.empty_usage())

    def test_task_without_runs_reports_zero_shape(self) -> None:
        task = self._new_task()
        self.assertEqual(ags.get_task(self.settings, str(task["id"]))["usage"],
                         ags.empty_usage())
        self.assertEqual(ags.task_usage(self.settings, ""), ags.empty_usage())

    def test_tasks_usage_covers_every_requested_task(self) -> None:
        first = self._new_task()
        self.assertTrue(self._run_pi(str(first["id"]), ANALYST,
                                     {"input": 30, "output": 4, "cacheRead": 0}).success)
        second = self._new_task()
        per_task = ags.tasks_usage(self.settings, [str(first["id"]), str(second["id"])])
        self.assertEqual(per_task[str(first["id"])]["input_tokens"], 30)
        self.assertEqual(per_task[str(first["id"])]["runs_with_usage"], 1)
        self.assertEqual(per_task[str(second["id"])], ags.empty_usage())
        self.assertEqual(ags.tasks_usage(self.settings, []), {})


class GoalUsageTests(_UsageCase):
    def test_goal_usage_sums_the_tasks_of_its_rounds(self) -> None:
        goal = self._create_goal()
        first = self._new_task()
        self.assertTrue(self._run_pi(str(first["id"]), ANALYST,
                                     {"input": 100, "output": 10, "cacheRead": 20}).success)
        second = self._new_task()
        self.assertTrue(self._run_pi(str(second["id"]), IMPLEMENTER,
                                     {"input": 5, "output": 7, "cacheRead": 0}).success)

        iterations = [
            {"n": 1, "task_id": str(first["id"])},
            {"n": 2, "task_id": str(second["id"])},
        ]
        with patch.object(goals, "_iterations", lambda *_a, **_kw: iterations):
            enriched = goals.get_goal(self.settings, str(goal["id"]))
            listed = {g["id"]: g for g in goals.list_goals(self.settings)}[str(goal["id"])]

        expected = {
            "input_tokens": 125, "output_tokens": 17, "cached_input_tokens": 20,
            "runs_with_usage": 2, "runs_without_usage": 0, "runs_before_recording": 0,
        }
        self.assertEqual(enriched["usage"], expected)
        self.assertEqual(listed["usage"], expected)

    def test_goal_without_rounds_reports_zero_usage(self) -> None:
        goal = self._create_goal()
        self.assertEqual(goal["usage"], ags.empty_usage())
        self.assertEqual(goals.get_goal(self.settings, str(goal["id"]))["usage"],
                         ags.empty_usage())


class UsageRecordingSinceTests(_UsageCase):
    """The recording stamp splits "ran before recording" from "stayed silent"."""

    def _stamp(self, settings: Settings | None = None) -> str:
        conn = ags._connect(settings or self.settings)
        try:
            row = conn.execute(
                "SELECT value FROM supervisor_meta WHERE key = ?",
                (ags._USAGE_RECORDING_SINCE_KEY,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        return str(row[0])

    def _insert_execution(self, task_id: str, execution_id: str, started_at: str, *,
                          usage: tuple[int, int, int] | None = None,
                          executor: str = "pi") -> None:
        conn = ags._connect(self.settings)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO supervisor_executions
                   (execution_id, task_id, role, executor, status, started_at, finished_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (execution_id, task_id, "analyst", executor, "finished",
                 started_at, started_at),
            )
            if usage is not None:
                conn.execute(
                    """INSERT INTO supervisor_execution_usage
                       (execution_id, task_id, role, executor, model,
                        input_tokens, output_tokens, cached_input_tokens, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (execution_id, task_id, "analyst", executor, "test-model",
                     usage[0], usage[1], usage[2], started_at),
                )
            conn.commit()
        finally:
            conn.close()

    def _around_stamp(self, settings: Settings | None = None) -> tuple[str, str, str]:
        stamp = datetime.fromisoformat(self._stamp(settings))
        return ((stamp - timedelta(hours=2)).isoformat(),
                self._stamp(settings),
                (stamp + timedelta(hours=2)).isoformat())

    def test_stamp_is_written_once_and_never_rewritten(self) -> None:
        self._new_task()
        first = self._stamp()
        self.assertTrue(first)
        # A database that is already at this version still stamps on first open,
        # and a second stamping call keeps the original value.
        conn = ags._connect(self.settings)
        try:
            ags._stamp_usage_recording_since(conn, own_transaction=True)
            ags._stamp_usage_recording_since(conn, own_transaction=True)
        finally:
            conn.close()
        self.assertEqual(self._stamp(), first)

    def test_already_migrated_database_without_the_stamp_gets_one(self) -> None:
        # A v4 database created before the meta table existed: the next open
        # recreates the table and stamps it, without redoing the migration.
        other = _make_settings(self.tmp / "upgrade")
        self.assertEqual(ags.tasks_usage(other, []), {})
        first = self._stamp(other)
        self.assertTrue(first)
        conn = ags._connect(other)
        try:
            conn.execute("DROP TABLE supervisor_meta")
            conn.execute("PRAGMA user_version = 4")
            conn.commit()
        finally:
            conn.close()
        ags._INITIALIZED_DBS.clear()
        self.assertEqual(ags.tasks_usage(other, []), {})
        stamped = self._stamp(other)
        self.assertTrue(stamped)
        ags._INITIALIZED_DBS.clear()
        self.assertEqual(ags.tasks_usage(other, []), {})
        self.assertEqual(self._stamp(other), stamped)

    def test_unknown_runs_split_by_start_time(self) -> None:
        task = self._new_task()
        task_id = str(task["id"])
        before, at_stamp, after = self._around_stamp()
        self._insert_execution(task_id, "ex-old-1", before)
        self._insert_execution(task_id, "ex-old-2", before)
        # Exactly at the stamp counts as "recording was already on".
        self._insert_execution(task_id, "ex-at-stamp", at_stamp)
        self._insert_execution(task_id, "ex-new-1", after)
        self._insert_execution(task_id, "ex-old-reported", before, usage=(100, 10, 20))
        # Operator validation is a shell check, never a usage question.
        self._insert_execution(task_id, "ex-val", before, executor="operator-validation")
        self.assertEqual(ags.get_task(self.settings, task_id)["usage"], {
            "input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 20,
            "runs_with_usage": 1, "runs_without_usage": 2,
            "runs_before_recording": 2,
        })

    def test_goal_of_only_pre_recording_runs(self) -> None:
        goal = self._create_goal()
        first = self._new_task()
        second = self._new_task()
        before, _at, _after = self._around_stamp()
        self._insert_execution(str(first["id"]), "ex-hist-1", before)
        self._insert_execution(str(first["id"]), "ex-hist-2", before)
        self._insert_execution(str(second["id"]), "ex-hist-3", before)
        iterations = [{"n": 1, "task_id": str(first["id"])},
                      {"n": 2, "task_id": str(second["id"])}]
        with patch.object(goals, "_iterations", lambda *_a, **_kw: iterations):
            detail = goals.get_goal(self.settings, str(goal["id"]))
        self.assertEqual(detail["usage"], {
            "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0,
            "runs_with_usage": 0, "runs_without_usage": 0,
            "runs_before_recording": 3,
        })
        text = goals.build_finish_report_text(self.settings, detail)
        self.assertIn("这个目标是在开始记录用量之前跑的", text)
        self.assertNotIn("token", text)
        # The silent-executor note stays reserved for runs that could report.
        self.assertNotIn("部分执行者没有报告用量", text)

    def test_goal_mixes_pre_recording_and_reported_rounds(self) -> None:
        goal = self._create_goal()
        task = self._new_task()
        task_id = str(task["id"])
        before, _at, after = self._around_stamp()
        self._insert_execution(task_id, "ex-hist", before)
        self._insert_execution(task_id, "ex-reported", after, usage=(30000, 4000, 0))
        with patch.object(goals, "_iterations",
                          lambda *_a, **_kw: [{"n": 1, "task_id": task_id}]):
            detail = goals.get_goal(self.settings, str(goal["id"]))
        self.assertEqual(detail["usage"]["runs_with_usage"], 1)
        self.assertEqual(detail["usage"]["runs_before_recording"], 1)
        text = goals.build_finish_report_text(self.settings, detail)
        self.assertIn("用了约 3.4 万 token（更早的轮次还没开始记录用量）", text)


class FormatTokensTests(unittest.TestCase):
    """Owner-facing token copy: same rounding the TS twin implements."""

    def test_boundaries(self) -> None:
        self.assertEqual(ags.format_tokens(0), "0 token")
        self.assertEqual(ags.format_tokens(8549), "约 8,500 token")
        # 9999 rounds up to 10,000, so it is shown in 万, not as "10,000 token".
        self.assertEqual(ags.format_tokens(9999), "约 1 万 token")
        self.assertEqual(ags.format_tokens(10000), "约 1 万 token")
        self.assertEqual(ags.format_tokens(34000), "约 3.4 万 token")
        self.assertEqual(ags.format_tokens(120000), "约 12 万 token")
        self.assertEqual(ags.format_tokens(1234567), "约 123 万 token")

    def test_rounding_rules(self) -> None:
        # Below 10,000 after rounding: nearest hundred, half up, thousands separator.
        self.assertEqual(ags.format_tokens(1), "不到 100 token")
        self.assertEqual(ags.format_tokens(49), "不到 100 token")
        self.assertEqual(ags.format_tokens(50), "约 100 token")
        self.assertEqual(ags.format_tokens(8500), "约 8,500 token")
        self.assertEqual(ags.format_tokens(8550), "约 8,600 token")
        # From 10,000: 万 with one decimal under 10 万, whole 万 above it.
        # Rounding happens first: 99999 rounds to 10 万, so it leaves the decimals.
        self.assertEqual(ags.format_tokens(99999), "约 10 万 token")
        self.assertEqual(ags.format_tokens(100000), "约 10 万 token")
        self.assertEqual(ags.format_tokens(125000), "约 13 万 token")
        # A trailing ".0" is dropped: 20000 is 2 万, not 2.0 万.
        self.assertEqual(ags.format_tokens(20000), "约 2 万 token")

    def test_junk_and_negatives_are_zero(self) -> None:
        self.assertEqual(ags.format_tokens(-5), "0 token")
        self.assertEqual(ags.format_tokens(None), "0 token")
        self.assertEqual(ags.format_tokens("nope"), "0 token")


class UsageSummaryPhraseTests(unittest.TestCase):
    def test_phrase_with_and_without_partial_reporting(self) -> None:
        self.assertEqual(
            ags.usage_summary_phrase({"input_tokens": 30000, "output_tokens": 4000,
                                      "runs_with_usage": 3, "runs_without_usage": 0}),
            "用了约 3.4 万 token")
        self.assertEqual(
            ags.usage_summary_phrase({"input_tokens": 30000, "output_tokens": 4000,
                                      "runs_with_usage": 3, "runs_without_usage": 1}),
            "用了约 3.4 万 token（部分执行者没有报告用量）")

    def test_no_usage_at_all_says_nothing(self) -> None:
        # Nobody reported usage is not the same as "cost nothing": say nothing.
        self.assertEqual(ags.usage_summary_phrase(ags.empty_usage()), "")
        self.assertEqual(ags.usage_summary_phrase({"input_tokens": 10, "output_tokens": 1,
                                                   "runs_with_usage": 0,
                                                   "runs_without_usage": 2}), "")
        self.assertEqual(ags.usage_summary_phrase(None), "")

    def test_runs_that_predate_recording_are_not_missing_reports(self) -> None:
        # A goal whose runs all started before recording began has no number to
        # show: say that instead of blaming its executors (or printing a zero).
        self.assertEqual(
            ags.usage_summary_phrase({"runs_with_usage": 0, "runs_without_usage": 0,
                                      "runs_before_recording": 3}),
            "这个目标是在开始记录用量之前跑的")
        # Runs that did record keep their number and gain the historical note.
        self.assertEqual(
            ags.usage_summary_phrase({"input_tokens": 30000, "output_tokens": 4000,
                                      "runs_with_usage": 3, "runs_without_usage": 0,
                                      "runs_before_recording": 2}),
            "用了约 3.4 万 token（更早的轮次还没开始记录用量）")
        # A genuinely silent executor still wins over the historical note.
        self.assertEqual(
            ags.usage_summary_phrase({"input_tokens": 30000, "output_tokens": 4000,
                                      "runs_with_usage": 3, "runs_without_usage": 1,
                                      "runs_before_recording": 2}),
            "用了约 3.4 万 token（部分执行者没有报告用量）")

    def test_older_usage_dicts_without_the_new_field_still_work(self) -> None:
        # A payload from before the field existed reads as "nothing historical".
        self.assertEqual(
            ags.usage_summary_phrase({"input_tokens": 30000, "output_tokens": 4000,
                                      "runs_with_usage": 3, "runs_without_usage": 0}),
            "用了约 3.4 万 token")

    def test_cached_input_is_not_counted_twice(self) -> None:
        # cached_input_tokens already live inside input_tokens: total is in + out.
        self.assertEqual(ags.usage_total_tokens({"input_tokens": 8000, "output_tokens": 549,
                                                 "cached_input_tokens": 6000}), 8549)


class UsageCopyTests(_UsageCase):
    """The report / progress copy shows the goal's usage, or nothing at all."""

    def _report(self, goal: dict[str, object]) -> str:
        return goals.build_finish_report_text(self.settings, goal)

    def test_finish_report_appends_usage_and_partial_note(self) -> None:
        goal = self._create_goal()
        goal["usage"] = {"input_tokens": 30000, "output_tokens": 4000,
                         "cached_input_tokens": 0, "runs_with_usage": 3,
                         "runs_without_usage": 1}
        text = self._report(goal)
        self.assertIn("用了约 3.4 万 token（部分执行者没有报告用量）", text)

    def test_finish_report_includes_cached_hit_percentage(self) -> None:
        goal = self._create_goal()
        goal["usage"] = {"input_tokens": 30000, "output_tokens": 4000,
                         "cached_input_tokens": 10000, "runs_with_usage": 3,
                         "runs_without_usage": 0}
        # 10000 cached / 34000 total = 29.4% -> 29%
        text = self._report(goal)
        self.assertIn("用了约 3.4 万 token（约 29% 命中缓存）", text)

    def test_finish_report_without_partial_reporting_has_no_note(self) -> None:
        goal = self._create_goal()
        goal["usage"] = {"input_tokens": 30000, "output_tokens": 4000,
                         "cached_input_tokens": 0, "runs_with_usage": 3,
                         "runs_without_usage": 0}
        text = self._report(goal)
        self.assertIn("用了约 3.4 万 token", text)
        self.assertNotIn("部分执行者没有报告用量", text)

    def test_finish_report_notes_rounds_that_predate_recording(self) -> None:
        # Some rounds recorded, older ones simply ran before recording existed.
        goal = self._create_goal()
        goal["usage"] = {"input_tokens": 30000, "output_tokens": 4000,
                         "cached_input_tokens": 0, "runs_with_usage": 3,
                         "runs_without_usage": 0, "runs_before_recording": 2}
        text = self._report(goal)
        self.assertIn("用了约 3.4 万 token（更早的轮次还没开始记录用量）", text)
        self.assertNotIn("部分执行者没有报告用量", text)

    def test_finish_report_for_a_goal_that_predates_recording(self) -> None:
        # Nothing was ever recorded, so there is no token amount to print at all.
        goal = self._create_goal()
        goal["usage"] = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0,
                         "runs_with_usage": 0, "runs_without_usage": 0,
                         "runs_before_recording": 3}
        text = self._report(goal)
        self.assertIn("这个目标是在开始记录用量之前跑的", text)
        self.assertNotIn("token", text)

    def test_finish_report_without_any_usage_says_nothing(self) -> None:
        goal = self._create_goal()
        goal["usage"] = ags.empty_usage()
        self.assertNotIn("token", self._report(goal))

    def test_finish_report_reads_usage_when_the_goal_dict_lacks_it(self) -> None:
        # Older callers hand over a goal without "usage": the report still finds
        # the real numbers instead of printing nothing (or a fabricated zero).
        goal = self._create_goal()
        goal.pop("usage", None)  # 老调用方交过来的目标不带 usage
        task = self._new_task()
        self.assertTrue(self._run_pi(str(task["id"]), ANALYST,
                                     {"input": 30000, "output": 4000,
                                      "cacheRead": 0}).success)
        with patch.object(goals, "_iterations",
                          lambda *_a, **_kw: [{"n": 1, "task_id": str(task["id"])}]):
            text = self._report(goal)
        self.assertIn("用了约 3.4 万 token", text)

    def _progress_reply(self, detail: dict[str, object]) -> str:
        with patch.object(goals, "list_goals", lambda *_a, **_kw: [detail]), \
                patch.object(goals, "get_goal", lambda *_a, **_kw: detail):
            text, _card = chat_intents._handle_progress_intent(
                self.settings, "usage-progress-session", "目标进度怎么样")
        return text

    def test_progress_reply_appends_usage_and_partial_note(self) -> None:
        goal = self._create_goal()
        detail = dict(goal)
        detail.update({"state": "running", "rounds_total": 3, "rounds_passed": 2,
                       "usage": {"input_tokens": 30000, "output_tokens": 4000,
                                  "cached_input_tokens": 0, "runs_with_usage": 3,
                                  "runs_without_usage": 1}})
        text = self._progress_reply(detail)
        self.assertIn("已跑 3 轮 / 通过 2 轮", text)
        self.assertIn("用了约 3.4 万 token（部分执行者没有报告用量）", text)

    def test_progress_reply_without_usage_says_nothing(self) -> None:
        goal = self._create_goal()
        detail = dict(goal)
        detail.update({"state": "running", "rounds_total": 1, "rounds_passed": 0,
                       "usage": ags.empty_usage()})
        text = self._progress_reply(detail)
        self.assertIn("已跑 1 轮 / 通过 0 轮", text)
        self.assertNotIn("token", text)

    def test_progress_reply_for_a_goal_that_predates_recording(self) -> None:
        goal = self._create_goal()
        detail = dict(goal)
        detail.update({"state": "running", "rounds_total": 3, "rounds_passed": 1,
                       "usage": {"input_tokens": 0, "output_tokens": 0,
                                  "cached_input_tokens": 0, "runs_with_usage": 0,
                                  "runs_without_usage": 0, "runs_before_recording": 3}})
        text = self._progress_reply(detail)
        self.assertIn("已跑 3 轮 / 通过 1 轮", text)
        self.assertIn("这个目标是在开始记录用量之前跑的", text)
        self.assertNotIn("token", text)



if __name__ == "__main__":
    unittest.main()
