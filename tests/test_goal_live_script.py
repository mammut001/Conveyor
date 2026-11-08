"""tests/test_goal_live_script.py — scripts/goal_live.py（目标闭环实测）.

- Preflight end to end inside a disposable --root with fake executors: no real
  model CLI, no network, no task or goal is ever created.
- Argument validation: Chinese errors, exit code 2 for bad arguments, 1 for an
  unusable --root (non-empty, a file, inside the real data root).
- Live path with scripted fake executors (`agent_supervisor.get_executor`
  patched) and a fake chat evaluator: the real supervisor flow, the real
  unittest check in the managed worktree and the real goal loop reach
  `achieved` in exactly 2 rounds. A timeout run stops the goal and cancels the
  running task.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_supervisor as ags  # noqa: E402
import supervisor_goals as goals  # noqa: E402
import supervisor_validation as sval  # noqa: E402
from scripts import goal_live  # noqa: E402

_ENV_KEYS = (
    "CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTATION_EXECUTOR",
    "CONVEYOR_SUPERVISOR_IMPLEMENTATION_EXECUTOR",
    *(
        f"CONVEYOR_AGENT_SUPERVISOR_{role}_{key}"
        for role in ("COORDINATOR", "ANALYST", "IMPLEMENTER", "REVIEWER")
        for key in ("EXECUTOR", "MODEL", "LOCATION")
    ),
    "CODEX_MEMORY_ROOT",
    "CODEX_TASK_ROOT",
    "CONVEYOR_MAX_WORKTREES_BYTES",
)

CALC_PARTIAL = '''def add(a, b):
    return a + b


def multiply(a, b):
    return a * b


def divide(a, b):
    return a / b
'''

CALC_FULL = '''def add(a, b):
    return a + b


def multiply(a, b):
    return a * b


def divide(a, b):
    if b == 0:
        raise ValueError("除数不能为 0")
    return a / b
'''

HAS_PYTHON3 = shutil.which("python3") is not None


def _git(wt: str, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
         "-c", "user.name=Fake Implementer", "-c", "user.email=fake@localhost",
         "-C", wt, *args],
        capture_output=True, check=False, timeout=30,
    )


class FakeExecutor:
    """One scripted stand-in for every executor name (analyst / implementer /
    reviewer / coordinator chat / goal evaluator). Never spawns a model CLI."""

    def __init__(self, name: str, *, available: bool = True, reason: str = "没有安装（测试）",
                 hang_implementer: bool = False) -> None:
        self.name = name
        self._available = available
        self._reason = reason
        self.hang_implementer = hang_implementer
        self.calls: list[str] = []
        self.evaluator_calls = 0
        self.implementer_prompts: list[str] = []

    def available(self) -> tuple[bool, str]:
        return (True, "ok") if self._available else (False, self._reason)

    async def cancel(self, execution_id: str) -> bool:
        return False

    async def run(self, *, role: Any, prompt: str, worktree_path: str, timeout_seconds: int,
                  task_id: str = "", context: Any = None) -> ags.ExecutionResult:
        role_name = getattr(role, "name", "")
        self.calls.append(role_name)
        await asyncio.sleep(0.01)
        if role_name == "coordinator":
            output = self._evaluate() if "目标评估器" in prompt else "工作说明：实现 calc.py 的三个函数。"
        elif role_name == "analyst":
            output = "计划：在 calc.py 里实现 add、multiply、divide，运行 tests/ 下的单元测试。"
        elif role_name == "implementer":
            self.implementer_prompts.append(prompt)
            if self.hang_implementer:
                await asyncio.sleep(3600)
            full = "的第 2 轮" in prompt or "的第 3 轮" in prompt
            Path(worktree_path, "calc.py").write_text(CALC_FULL if full else CALC_PARTIAL, encoding="utf-8")
            _git(worktree_path, "add", "calc.py")
            _git(worktree_path, "commit", "--no-verify", "-m", "实现 calc")
            output = "改了 calc.py。"
        elif role_name == "reviewer":
            ident = ags.target_identity(worktree_path)
            output = json.dumps({
                "passed": True,
                "target_sha": ident["sha"],
                "target_fingerprint": ident["fingerprint"],
                "issues": [],
                "checks": [{"name": "unittest", "command": "python3 -m unittest discover -s tests",
                            "passed": True, "output": "ok"}],
                "summary": "看过了。",
            }, ensure_ascii=False)
        else:
            output = "ok"
        return ags.ExecutionResult(success=True, output=output,
                                   started_at=ags._utc_now(), finished_at=ags._utc_now())

    def _evaluate(self) -> str:
        self.evaluator_calls += 1
        if self.evaluator_calls == 1:
            payload = {
                "decision": "continue",
                "reason": "divide 除以 0 时还没有抛出 ValueError",
                "next_prompt": "让 divide 在除数为 0 时抛出 ValueError，其余保持不变。",
                "question": "",
                "summary": "add 和 multiply 已经通过，divide 还差除以 0 的处理。",
            }
        else:
            payload = {
                "decision": "achieved",
                "reason": "全部单元测试通过",
                "next_prompt": "",
                "question": "",
                "summary": "三个函数都写好了，测试全部通过。",
            }
        return json.dumps(payload, ensure_ascii=False)


class _GoalLiveTestBase(unittest.TestCase):
    def setUp(self) -> None:
        env_patch = patch.dict(os.environ, {})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        # Keep the forbidden-root guard away from the developer's real ~/.codex.
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-goal-live-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        os.environ["CODEX_MEMORY_ROOT"] = str(self.tmp / "real-memory")

        self.fake = FakeExecutor("fake")
        self.unavailable: dict[str, FakeExecutor] = {}
        self.requested: list[str] = []
        exec_patch = patch.object(ags, "get_executor", self._get_executor)
        exec_patch.start()
        self.addCleanup(exec_patch.stop)

    def _get_executor(self, _settings: Any, name: str) -> Any:
        self.requested.append(name)
        return self.unavailable.get(name, self.fake)

    def _run_main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = goal_live.main(argv)
        return rc, out.getvalue(), err.getvalue()


class PreflightTests(_GoalLiveTestBase):
    def test_preflight_end_to_end_without_model_spend(self) -> None:
        self.unavailable["antigravity"] = FakeExecutor("antigravity", available=False,
                                                       reason="没有安装 Antigravity（测试）")
        root = self.tmp / "run" / "pf"
        rc, out, _err = self._run_main(["--root", str(root)])
        self.assertEqual(rc, 0, out)
        report = json.loads(out)

        self.assertEqual(report["status"], "PREFLIGHT_PASS")
        self.assertFalse(report["run_real_models_optin"])
        self.assertTrue(report["ready_for_live"], report["blockers"])
        self.assertEqual(report["root"], str(root))

        # Kata repo with a failing unittest, committed.
        repo = Path(report["repo_path"])
        self.assertEqual(repo, root / "repo")
        self.assertTrue((repo / "calc.py").is_file())
        self.assertTrue((repo / "tests" / "test_calc.py").is_file())
        self.assertEqual(len(report["base_sha"]), 40)

        # Managed worktree under the isolated task root.
        wt = Path(report["worktree_path"])
        self.assertTrue(wt.is_dir())
        self.assertTrue(str(wt).startswith(str(root / "tasks" / "worktrees" / "supervisor")))
        self.assertEqual(report["branch"], "goal-calc")

        # Detected unittest check is the stored per-repo selection.
        self.assertEqual(report["validation"]["check_id"], "unittest")
        self.assertEqual(report["validation"]["source"], "repo")
        settings = goal_live.build_isolated_settings(root)
        stored = sval.get_repo_validation(settings, report["repo_id"])
        self.assertEqual(stored["selected"], ["unittest"])
        self.assertEqual(stored["effective"][0]["argv"][:3], ["python3", "-m", "unittest"])
        if HAS_PYTHON3:
            self.assertTrue(report["baseline_check"]["ran"])
            self.assertTrue(report["baseline_check"]["fails_as_expected"])

        # Executor availability is reported, with the Chinese reason.
        by_name = {e["name"]: e for e in report["executors"]}
        self.assertFalse(by_name["antigravity"]["available"])
        self.assertIn("Antigravity", by_name["antigravity"]["reason"])
        self.assertTrue(by_name["pi"]["available"])
        self.assertEqual(set(report["roles"]), {"analyst", "implementer", "reviewer"})
        self.assertTrue(report["evaluator"]["available"])

        # It says exactly what a live run would do, in Chinese.
        self.assertGreaterEqual(len(report["would_do"]), 5)
        joined = "\n".join(report["would_do"])
        self.assertIn("最多跑 3 轮", joined)
        self.assertIn("python3 -m unittest discover -s tests", joined)
        self.assertIn(goal_live.RESULT_FILENAME, joined)

        # No model was called, no task or goal was created, nothing outside --root.
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(ags.list_tasks(settings), [])
        self.assertEqual(goals.list_goals(settings), [])
        self.assertTrue((root / goal_live.PREFLIGHT_FILENAME).is_file())
        self.assertFalse((root / goal_live.RESULT_FILENAME).exists())
        self.assertFalse((self.tmp / "real-memory").exists())
        for path in (settings.codex_memory_root, settings.codex_task_root, settings.codex_workspace_root):
            self.assertTrue(str(Path(path).resolve()).startswith(str(root)))

    def test_preflight_lists_blockers_when_evaluator_unavailable(self) -> None:
        self.unavailable["chat"] = FakeExecutor("chat", available=False, reason="还没有设置对话模型（测试）")
        root = self.tmp / "pf-blocked"
        rc, out, _err = self._run_main(["--root", str(root), "--executor", "claude"])
        self.assertEqual(rc, 0)
        report = json.loads(out)
        self.assertFalse(report["ready_for_live"])
        self.assertTrue(any("评估器" in b for b in report["blockers"]))
        self.assertEqual(report["roles"]["implementer"]["executor"], "claude")
        self.assertEqual(report["roles"]["implementer"]["source"], "手动指定（--executor）")
        self.assertEqual(self.fake.calls, [])

    def test_explicit_executor_unavailable_is_a_blocker(self) -> None:
        self.unavailable["antigravity"] = FakeExecutor("antigravity", available=False,
                                                       reason="没有安装 Antigravity（测试）")
        rc, out, _err = self._run_main(["--root", str(self.tmp / "pf-agy"), "--executor", "antigravity"])
        self.assertEqual(rc, 0)
        report = json.loads(out)
        self.assertFalse(report["ready_for_live"])
        self.assertTrue(any("实现角色" in b for b in report["blockers"]))


class ArgumentValidationTests(_GoalLiveTestBase):
    def _assert_usage_error(self, argv: list[str], needle: str) -> None:
        rc, out, err = self._run_main(argv)
        self.assertEqual(rc, 2, err)
        self.assertEqual(out, "")
        self.assertIn("参数有误", err)
        self.assertIn(needle, err)
        self.assertNotIn("usage:", err)
        self.assertNotIn("error:", err)

    def test_root_is_required(self) -> None:
        self._assert_usage_error([], "--root")
        self._assert_usage_error(["--root", "   "], "--root")

    def test_unknown_executor_and_codex_are_rejected(self) -> None:
        self._assert_usage_error(["--root", str(self.tmp / "x"), "--executor", "gpt"], "不认识的执行者")
        self._assert_usage_error(["--root", str(self.tmp / "x"), "--executor", "codex"], "Codex")
        self.assertFalse((self.tmp / "x").exists())

    def test_bad_numbers_are_rejected(self) -> None:
        for value in ("0", "601", "abc"):
            self._assert_usage_error(["--root", str(self.tmp / "x"), "--timeout-minutes", value], "分钟")
        self._assert_usage_error(["--root", str(self.tmp / "x"), "--repair-rounds", "11"], "修复")
        self._assert_usage_error(["--root", str(self.tmp / "x"), "--timeout-minutes"], "要跟一个值")

    def test_unknown_argument_is_rejected(self) -> None:
        self._assert_usage_error(["--root", str(self.tmp / "x"), "--yolo"], "不认识这些参数")

    def test_valid_arguments_parse(self) -> None:
        args = goal_live.parse_args([
            "--root", "/tmp/somewhere", "--run-real-models", "--executor", "Pi",
            "--timeout-minutes", "15", "--repair-rounds", "2",
        ])
        self.assertTrue(args.run_real_models)
        self.assertEqual(args.executor, "pi")
        self.assertEqual((args.timeout_minutes, args.repair_rounds), (15, 2))
        defaults = goal_live.parse_args(["--root", "/tmp/somewhere"])
        self.assertFalse(defaults.run_real_models)
        self.assertEqual(defaults.executor, "auto")
        self.assertEqual(defaults.timeout_minutes, goal_live.DEFAULT_TIMEOUT_MINUTES)

    def test_non_empty_root_is_refused_untouched(self) -> None:
        root = self.tmp / "busy"
        root.mkdir()
        (root / "keep.txt").write_text("别动我", encoding="utf-8")
        rc, out, _err = self._run_main(["--root", str(root), "--run-real-models"])
        self.assertEqual(rc, 1)
        report = json.loads(out)
        self.assertEqual(report["status"], "ROOT_ERROR")
        self.assertIn("不是空的", report["error"])
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["keep.txt"])
        self.assertEqual(self.fake.calls, [])

    def test_root_that_is_a_file_is_refused(self) -> None:
        target = self.tmp / "a-file"
        target.write_text("x", encoding="utf-8")
        rc, out, _err = self._run_main(["--root", str(target)])
        self.assertEqual(rc, 1)
        self.assertIn("文件", json.loads(out)["error"])

    def test_root_inside_real_data_dir_is_refused(self) -> None:
        real = self.tmp / "real-memory"
        real.mkdir()
        rc, out, _err = self._run_main(["--root", str(real / "goal-live")])
        self.assertEqual(rc, 1)
        self.assertIn("数据目录", json.loads(out)["error"])
        self.assertFalse((real / "goal-live").exists())
        rc, out, _err = self._run_main(["--root", str(ROOT / "goal-live-tmp")])
        self.assertEqual(rc, 1)
        self.assertFalse((ROOT / "goal-live-tmp").exists())


@unittest.skipUnless(HAS_PYTHON3, "the detected unittest check runs `python3`")
class LiveRunTests(_GoalLiveTestBase):
    def setUp(self) -> None:
        super().setUp()
        poll_patch = patch.object(goal_live, "POLL_SECONDS", 0.02)
        poll_patch.start()
        self.addCleanup(poll_patch.stop)

    def test_live_run_reaches_achieved_in_two_rounds(self) -> None:
        root = self.tmp / "live"
        rc, out, _err = self._run_main(["--root", str(root), "--run-real-models", "--timeout-minutes", "5"])
        result_path = root / goal_live.RESULT_FILENAME
        self.assertTrue(result_path.is_file())
        summary = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(rc, 0, json.dumps(summary, ensure_ascii=False, indent=2))
        self.assertEqual(json.loads(out), summary)

        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["final_goal_state"], "achieved")
        self.assertEqual(summary["final_goal_state_label"], "已达成")
        self.assertFalse(summary["timed_out"])
        self.assertEqual(summary["max_rounds"], 3)
        self.assertEqual(summary["rounds_used"], 2)
        self.assertEqual(summary["decisions"], ["continue", "achieved"])
        round1, round2 = summary["rounds"]
        # Round 1: divide-by-zero test fails the real check every attempt → task fails.
        self.assertEqual(round1["task_state"], "failed")
        self.assertEqual(round1["task_stage"], "repair_limit_exceeded")
        # Round 2: the evaluator's next_prompt fixed it, check + review passed.
        self.assertEqual(round2["task_state"], "completed")
        self.assertEqual(summary["task_states"], {round1["task_id"]: "failed", round2["task_id"]: "completed"})
        self.assertIn("calc.py", summary["diff_files"])
        self.assertEqual(summary["flow_errors"], [])
        self.assertIn("第 2 轮达成", summary["message"])

        # Model traffic: evaluator twice; implementer 2× in round 1 (1 repair) + 1× in round 2.
        self.assertEqual(self.fake.evaluator_calls, 2)
        self.assertEqual(len(self.fake.implementer_prompts), 3)
        self.assertIn("这是目标的第 1 轮。", self.fake.implementer_prompts[0])
        self.assertIn("的第 2 轮", self.fake.implementer_prompts[2])
        self.assertEqual(
            Path(summary["worktree_path"], "calc.py").read_text(encoding="utf-8"), CALC_FULL
        )
        # The owner's own checkout of the kata is untouched.
        self.assertEqual(
            (root / "repo" / "calc.py").read_text(encoding="utf-8"), goal_live.CALC_STUB_SOURCE
        )
        self.assertFalse((self.tmp / "real-memory").exists())

    def test_explicit_executor_is_sent_as_role_override(self) -> None:
        root = self.tmp / "live-claude"
        rc, _out, _err = self._run_main(["--root", str(root), "--run-real-models", "--executor", "claude"])
        summary = json.loads((root / goal_live.RESULT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(rc, 0, summary)
        self.assertEqual(summary["final_goal_state"], "achieved")
        settings = goal_live.build_isolated_settings(root)
        goal = goals.get_goal(settings, summary["goal_id"])
        self.assertEqual(
            goal["role_overrides"],
            {r: {"executor": "claude"} for r in ("analyst", "implementer", "reviewer")},
        )
        self.assertIn("claude", self.requested)

    def test_not_ready_run_spends_nothing(self) -> None:
        self.unavailable["chat"] = FakeExecutor("chat", available=False, reason="还没有设置对话模型（测试）")
        root = self.tmp / "live-not-ready"
        rc, _out, _err = self._run_main(["--root", str(root), "--run-real-models"])
        self.assertEqual(rc, 1)
        summary = json.loads((root / goal_live.RESULT_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "NOT_READY")
        self.assertTrue(any("评估器" in b for b in summary["blockers"]))
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(goals.list_goals(goal_live.build_isolated_settings(root)), [])

    def test_timeout_stops_goal_and_cancels_running_task(self) -> None:
        self.fake.hang_implementer = True
        root = goal_live.prepare_root(str(self.tmp / "live-timeout"))
        args = goal_live.parse_args(["--root", str(root), "--run-real-models"])
        summary = asyncio.run(goal_live.run_live(root, args, timeout_seconds=1.5))
        self.assertTrue(summary["timed_out"])
        self.assertEqual(summary["status"], "FAIL")
        self.assertEqual(summary["final_goal_state"], "stopped")
        self.assertEqual(summary["rounds_used"], 1)
        self.assertEqual(summary["rounds"][0]["task_state"], "cancelled")
        self.assertIn("分钟", summary["message"])
        settings = goal_live.build_isolated_settings(root)
        self.assertFalse(ags.has_active_task_for_worktree(settings, summary["worktree_path"]))


if __name__ == "__main__":
    unittest.main()
