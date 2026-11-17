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
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("CONVEYOR_AGENT_SUPERVISOR_ENABLED", "true")

from conveyor.agents import agent_supervisor as ags
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
            "runs_with_usage": 0, "runs_without_usage": 0,
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
            "runs_with_usage": 1, "runs_without_usage": 0,
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
            "runs_with_usage": 0, "runs_without_usage": 1,
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
            "runs_with_usage": 2, "runs_without_usage": 1,
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
    def _create_goal(self) -> dict[str, object]:
        repo, _created = svc.register_repository(self.settings, path=str(self.repo))
        wt = svc.create_managed_worktree(self.settings, repo["id"], branch="feat/usage")
        return goals.create_goal(None, self.settings, {
            "outcome": "在 managed worktree 里新增 feature.txt，内容为 hello-usage，并干净提交",
            "repo_id": repo["id"],
            "worktree_id": wt["id"],
        })

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
            "runs_with_usage": 2, "runs_without_usage": 0,
        }
        self.assertEqual(enriched["usage"], expected)
        self.assertEqual(listed["usage"], expected)

    def test_goal_without_rounds_reports_zero_usage(self) -> None:
        goal = self._create_goal()
        self.assertEqual(goal["usage"], ags.empty_usage())
        self.assertEqual(goals.get_goal(self.settings, str(goal["id"]))["usage"],
                         ags.empty_usage())


if __name__ == "__main__":
    unittest.main()
