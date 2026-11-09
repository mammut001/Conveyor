#!/usr/bin/env python3
"""scripts/goal_live.py — 目标闭环实测（opt-in）。

An opt-in acceptance harness for the Agent Supervisor goal loop
(`supervisor_goals.py`), modelled on `scripts/agent_supervisor_live.py`.

- Default = preflight only, **no model spend**: inside an isolated `--root` it
  creates a tiny git repo (a calc kata: `add` / `multiply` / `divide` with a
  failing unittest), registers it, creates a managed worktree, selects the
  server-detected `unittest` check (`supervisor_validation`), reports which
  executors are available, and prints exactly what a live run would do.
- `--run-real-models` (explicit opt-in) does the same setup, then starts a goal
  through `supervisor_goals.create_goal` with at most 3 rounds and drives
  SupervisorWorkerManager-equivalent ticks itself: claim queued tasks with
  `agent_supervisor.claim_task` + `run_supervisor_flow`, then
  `supervisor_goals.advance_goals` (single-flight, like the server worker), until
  the goal leaves `running` or the `--timeout-minutes` budget ends. It prints a
  JSON summary (rounds, task states, decisions, final goal state, diff file list)
  and writes it to `<root>/goal_live_result.json`.

Every state root (workspace, tasks/worktrees, memory/SQLite) lives under
`--root`; the owner's real data dirs are never read or written. Owner
notifications (`goal_notify`) are switched off for the isolated run.
Everything the owner sees (messages, errors) is plain Simplified Chinese.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

# Ensure the Conveyor root is importable when run as a script.
CONVEYOR_ROOT = Path(__file__).resolve().parents[1]
if str(CONVEYOR_ROOT) not in sys.path:
    sys.path.insert(0, str(CONVEYOR_ROOT))

from config import Settings  # noqa: E402
import agent_supervisor as ags  # noqa: E402
import supervisor_goals as goals  # noqa: E402
import supervisor_service as svc  # noqa: E402
import supervisor_validation as sval  # noqa: E402
from redaction import redact_obj, redact_text  # noqa: E402

log = logging.getLogger("goal_live")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXECUTOR_CHOICES = ("auto", "pool", "pi", "claude", "antigravity", "command-code")
MAX_ROUNDS = 3
DEFAULT_TIMEOUT_MINUTES = 60
MIN_TIMEOUT_MINUTES = 1
MAX_TIMEOUT_MINUTES = 600
DEFAULT_REPAIR_ROUNDS = 1
MIN_REPAIR_ROUNDS = 1
MAX_REPAIR_ROUNDS = 10
VALIDATION_TIMEOUT_SECONDS = 120
# Seconds between worker ticks (the server worker waits up to 0.5 s as well).
POLL_SECONDS = 0.5

RESULT_FILENAME = "goal_live_result.json"
PREFLIGHT_FILENAME = "goal_live_preflight.json"
VALIDATION_CHECK_ID = "unittest"
WORKTREE_BRANCH = "goal-calc"
WORKED_ROLES = ("analyst", "implementer", "reviewer")

ROLE_LABELS = {"analyst": "分析", "implementer": "实现", "reviewer": "审阅"}
EXECUTOR_LABELS = dict(svc._UI_EXPOSED_EXECUTORS)
EXECUTOR_LABELS["chat"] = "对话模型"
GOAL_STATE_LABELS = {
    "running": "进行中",
    "waiting_user": "等你回复",
    "achieved": "已达成",
    "stopped": "已停止",
    "exhausted": "轮数用完了",
    "failed": "没能完成",
}

GOAL_OUTCOME = (
    "让练习仓库里 calc.py 的 add、multiply、divide 三个函数全部写好，"
    "并让 tests/ 下的单元测试全部通过：add 返回两数之和，multiply 返回两数之积，"
    "divide 返回 a 除以 b 的结果，除数为 0 时 divide 要抛出 ValueError。改完后提交。"
)

CALC_STUB_SOURCE = '''"""一个很小的计算器练习（kata）：三个函数都还没写。"""


def add(a, b):
    raise NotImplementedError("add 还没实现")


def multiply(a, b):
    raise NotImplementedError("multiply 还没实现")


def divide(a, b):
    raise NotImplementedError("divide 还没实现")
'''

CALC_TEST_SOURCE = '''import unittest

import calc


class AddTests(unittest.TestCase):
    def test_add_integers(self):
        self.assertEqual(calc.add(2, 3), 5)

    def test_add_negative_and_float(self):
        self.assertEqual(calc.add(-1, 1), 0)
        self.assertAlmostEqual(calc.add(0.1, 0.2), 0.3)


class MultiplyTests(unittest.TestCase):
    def test_multiply(self):
        self.assertEqual(calc.multiply(4, 5), 20)
        self.assertEqual(calc.multiply(-2, 3), -6)
        self.assertEqual(calc.multiply(7, 0), 0)


class DivideTests(unittest.TestCase):
    def test_divide(self):
        self.assertEqual(calc.divide(10, 2), 5)
        self.assertAlmostEqual(calc.divide(1, 3), 1 / 3)

    def test_divide_by_zero_raises_value_error(self):
        with self.assertRaises(ValueError):
            calc.divide(1, 0)


if __name__ == "__main__":
    unittest.main()
'''

GITIGNORE_SOURCE = "__pycache__/\n*.pyc\n.DS_Store\n"

# git flags that keep the fixture independent of the operator's global git
# config (hooks, signing, editors).
_GIT_SAFE_FLAGS = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "commit.gpgsign=false",
    "-c", "tag.gpgsign=false",
    "-c", "core.editor=true",
    "-c", "user.name=Conveyor Goal Live",
    "-c", "user.email=goal-live@localhost",
)


class GoalLiveError(Exception):
    """A problem the owner must see; the message is plain Chinese."""


# ---------------------------------------------------------------------------
# Argument parsing (Chinese messages, never argparse's English errors)
# ---------------------------------------------------------------------------

class _UsageError(Exception):
    pass


class _ChineseHelpFormatter(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):  # type: ignore[override]
        return super().add_usage(usage, actions, groups, prefix="用法：")


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _translate_argparse_error(message: str) -> str:
    text = str(message or "").strip()
    m = re.match(r"the following arguments are required: (.+)", text)
    if m:
        return f"缺少必填参数：{m.group(1)}"
    m = re.match(r"unrecognized arguments: (.+)", text)
    if m:
        return f"不认识这些参数：{m.group(1)}"
    m = re.match(r"argument ([^:]+): (.+)", text, re.S)
    if m:
        name, detail = m.group(1).strip(), m.group(2).strip()
        if detail.startswith("expected one argument"):
            return f"参数 {name} 后面要跟一个值"
        if detail.startswith("ignored explicit argument"):
            return f"参数 {name} 不需要跟值"
        if _CJK_RE.search(detail):
            return f"参数 {name}：{detail}"
        return f"参数 {name} 的值不对"
    if _CJK_RE.search(text):
        return text
    return "参数不对"


class _ChineseArgumentParser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        raise _UsageError(_translate_argparse_error(message))


def _int_in_range(label: str, low: int, high: int):
    def _parse(raw: str) -> int:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(f"{label}需要是 {low} 到 {high} 之间的整数") from None
        if value < low or value > high:
            raise argparse.ArgumentTypeError(f"{label}需要是 {low} 到 {high} 之间的整数")
        return value

    return _parse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and validate arguments. Raises `_UsageError` with a Chinese message."""
    parser = _ChineseArgumentParser(
        prog="goal_live.py",
        description=(
            "目标闭环实测。默认只做准备检查，不调用任何模型、不花额度；"
            "加上 --run-real-models 才会真正跑一个最多 3 轮的目标。"
        ),
        formatter_class=_ChineseHelpFormatter,
        add_help=False,
        allow_abbrev=False,
    )
    parser._optionals.title = "参数"
    parser._positionals.title = "位置参数"
    parser.add_argument("-h", "--help", action="help", help="显示这段帮助并退出")
    parser.add_argument(
        "--root",
        default="",
        help="必填。实测用的空目录（或还不存在的目录），所有数据都放在这里面。",
    )
    parser.add_argument(
        "--run-real-models",
        action="store_true",
        help="明确同意调用真实模型（会花额度）。不加就只做准备检查。",
    )
    parser.add_argument(
        "--executor",
        default="auto",
        help="分析、实现、审阅三个角色用哪个执行者：auto（自动选）、pool、pi、claude、antigravity、command-code。",
    )
    parser.add_argument(
        "--timeout-minutes",
        type=_int_in_range("总时长（分钟）", MIN_TIMEOUT_MINUTES, MAX_TIMEOUT_MINUTES),
        default=DEFAULT_TIMEOUT_MINUTES,
        help=f"实测最长运行多少分钟，默认 {DEFAULT_TIMEOUT_MINUTES}；到时间会停止目标并取消正在跑的任务。",
    )
    parser.add_argument(
        "--repair-rounds",
        type=_int_in_range("每轮内的修复次数", MIN_REPAIR_ROUNDS, MAX_REPAIR_ROUNDS),
        default=DEFAULT_REPAIR_ROUNDS,
        help=f"每一轮任务里审阅不通过时最多再改几次，默认 {DEFAULT_REPAIR_ROUNDS}。",
    )
    args = parser.parse_args(argv)

    if not str(args.root or "").strip():
        raise _UsageError("缺少 --root：请指定一个空目录（或还不存在的目录），实测的所有数据都会放在里面")
    executor = str(args.executor or "").strip().lower()
    if executor == "codex":
        raise _UsageError("这个版本还没有接 Codex，--executor 请选 auto、pool、pi、claude、antigravity 或 command-code")
    if executor not in EXECUTOR_CHOICES:
        raise _UsageError(
            f"不认识的执行者「{args.executor}」，--executor 可选：{'、'.join(EXECUTOR_CHOICES)}"
        )
    args.executor = executor
    return args


# ---------------------------------------------------------------------------
# Isolated root + settings
# ---------------------------------------------------------------------------

def _forbidden_roots() -> list[Path]:
    """Directories the harness must never write into: the owner's real Conveyor
    data roots and the Conveyor checkout itself."""
    raw = [
        os.getenv("CODEX_MEMORY_ROOT") or "~/.codex",
        os.getenv("CODEX_TASK_ROOT") or "",
        str(CONVEYOR_ROOT),
    ]
    out: list[Path] = []
    for item in raw:
        if not item or not str(item).strip():
            continue
        try:
            out.append(Path(os.path.realpath(os.path.expanduser(str(item).strip()))))
        except (OSError, ValueError):
            continue
    return out


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def prepare_root(candidate: str) -> Path:
    """Validate `--root`: must be empty or absent, never inside real data dirs."""
    raw = str(candidate or "").strip()
    if not raw:
        raise GoalLiveError("缺少 --root：请指定一个空目录")
    root = Path(os.path.realpath(os.path.expanduser(raw)))
    for forbidden in _forbidden_roots():
        if root == forbidden or _is_within(root, forbidden):
            raise GoalLiveError(
                f"--root 不能放在平时使用的数据目录或 Conveyor 代码目录里面（{forbidden}），请换一个单独的空目录"
            )
    if root.exists():
        if not root.is_dir():
            raise GoalLiveError(f"--root 指向的是一个文件，不是目录：{root}")
        entries = sorted(e.name for e in root.iterdir())
        if entries:
            shown = "、".join(entries[:5])
            raise GoalLiveError(
                f"--root 目录不是空的（里面有 {len(entries)} 项，例如：{shown}），"
                f"为了不覆盖已有内容，请换一个空目录：{root}"
            )
    else:
        root.mkdir(parents=True, exist_ok=False)
    return root


def build_isolated_settings(
    root: Path,
    *,
    repair_rounds: int = DEFAULT_REPAIR_ROUNDS,
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
) -> Settings:
    """Settings whose every state root sits under `root`.

    Built directly (not via `config.load_settings`, which creates directories in
    the owner's real memory root). The chat evaluator still picks up the
    `CONVEYOR_CHAT_*` environment variables through `runner.chat_client`.
    """
    workspace = root / "workspace"
    task_root = root / "tasks"
    memory_root = root / "memory"
    for d in (workspace, task_root, memory_root):
        d.mkdir(parents=True, exist_ok=True)
    role_timeout = max(60, min(3600, int(timeout_minutes) * 60))
    agy_bin = (
        os.getenv("CONVEYOR_ANTIGRAVITY_BIN")
        or os.getenv("CONVEYOR_AGY_BIN")
        or "agy"
    )
    return Settings(
        telegram_bot_token="",
        telegram_allowed_user_id=0,
        codex_workspace_root=workspace,
        codex_bin="codex",
        codex_task_root=task_root,
        codex_model=None,
        codex_timeout_seconds=role_timeout,
        telegram_progress_seconds=3,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=memory_root,
        user_timezone="UTC",
        agent_supervisor_enabled=True,
        agent_supervisor_max_repair_rounds=int(repair_rounds),
        agent_supervisor_task_timeout_seconds=role_timeout,
        agent_supervisor_antigravity_bin=agy_bin,
        # Operator env specs are deliberately NOT inherited: the per-repo check
        # selected below is what the goal loop must use.
        agent_supervisor_validation_commands=(),
        agent_supervisor_validation_timeout_seconds=VALIDATION_TIMEOUT_SECONDS,
        # Never message the owner (Telegram / Feishu / Web Push) from a test run.
        goal_notify_enabled=False,
    )


# ---------------------------------------------------------------------------
# Fixture: kata repo, registration, managed worktree, validation selection
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *_GIT_SAFE_FLAGS, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def create_kata_repo(repo_dir: Path) -> str:
    """A fresh git repo with the calc stub and its failing unittest. Returns HEAD."""
    repo_dir.mkdir(parents=True, exist_ok=False)
    (repo_dir / "tests").mkdir()
    (repo_dir / ".gitignore").write_text(GITIGNORE_SOURCE, encoding="utf-8")
    (repo_dir / "calc.py").write_text(CALC_STUB_SOURCE, encoding="utf-8")
    (repo_dir / "tests" / "test_calc.py").write_text(CALC_TEST_SOURCE, encoding="utf-8")
    steps = (
        ("init", "-b", "main"),
        ("add", "."),
        ("commit", "--no-verify", "-m", "calc 练习：三个函数待实现"),
    )
    for step in steps:
        proc = _git(repo_dir, *step)
        if proc.returncode != 0:
            detail = redact_text((proc.stderr or proc.stdout or "").strip())[:300]
            raise GoalLiveError(f"建练习仓库时 git 出错（git {step[0]}）：{detail}")
    head = _git(repo_dir, "rev-parse", "HEAD")
    sha = (head.stdout or "").strip()
    if head.returncode != 0 or len(sha) not in (40, 64):
        raise GoalLiveError("建练习仓库后读不到提交编号")
    return sha


def prepare_fixture(settings: Settings, root: Path) -> dict[str, Any]:
    """Create + register the kata repo, a managed worktree, and select the
    detected `unittest` check. Raises `GoalLiveError` (Chinese) on failure."""
    repo_dir = root / "repo"
    base_sha = create_kata_repo(repo_dir)
    try:
        repo, _created = svc.register_repository(settings, path=str(repo_dir), name="calc 练习")
        wt = svc.create_managed_worktree(settings, str(repo["id"]), branch=WORKTREE_BRANCH)
    except svc.SupervisorServiceError as exc:
        raise GoalLiveError(f"登记练习仓库或新建工作目录失败：{exc.message}") from exc

    candidates = sval.detect_candidates(str(repo["path"]))
    check = next((c for c in candidates if c.get("id") == VALIDATION_CHECK_ID), None)
    if check is None:
        raise GoalLiveError("没有在练习仓库里检测到 unittest 检查，无法继续")
    try:
        validation = sval.set_repo_validation(settings, str(repo["id"]), [VALIDATION_CHECK_ID])
    except svc.SupervisorServiceError as exc:
        raise GoalLiveError(f"选择自动检查失败：{exc.message}") from exc

    return {
        "repo_id": str(repo["id"]),
        "repo_path": str(repo["path"]),
        "base_sha": base_sha,
        "worktree_id": str(wt["id"]),
        "worktree_path": str(wt["path"]),
        "branch": str(wt.get("branch") or WORKTREE_BRANCH),
        "validation": {
            "check_id": VALIDATION_CHECK_ID,
            "label": str(check.get("label") or ""),
            "argv": list(check.get("argv") or []),
            "available": bool(check.get("available")),
            "unavailable_reason": str(check.get("unavailable_reason") or ""),
            "source": str(validation.get("source") or ""),
            "effective": validation.get("effective") or [],
        },
    }


def run_baseline_check(fixture: dict[str, Any]) -> dict[str, Any]:
    """Run the selected check once in the main checkout: it must fail before any
    model touched the code (proves the kata really starts red). No model spend."""
    validation = fixture["validation"]
    argv = list(validation.get("argv") or [])
    if not validation.get("available") or not argv:
        return {
            "ran": False,
            "fails_as_expected": None,
            "message": validation.get("unavailable_reason") or "检查命令在这台机器上不可用，跳过基线检查",
        }
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            argv,
            cwd=fixture["repo_path"],
            env=env,
            capture_output=True,
            text=True,
            timeout=VALIDATION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ran": False,
            "fails_as_expected": None,
            "message": f"基线检查没能运行：{type(exc).__name__}",
        }
    fails = proc.returncode != 0
    return {
        "ran": True,
        "exit_code": proc.returncode,
        "fails_as_expected": fails,
        "message": "检查目前不通过（符合预期：三个函数都还没写）" if fails else "检查竟然已经通过了，练习仓库有问题",
    }


# ---------------------------------------------------------------------------
# Executors / roles
# ---------------------------------------------------------------------------

def _probe(settings: Settings, name: str) -> tuple[bool, str]:
    try:
        ok, reason = ags.get_executor(settings, name).available()
    except Exception as exc:  # defensive: a broken adapter must not stop the report
        return False, f"检查「{EXECUTOR_LABELS.get(name, name)}」时出错：{type(exc).__name__}"
    return bool(ok), redact_text(str(reason or ""))


def executor_report(settings: Settings) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, label in svc._UI_EXPOSED_EXECUTORS:
        ok, reason = _probe(settings, name)
        item: dict[str, Any] = {"name": name, "label": label, "available": ok}
        if not ok and reason:
            item["reason"] = reason
        out.append(item)
    return out


def role_overrides_for(executor: str) -> dict[str, dict[str, str]]:
    if executor == "auto":
        return {}
    return {role: {"executor": executor} for role in WORKED_ROLES}


def resolve_roles(settings: Settings, executor: str) -> dict[str, Any]:
    """Which executor each worked role would use, plus readiness blockers."""
    roles, sources = ags.load_roles_with_sources(settings)
    out: dict[str, Any] = {}
    blockers: list[str] = []
    for role in WORKED_ROLES:
        name = executor if executor != "auto" else roles[role].executor
        ok, reason = _probe(settings, name)
        if executor != "auto":
            source = "手动指定（--executor）"
        elif sources.get(role) == "auto":
            source = "自动选择"
        else:
            source = "服务器环境变量里的配置"
        label = EXECUTOR_LABELS.get(name, name)
        entry: dict[str, Any] = {"executor": name, "label": label, "available": ok, "source": source}
        if not ok:
            entry["reason"] = reason
            blockers.append(f"{ROLE_LABELS[role]}角色要用的「{label}」现在不能用：{reason}")
        out[role] = entry

    chat_ok, chat_reason = _probe(settings, "chat")
    evaluator: dict[str, Any] = {"executor": "chat", "label": "对话模型", "available": chat_ok}
    if not chat_ok:
        evaluator["reason"] = chat_reason
        blockers.append(f"评估器（对话模型）现在不能用：{chat_reason}")
    return {"roles": out, "evaluator": evaluator, "blockers": blockers}


def _would_do(root: Path, args: argparse.Namespace, fixture: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    roles = plan["roles"]
    executor_desc = "、".join(
        f"{ROLE_LABELS[r]}={roles[r]['label']}" for r in WORKED_ROLES
    )
    check_cmd = " ".join(fixture["validation"]["argv"])
    title = goals._title_from_outcome(GOAL_OUTCOME)
    return [
        "用一个新的空目录作为 --root，重新建同样的练习仓库和工作目录（准备检查用过的目录不能再用）",
        f"创建目标「{title}」，最多跑 {MAX_ROUNDS} 轮",
        f"每一轮派发一个任务：{executor_desc}；每轮内审阅不通过最多再改 {args.repair_rounds} 次",
        f"每次改动后自动运行检查：{check_cmd}（在工作目录里运行，结果作为验收依据）",
        "每轮结束后由对话模型（评估器）判断：达成 / 继续下一轮 / 需要你回复 / 放弃",
        f"最长运行 {args.timeout_minutes} 分钟；到时间会停止目标并取消正在跑的任务",
        "目标进入「等你回复」时脚本也会停下（脚本不会替你回答）",
        f"结果写到 {root / RESULT_FILENAME}（新目录下的同名文件）",
        "所有数据都放在 --root 目录里，不会碰你平时用的数据目录，也不会给你发通知",
    ]


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def run_preflight(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    settings = build_isolated_settings(
        root, repair_rounds=args.repair_rounds, timeout_minutes=args.timeout_minutes
    )
    fixture = prepare_fixture(settings, root)
    baseline = run_baseline_check(fixture)
    executors = executor_report(settings)
    plan = resolve_roles(settings, args.executor)

    blockers = list(plan["blockers"])
    if not fixture["validation"]["available"]:
        blockers.append(
            "自动检查命令在这台机器上不可用："
            + (fixture["validation"]["unavailable_reason"] or "找不到 python3")
        )
    if baseline.get("fails_as_expected") is False:
        blockers.append("练习仓库的检查一开始就通过了，实测没有意义")
    ready = not blockers

    return {
        "status": "PREFLIGHT_PASS",
        "run_real_models_optin": False,
        "ready_for_live": ready,
        "blockers": blockers,
        "message": (
            "准备检查通过，没有调用任何模型。确认无误后，换一个新的空目录作为 --root，并加上 --run-real-models 开始实测。"
            if ready
            else "准备检查做完了，没有调用任何模型，但现在还不能实测，原因见 blockers。"
        ),
        "root": str(root),
        "repo_path": fixture["repo_path"],
        "base_sha": fixture["base_sha"],
        "repo_id": fixture["repo_id"],
        "worktree_id": fixture["worktree_id"],
        "worktree_path": fixture["worktree_path"],
        "branch": fixture["branch"],
        "validation": fixture["validation"],
        "baseline_check": baseline,
        "executor_choice": args.executor,
        "executors": executors,
        "roles": plan["roles"],
        "evaluator": plan["evaluator"],
        "goal_outcome": GOAL_OUTCOME,
        "max_rounds": MAX_ROUNDS,
        "timeout_minutes": args.timeout_minutes,
        "repair_rounds": args.repair_rounds,
        "would_do": _would_do(root, args, fixture, plan),
    }


# ---------------------------------------------------------------------------
# Live run: SupervisorWorkerManager-equivalent driver
# ---------------------------------------------------------------------------

class GoalDriver:
    """Drives one goal the way `SupervisorWorkerManager` does on the server:
    each tick reaps finished flows, claims queued tasks (`ags.claim_task` →
    `ags.run_supervisor_flow`, at most `ags.MAX_CONCURRENT_FLOWS`), then starts a
    single-flight `supervisor_goals.advance_goals` pass in the background.

    Stops when the goal leaves `running` (and no goal pass is in flight) or when
    the time budget ends; on timeout the goal is stopped and every running flow
    is cancelled so no executor process outlives the run.
    """

    def __init__(self, settings: Settings, goal_id: str, *, timeout_seconds: float) -> None:
        self.settings = settings
        self.goal_id = goal_id
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self.flows: dict[str, asyncio.Task[Any]] = {}
        self.goals_task: asyncio.Task[None] | None = None
        self.ticks = 0
        self.claimed: list[str] = []
        self.flow_errors: list[dict[str, str]] = []
        self.goal_pass_errors: list[str] = []

    # -- tick parts --------------------------------------------------------

    def _reap(self) -> None:
        for tid, flow in list(self.flows.items()):
            if not flow.done():
                continue
            self.flows.pop(tid, None)
            if not flow.cancelled():
                exc = flow.exception()
                if exc is not None:
                    msg = redact_text(f"{type(exc).__name__}: {exc}").strip() or "supervisor flow error"
                    self.flow_errors.append({"task_id": tid, "error": msg})
                    try:
                        cur = ags.get_task(self.settings, tid)
                        if cur is not None and str(cur.get("state") or "") not in ags.TERMINAL_STATES:
                            ags.transition(self.settings, tid, "failed", stage="unexpected_error", error=msg)
                    except Exception:
                        log.warning("goal_live: could not fail task %s after flow error", tid, exc_info=True)
            self._release_lock_if_safe(tid)

    def _release_lock_if_safe(self, tid: str) -> None:
        try:
            cur = ags.get_task(self.settings, tid)
            if cur is not None and str(cur.get("state") or "") in ags.TERMINAL_STATES:
                wp = svc._canonical_internal_path(str(cur.get("worktree_path") or ""))
                if wp and ags.lock_release_safe(self.settings, wp, tid):
                    ags.release_worktree_lock(self.settings, wp, tid)
        except Exception:
            log.warning("goal_live: lock release check failed for %s", tid, exc_info=True)

    def _claim(self) -> None:
        loop = asyncio.get_running_loop()
        max_flows = max(1, int(getattr(ags, "MAX_CONCURRENT_FLOWS", 2)))
        while len(self.flows) < max_flows:
            claimed = ags.claim_task(self.settings)
            if claimed is None:
                break
            tid = str(claimed["id"])
            self.claimed.append(tid)
            self.flows[tid] = loop.create_task(
                ags.run_supervisor_flow(self.settings, tid),
                name=f"goal-live-flow-{tid[:16]}",
            )

    def _goals_pass_idle(self) -> bool:
        task = self.goals_task
        if task is None:
            return True
        if not task.done():
            return False
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                self.goal_pass_errors.append(redact_text(f"{type(exc).__name__}: {exc}"))
        self.goals_task = None
        return True

    def _start_goals_pass(self) -> None:
        if not self._goals_pass_idle():
            return
        self.goals_task = asyncio.get_running_loop().create_task(
            goals.advance_goals(None, self.settings),
            name="goal-live-advance",
        )

    def _goal_state(self) -> str:
        return str(goals._get_goal_row(self.settings, self.goal_id).get("state") or "")

    # -- main loop ---------------------------------------------------------

    async def run(self) -> dict[str, Any]:
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        timed_out = False
        finished_cleanly = False
        try:
            while True:
                self._reap()
                self._claim()
                self.ticks += 1
                if self._goal_state() != goals.GOAL_RUNNING:
                    if self._goals_pass_idle():
                        break
                else:
                    self._start_goals_pass()
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                await asyncio.sleep(POLL_SECONDS)
            finished_cleanly = True
        finally:
            await self._shutdown(stop_goal=timed_out or not finished_cleanly)
        return {
            "timed_out": timed_out,
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "ticks": self.ticks,
            "claimed_tasks": list(self.claimed),
            "flow_errors": list(self.flow_errors),
            "goal_pass_errors": list(self.goal_pass_errors),
        }

    async def _shutdown(self, *, stop_goal: bool) -> None:
        # 1. No new goal pass may dispatch another round after this point.
        task = self.goals_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.goals_task = None

        # 2. Out of time / interrupted: stop the goal (cancels its active task too).
        if stop_goal:
            try:
                if self._goal_state() in (goals.GOAL_RUNNING, goals.GOAL_WAITING_USER):
                    goals.stop_goal(None, self.settings, self.goal_id)
            except Exception:
                log.warning("goal_live: stopping goal %s failed", self.goal_id, exc_info=True)

        # 3. Cancel any flow still running (kills its process groups first).
        pending = [(tid, f) for tid, f in self.flows.items() if not f.done()]
        for tid, flow in pending:
            try:
                ags.cancel_task(self.settings, tid, reason="实测结束，取消还在跑的任务")
            except Exception:
                log.warning("goal_live: cancel_task %s failed", tid, exc_info=True)
            flow.cancel()
        if pending:
            await asyncio.gather(*(f for _tid, f in pending), return_exceptions=True)
        self._reap()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def build_summary(
    settings: Settings,
    goal_id: str,
    *,
    root: Path,
    args: argparse.Namespace,
    fixture: dict[str, Any],
    plan: dict[str, Any],
    drive: dict[str, Any],
) -> dict[str, Any]:
    detail = goals.get_goal(settings, goal_id)
    rounds: list[dict[str, Any]] = []
    task_states: dict[str, str] = {}
    for it in detail.get("iterations") or []:
        tid = str(it.get("task_id") or "")
        task = ags.get_task(settings, tid) if tid else None
        state = str((task or {}).get("state") or ("missing" if tid else ""))
        stage = str((task or {}).get("stage") or "")
        if tid:
            task_states[tid] = state
        rounds.append({
            "n": int(it.get("n") or 0),
            "task_id": tid,
            "task_state": state,
            "task_stage": stage,
            "repair_round": int((task or {}).get("repair_round") or 0),
            "decision": str(it.get("decision") or ""),
            "decision_reason": str(it.get("decision_reason") or ""),
        })

    diff_files: list[str] = []
    diff_error = ""
    try:
        diff = goals.get_goal_diff(settings, goal_id)
        diff_files = [str(f.get("path") or "") for f in (diff.get("files") or []) if f.get("path")]
        for extra in diff.get("untracked") or []:
            if extra not in diff_files:
                diff_files.append(str(extra))
    except svc.SupervisorServiceError as exc:
        diff_error = str(exc.message)

    final_state = str(detail.get("state") or "")
    label = GOAL_STATE_LABELS.get(final_state, final_state)
    used = len(rounds)
    if drive.get("timed_out"):
        message = f"到了 {args.timeout_minutes} 分钟还没结束，已停止目标（用了 {used} 轮）。"
    elif final_state == goals.GOAL_ACHIEVED:
        message = f"目标在第 {used} 轮达成。"
    elif final_state == goals.GOAL_WAITING_USER:
        message = f"目标停在「等你回复」（第 {used} 轮）：{detail.get('question') or ''}"
    else:
        message = f"目标没有达成（{label}，用了 {used} 轮）：{detail.get('stop_reason') or ''}"

    return {
        "status": "PASS" if final_state == goals.GOAL_ACHIEVED else "FAIL",
        "run_real_models_optin": True,
        "message": message,
        "goal_id": goal_id,
        "goal_title": str(detail.get("title") or ""),
        "final_goal_state": final_state,
        "final_goal_state_label": label,
        "stop_reason": str(detail.get("stop_reason") or ""),
        "question": str(detail.get("question") or ""),
        "goal_summary": str(detail.get("summary") or ""),
        "max_rounds": int(detail.get("max_iterations") or MAX_ROUNDS),
        "rounds_used": used,
        "rounds": rounds,
        "task_states": task_states,
        "decisions": [r["decision"] for r in rounds if r["decision"]],
        "diff_files": diff_files,
        "diff_error": diff_error,
        "timed_out": bool(drive.get("timed_out")),
        "elapsed_seconds": drive.get("elapsed_seconds"),
        "ticks": drive.get("ticks"),
        "flow_errors": drive.get("flow_errors") or [],
        "goal_pass_errors": drive.get("goal_pass_errors") or [],
        "timeout_minutes": args.timeout_minutes,
        "repair_rounds": args.repair_rounds,
        "executor_choice": args.executor,
        "roles": plan["roles"],
        "evaluator": plan["evaluator"],
        "root": str(root),
        "repo_path": fixture["repo_path"],
        "worktree_path": fixture["worktree_path"],
        "branch": fixture["branch"],
        "validation": fixture["validation"],
        "result_path": str(root / RESULT_FILENAME),
    }


async def run_live(
    root: Path,
    args: argparse.Namespace,
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Full live run. `timeout_seconds` overrides `--timeout-minutes` (tests)."""
    settings = build_isolated_settings(
        root, repair_rounds=args.repair_rounds, timeout_minutes=args.timeout_minutes
    )
    fixture = prepare_fixture(settings, root)
    plan = resolve_roles(settings, args.executor)
    blockers = list(plan["blockers"])
    if not fixture["validation"]["available"]:
        blockers.append(
            "自动检查命令在这台机器上不可用："
            + (fixture["validation"]["unavailable_reason"] or "找不到 python3")
        )
    if blockers:
        return {
            "status": "NOT_READY",
            "run_real_models_optin": True,
            "message": "现在还不能实测，没有调用任何模型。原因见 blockers；可以先不加 --run-real-models 跑一次准备检查。",
            "blockers": blockers,
            "executor_choice": args.executor,
            "roles": plan["roles"],
            "evaluator": plan["evaluator"],
            "root": str(root),
            "result_path": str(root / RESULT_FILENAME),
        }

    body: dict[str, Any] = {
        "outcome": GOAL_OUTCOME,
        "repo_id": fixture["repo_id"],
        "worktree_id": fixture["worktree_id"],
        "max_iterations": MAX_ROUNDS,
    }
    overrides = role_overrides_for(args.executor)
    if overrides:
        body["role_overrides"] = overrides
    try:
        goal = goals.create_goal(None, settings, body)
    except svc.SupervisorServiceError as exc:
        raise GoalLiveError(f"创建目标失败：{exc.message}") from exc

    budget = float(timeout_seconds) if timeout_seconds is not None else float(args.timeout_minutes) * 60.0
    driver = GoalDriver(settings, str(goal["id"]), timeout_seconds=budget)
    drive = await driver.run()
    return build_summary(
        settings,
        str(goal["id"]),
        root=root,
        args=args,
        fixture=fixture,
        plan=plan,
        drive=drive,
    )


# ---------------------------------------------------------------------------
# Output + entry point
# ---------------------------------------------------------------------------

def _emit(report: dict[str, Any], path: Path | None) -> dict[str, Any]:
    clean = redact_obj(report)
    text = json.dumps(clean, ensure_ascii=False, indent=2)
    if path is not None:
        try:
            path.write_text(text + "\n", encoding="utf-8")
        except OSError:
            log.warning("goal_live: could not write %s", path, exc_info=True)
    print(text)
    return clean


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except _UsageError as exc:
        print(f"参数有误：{exc}。可以用 --help 查看用法。", file=sys.stderr)
        return 2
    except SystemExit as exc:  # --help
        return int(exc.code or 0)

    try:
        root = prepare_root(args.root)
    except GoalLiveError as exc:
        _emit({"status": "ROOT_ERROR", "error": str(exc)}, None)
        return 1

    if not args.run_real_models:
        preflight_path = root / PREFLIGHT_FILENAME
        try:
            report = run_preflight(root, args)
        except GoalLiveError as exc:
            _emit({"status": "PREFLIGHT_FAIL", "error": str(exc), "root": str(root)}, preflight_path)
            return 1
        except Exception as exc:
            log.exception("goal_live: unexpected preflight error")
            _emit(
                {
                    "status": "PREFLIGHT_FAIL",
                    "error": f"准备检查时出了意外错误：{type(exc).__name__}",
                    "detail": redact_text(str(exc))[:500],
                    "root": str(root),
                },
                preflight_path,
            )
            return 1
        _emit(report, preflight_path)
        return 0

    result_path = root / RESULT_FILENAME
    try:
        report = asyncio.run(run_live(root, args))
    except GoalLiveError as exc:
        _emit({"status": "ERROR", "error": str(exc), "root": str(root)}, result_path)
        return 1
    except KeyboardInterrupt:
        _emit(
            {"status": "INTERRUPTED", "error": "实测被手动中断了（Ctrl+C），目标已停止", "root": str(root)},
            result_path,
        )
        return 130
    except Exception as exc:
        log.exception("goal_live: unexpected live error")
        _emit(
            {
                "status": "ERROR",
                "error": f"实测过程中出了意外错误：{type(exc).__name__}",
                "detail": redact_text(str(exc))[:500],
                "root": str(root),
            },
            result_path,
        )
        return 1
    clean = _emit(report, result_path)
    return 0 if clean.get("status") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
