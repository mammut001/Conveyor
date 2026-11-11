"""goal_loop.py — unattended ``/goal`` harness for the Coder agent.

A single Coder job answers once and stops. ``/goal <objective>`` wraps the
existing job queue in a durable loop so the owner can go to sleep:

    iterate -> independent verification -> decide -> iterate ...

Why a harness and not "just a longer prompt":

* The agent's own claim of success is never trusted. After every iteration the
  harness runs the owner's ``--check`` commands itself inside the job worktree.
  With no checks, "done" must be claimed on consecutive iterations.
* Progress is measured from the worktree (HEAD + dirty state + check results),
  so a loop that keeps talking but changes nothing stops after
  ``NO_PROGRESS_LIMIT`` iterations instead of burning budget all night.
* Hard budgets: wall-clock deadline, iteration cap, consecutive-failure cap.
* State is persisted after every step, so a restart resumes instead of losing
  the run (``resume_all``).
* Safety: iterations run as ordinary FIX jobs in the isolated worktree chain.
  Nothing is merged, pushed or deployed; the owner reviews the diff in the
  morning and decides with the normal apply / discard controls.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

logger = logging.getLogger("goal_loop")

DEFAULT_HOURS = 8.0
MAX_HOURS = 24.0
DEFAULT_MAX_ITERATIONS = 60
MAX_ITERATIONS_CAP = 200
NO_PROGRESS_LIMIT = 3
FAILURE_LIMIT = 3
DONE_CONFIRMATIONS = 2
CHECK_TIMEOUT_SECONDS = 900
ITERATION_TIMEOUT_SECONDS = 2 * 3600
# An iteration that starts right at the goal deadline still gets this much
# grace before the driver cancels it (so it is never cancelled instantly).
ITERATION_MIN_GRACE_SECONDS = 60.0
GOAL_DEADLINE_ERROR = "goal deadline reached"
TRANSIENT_RETRY_SECONDS = 120
MAX_CHECKS = 5
MAX_OBJECTIVE_CHARS = 4000

TERMINAL = frozenset({"done", "stopped", "timeout", "max_iterations", "stalled", "blocked", "failed"})
_TERMINAL_LABEL = {
    "done": "✅ 目标已完成（独立验收通过）",
    "stopped": "⏹ 已按你的要求停止",
    "timeout": "⏰ 到达时间上限",
    "max_iterations": "🔁 到达迭代上限",
    "stalled": "💤 连续多轮没有进展，已自动停止",
    "blocked": "🚧 助手报告被阻塞，需要你介入",
    "failed": "❌ 连续失败，已停止",
}

USAGE = (
    "用法：\n"
    "/goal <目标>  — 开启无人值守迭代（隔离 worktree 内反复：改代码 → 测试 → 找问题 → 修复）\n"
    "可选：--hours 8（时间上限）  --max 60（迭代上限）  --check \"pytest -q\"（独立验收命令，可多次）\n"
    "/goal status  — 查看进度\n"
    "/goal stop    — 停止\n"
    "不会自动合并、推送或部署；早上用「改动」查看 diff，再决定是否应用。"
)


# --------------------------------------------------------------------------
# Command parsing
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GoalCommand:
    action: str  # start | status | stop | help | invalid
    objective: str = ""
    checks: tuple[str, ...] = ()
    hours: float = DEFAULT_HOURS
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    error: str = ""


_CMD = re.compile(r"^/goal(?:@\w+)?(?:\s+(.*))?$", re.S)
_CHECK = re.compile(r"--check(?:=|\s+)(?:\"([^\"]*)\"|'([^']*)'|“([^”]*)”|‘([^’]*)’)")
_HOURS = re.compile(r"--hours(?:=|\s+)([0-9]+(?:\.[0-9]+)?)")
_MAX = re.compile(r"--max(?:=|\s+)([0-9]+)")
# iOS/macOS "Smart Punctuation" rewrites "--" into an em/en (or full-width) dash
# right before a flag name. Rewrite those back to "--" so the flags are not
# silently swallowed by the objective. A run that contains at least one smart
# dash is required, so a plain ASCII hyphen ("re-check", "2024—2025") is untouched.
_SMART_DASH = "\u2014\u2013\u2015\uff0d\u2212"  # — – ― － −
_FLAG_DASH = re.compile(rf"[-{_SMART_DASH}]*[{_SMART_DASH}][-{_SMART_DASH}]*(?=(?:check|hours|max)\b)")

_UNQUOTED_CHECK_ERROR = '--check 后面的命令需要用引号括起来，例如 --check "npm test"。'


def is_goal_command(text: str | None) -> bool:
    return bool(_CMD.match((text or "").strip()))


def parse_goal_command(text: str | None) -> GoalCommand | None:
    match = _CMD.match((text or "").strip())
    if not match:
        return None
    rest = (match.group(1) or "").strip()
    word = rest.lower()
    if not rest or word in ("help", "帮助"):
        return GoalCommand("help")
    if word in ("status", "状态"):
        return GoalCommand("status")
    if word in ("stop", "cancel", "停止", "取消"):
        return GoalCommand("stop")

    # iPhone owners type "—hours 2"; accept it as "--hours 2".
    rest = _FLAG_DASH.sub("--", rest)

    checks = tuple(next(g for g in m.groups() if g is not None).strip()
                   for m in _CHECK.finditer(rest))
    checks = tuple(c for c in checks if c)
    hours_match = _HOURS.search(rest)
    max_match = _MAX.search(rest)
    objective = _CHECK.sub(" ", rest)
    objective = _HOURS.sub(" ", objective)
    objective = _MAX.sub(" ", objective)
    objective = re.sub(r"\s+", " ", objective).strip()

    # A leftover --check means the command was not quoted; the owner would not
    # notice the acceptance check silently disappearing, so refuse it. A bad
    # --hours/--max only falls back to the default and may be part of the
    # objective text ("给 CLI 加上 --max 参数"), so those stay lenient.
    if re.search(r"--check\b", objective):
        return GoalCommand("invalid", error=_UNQUOTED_CHECK_ERROR)

    if len(objective) < 4:
        return GoalCommand("invalid", error="目标太短，请用一句话说清要达成什么。")
    if len(objective) > MAX_OBJECTIVE_CHARS:
        return GoalCommand("invalid", error=f"目标过长（最多 {MAX_OBJECTIVE_CHARS} 字）。")
    if len(checks) > MAX_CHECKS:
        return GoalCommand("invalid", error=f"最多 {MAX_CHECKS} 条 --check 验收命令。")
    hours = DEFAULT_HOURS
    if hours_match:
        hours = min(max(float(hours_match.group(1)), 0.1), MAX_HOURS)
    max_iterations = DEFAULT_MAX_ITERATIONS
    if max_match:
        max_iterations = min(max(int(max_match.group(1)), 1), MAX_ITERATIONS_CAP)
    return GoalCommand("start", objective, checks, hours, max_iterations)


# --------------------------------------------------------------------------
# State + persistence
# --------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


@dataclass
class GoalState:
    id: str
    scope: str
    objective: str
    checks: list[str] = field(default_factory=list)
    status: str = "running"  # running | stopping | <TERMINAL>
    created_at: str = ""
    deadline: str = ""
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    iteration: int = 0
    no_progress: int = 0
    failures: int = 0
    done_claims: int = 0
    fingerprint: str = ""
    worktree: str = ""
    last_job_id: str = ""
    last_next: str = ""
    last_check_summary: str = ""
    detail: str = ""
    ctx: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def branch(self) -> str:
        return f"goal/{self.id}"

    @property
    def active(self) -> bool:
        return self.status in ("running", "stopping")

    def deadline_dt(self) -> datetime:
        return datetime.fromisoformat(self.deadline)

    def deadline_budget(self) -> float | None:
        """Seconds until this goal's own ``--hours`` deadline, or None if unknown."""
        try:
            return (self.deadline_dt() - _now()).total_seconds()
        except (ValueError, TypeError):
            return None


class GoalStore:
    """One JSON file per goal under ``<root>/goals``; atomic writes."""

    def __init__(self, root: Path) -> None:
        self.dir = Path(root) / "goals"

    def _path(self, goal_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", goal_id):
            raise ValueError("invalid goal id")
        return self.dir / f"{goal_id}.json"

    def save(self, state: GoalState) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(state.id)
        tmp = path.with_suffix(f".{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_text(json.dumps(dataclasses.asdict(state), ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, path)

    def load(self, goal_id: str) -> GoalState | None:
        try:
            data = json.loads(self._path(goal_id).read_text("utf-8"))
            known = {f.name for f in dataclasses.fields(GoalState)}
            return GoalState(**{k: v for k, v in data.items() if k in known})
        except (OSError, ValueError, TypeError):
            return None

    def all(self) -> list[GoalState]:
        if not self.dir.is_dir():
            return []
        states = [self.load(p.stem) for p in self.dir.glob("*.json")]
        return sorted((s for s in states if s), key=lambda s: s.created_at)

    def active_for(self, scope: str) -> GoalState | None:
        return next((s for s in reversed(self.all()) if s.scope == scope and s.active), None)

    def latest_for(self, scope: str) -> GoalState | None:
        return next((s for s in reversed(self.all()) if s.scope == scope), None)


# --------------------------------------------------------------------------
# Prompt + verdict
# --------------------------------------------------------------------------

# The agent replies in Chinese and often in Markdown: accept a full-width colon
# and bold/backtick markup around the key or the value ("**GOAL_STATUS：** `done`").
_STATUS_RE = re.compile(r"GOAL_STATUS[*_`]*\s*[:：]\s*[*_`]*\s*(continue|done|blocked)\b", re.I)
_NEXT_RE = re.compile(r"^[\s>*_`-]*NEXT[*_`]*\s*[:：]\s*[*_`]*\s*(.+?)\s*$", re.M)


def parse_verdict(text: str | None) -> tuple[str, str]:
    """Return (status, next) from the agent's closing lines; default continue."""
    body = text or ""
    found = _STATUS_RE.findall(body)
    status = found[-1].lower() if found else "continue"
    nxt = _NEXT_RE.findall(body)
    return status, (nxt[-1].strip()[:300] if nxt else "")


def build_prompt(state: GoalState) -> str:
    lines = [
        f"你正在无人值守的「目标模式」下工作（第 {state.iteration}/{state.max_iterations} 轮，"
        f"截止 {state.deadline}）。没有人会在中途回答问题，请自己做合理决定。",
        "",
        f"目标：{state.objective}",
        "",
    ]
    if state.checks:
        lines.append("验收检查（你结束后由 harness 独立运行，不看你的自述；全部通过才算完成）：")
        lines += [f"- `{c}`" for c in state.checks]
    else:
        lines.append("没有固定验收命令：请自行设计并运行最能证明目标达成的测试，并把命令与结果写进回复。")
    lines.append("")
    if state.iteration > 1:
        lines.append("上一轮 harness 结果：")
        lines.append(state.last_check_summary or "（无检查结果）")
        if state.last_next:
            lines.append(f"你上一轮留下的下一步：{state.last_next}")
        if state.no_progress:
            lines.append(f"⚠ 已连续 {state.no_progress} 轮没有产生任何改动或进展，请换一个思路，"
                         "或明确报告 blocked。")
        lines.append("")
    lines += [
        "工作规则：",
        f"1. 只在当前隔离 worktree 内改动。先执行 `git checkout {state.branch} 2>/dev/null || "
        f"git checkout -b {state.branch}`；每轮结束前 `git add -A && git commit`（写清楚这轮做了什么）。",
        "2. 不要 push、merge、部署，不要碰生产配置、密钥、/opt/conveyor 或其他项目目录。",
        "3. 每一轮：先跑现有测试看失败 → 主动做探索性测试（边界、错误路径、手机视口、并发、断线重连）找新问题 "
        "→ 逐个修复 → 重新验证。优先修复会导致功能不可用的问题。",
        "4. 严禁为了通过检查而弱化、跳过或删除测试与检查；发现检查本身有误，请在回复里说明而不是改掉。",
        "5. 回复最后必须单独写两行：",
        "GOAL_STATUS: continue 或 done 或 blocked",
        "NEXT: 下一轮最该做的一件事（done 时写总结，blocked 时写阻塞原因）",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Verification + fingerprint
# --------------------------------------------------------------------------

@dataclass
class CheckResult:
    command: str
    ok: bool
    code: int | None
    output: str = ""


def summarize_checks(results: list[CheckResult]) -> str:
    if not results:
        return "（未配置验收命令）"
    lines = []
    for r in results:
        mark = "通过" if r.ok else f"失败(exit {r.code})"
        lines.append(f"- `{r.command}` → {mark}")
        if not r.ok and r.output:
            lines.append("  " + r.output[-600:].replace("\n", "\n  "))
    return "\n".join(lines)


def _child_env(settings: Any) -> dict[str, str]:
    """Secret-free environment for owner-supplied check commands."""
    try:
        from conveyor.core import subprocess_env
        env = subprocess_env.utility_env(settings)
        return dict(env) if env is not None else dict(subprocess_env.build_child_env())
    except Exception:
        return {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")}


def _inside_worktrees(settings: Any, worktree: str) -> bool:
    try:
        root = (Path(settings.codex_task_root) / "worktrees").resolve()
        path = Path(worktree).resolve()
        return path.is_dir() and (path == root or root in path.parents)
    except (OSError, AttributeError, ValueError):
        return False


async def _run(cmd: str, cwd: str, env: dict[str, str], timeout: float) -> tuple[int | None, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            proc.kill()
        await proc.wait()
        return None, f"timeout after {int(timeout)}s"
    return proc.returncode, out.decode("utf-8", errors="replace")


async def run_checks(settings: Any, state: GoalState, worktree: str) -> list[CheckResult]:
    if not state.checks:
        return []
    if not worktree or not _inside_worktrees(settings, worktree):
        return [CheckResult(c, False, None, "worktree unavailable; check not run") for c in state.checks]
    env = _child_env(settings)
    results = []
    for cmd in state.checks:
        code, out = await _run(cmd, worktree, env, CHECK_TIMEOUT_SECONDS)
        results.append(CheckResult(cmd, code == 0, code, out[-2000:]))
    return results


async def worktree_fingerprint(worktree: str, results: list[CheckResult]) -> str:
    parts = [",".join(f"{r.command}:{r.ok}" for r in results)]
    if worktree and Path(worktree).is_dir():
        for cmd in ("git rev-parse HEAD", "git status --porcelain", "git diff HEAD"):
            code, out = await _run(cmd, worktree, {"PATH": os.environ.get("PATH", "")}, 60)
            parts.append(f"{code}:{out}")
    return hashlib.sha256("\0".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

@dataclass
class IterationResult:
    ok: bool
    text: str = ""
    worktree: str = ""
    job_id: str = ""
    error: str = ""
    transient: bool = False  # rate limit / queue full: wait and retry, not a failure


class Driver(Protocol):
    async def run_iteration(self, state: GoalState, prompt: str) -> IterationResult: ...
    async def cancel_current(self) -> None: ...
    async def notify(self, text: str) -> None: ...


Verifier = Callable[[GoalState, str], Awaitable[list[CheckResult]]]
Fingerprinter = Callable[[str, list[CheckResult]], Awaitable[str]]
Sleeper = Callable[[float], Awaitable[None]]


class GoalEngine:
    def __init__(self, store: GoalStore, driver: Driver, *, verifier: Verifier,
                 fingerprint: Fingerprinter = worktree_fingerprint,
                 sleep: Sleeper = asyncio.sleep, now: Callable[[], datetime] = _now) -> None:
        self.store = store
        self.driver = driver
        self.verifier = verifier
        self.fingerprint = fingerprint
        self.sleep = sleep
        self.now = now

    def _refresh_stop(self, state: GoalState) -> None:
        fresh = self.store.load(state.id)
        if fresh is not None and fresh.status == "stopping":
            state.status = "stopping"

    def _stop_reason(self, state: GoalState) -> str:
        self._refresh_stop(state)
        if state.status == "stopping":
            return "stopped"
        if self.now() >= state.deadline_dt():
            return "timeout"
        if state.iteration >= state.max_iterations:
            return "max_iterations"
        if state.failures >= FAILURE_LIMIT:
            return "failed"
        if state.no_progress >= NO_PROGRESS_LIMIT:
            return "stalled"
        return ""

    async def run(self, state: GoalState) -> GoalState:
        try:
            reason = ""
            while not reason:
                reason = self._stop_reason(state)
                if reason:
                    break
                reason = await self._iterate(state)
        except asyncio.CancelledError:
            # Service shutdown: leave the state "running" so resume_all continues it.
            self.store.save(state)
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must report, not vanish
            logger.exception("goal %s crashed", state.id)
            state.detail = f"{type(exc).__name__}: {exc}"[:300]
            reason = "failed"
        state.status = reason
        self.store.save(state)
        await self._safe_notify(self.final_report(state))
        return state

    async def _iterate(self, state: GoalState) -> str:
        state.iteration += 1
        self.store.save(state)
        started = time.monotonic()
        result = await self.driver.run_iteration(state, build_prompt(state))
        entry: dict[str, Any] = {"iteration": state.iteration, "job_id": result.job_id,
                                 "at": _iso(self.now())}
        if result.job_id:
            state.last_job_id = result.job_id

        if result.transient:
            state.iteration -= 1  # not a real attempt
            self.store.save(state)
            await self._safe_notify(f"🎯 目标暂缓：{result.error or '队列繁忙'}，{TRANSIENT_RETRY_SECONDS}s 后重试。")
            await self.sleep(TRANSIENT_RETRY_SECONDS)
            return ""
        self._refresh_stop(state)
        if state.status == "stopping":
            state.history.append({**entry, "outcome": "stopped"})
            return "stopped"  # the cancelled job is not a failure
        if not result.ok:
            state.failures += 1
            entry.update(outcome="failed", error=result.error[:300])
            state.history.append(entry)
            state.detail = result.error[:300]
            self.store.save(state)
            if result.error != GOAL_DEADLINE_ERROR:
                # A goal-deadline stop is not an iteration failure; the loop ends
                # with "timeout" on the next _stop_reason check, so do not spam the
                # owner with a "第 N 轮失败" notice or burn the retry sleep.
                await self._safe_notify(
                    f"🎯 目标第 {state.iteration} 轮失败（{state.failures}/{FAILURE_LIMIT}）：{result.error[:200]}")
                if state.failures < FAILURE_LIMIT:
                    await self.sleep(30)
            return ""

        state.failures = 0
        if result.worktree:
            state.worktree = result.worktree
        verdict, nxt = parse_verdict(result.text)
        checks = await self.verifier(state, state.worktree)
        all_ok = all(r.ok for r in checks)
        fingerprint = await self.fingerprint(state.worktree, checks)
        state.no_progress = state.no_progress + 1 if fingerprint == state.fingerprint else 0
        state.fingerprint = fingerprint
        state.last_next = nxt
        state.last_check_summary = summarize_checks(checks)
        state.done_claims = state.done_claims + 1 if (verdict == "done" and all_ok) else 0
        entry.update(outcome=verdict, checks_ok=all_ok, progress=state.no_progress == 0,
                     seconds=int(time.monotonic() - started), next=nxt)
        state.history.append(entry)
        self.store.save(state)

        passed = sum(r.ok for r in checks)
        check_line = f"验收 {passed}/{len(checks)} 通过" if checks else "无固定验收"
        await self._safe_notify(
            f"🎯 第 {state.iteration}/{state.max_iterations} 轮 · {check_line} · "
            f"{'有进展' if state.no_progress == 0 else f'无进展 {state.no_progress}/{NO_PROGRESS_LIMIT}'}"
            + (f"\n下一步：{nxt}" if nxt else ""))

        if verdict == "blocked":
            state.detail = nxt
            return "blocked"
        needed = 1 if state.checks else DONE_CONFIRMATIONS
        if state.done_claims >= needed:
            return "done"
        return ""

    async def _safe_notify(self, text: str) -> None:
        try:
            await self.driver.notify(text)
        except Exception:  # noqa: BLE001
            logger.debug("goal notify failed", exc_info=True)

    @staticmethod
    def final_report(state: GoalState) -> str:
        head = _TERMINAL_LABEL.get(state.status, state.status)
        lines = [f"🎯 目标结束：{head}", f"目标：{state.objective}",
                 f"共 {state.iteration} 轮，起止 {state.created_at} → {state.history[-1]['at'] if state.history else '-'}"]
        if state.detail:
            lines.append(f"说明：{state.detail}")
        if state.last_check_summary:
            lines.append("最后一次验收：\n" + state.last_check_summary)
        if state.worktree:
            lines.append(f"改动在隔离分支 {state.branch}（worktree：{state.worktree}）。"
                         "用「改动」查看 diff；满意再应用，不满意可丢弃。未自动合并或部署。")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Queue-backed driver (real runs)
# --------------------------------------------------------------------------

class RecordingPort:
    """Wraps an OutboundPort and remembers the last substantial message."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.last_text = ""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _record(self, text: str) -> None:
        if text and not text.startswith(("⏳", "💭", "🔧", "仍在处理")):
            self.last_text = text

    async def reply(self, msg: Any, text: str) -> Any:
        self._record(text)
        return await self._inner.reply(msg, text)

    async def send_new(self, msg: Any, text: str) -> Any:
        self._record(text)
        return await self._inner.send_new(msg, text)

    async def edit_progress(self, msg: Any, placeholder_id: str, text: str) -> Any:
        self._record(text)
        return await self._inner.edit_progress(msg, placeholder_id, text)


_JOB_TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})


class QueueDriver:
    """Runs each iteration as a FIX job in the owner's normal session/worktree chain."""

    def __init__(self, msg: Any, runner: Any, settings: Any,
                 port_factory: Callable[[str], Any], notify: Callable[[str], Awaitable[Any]],
                 *, poll_seconds: float = 5.0,
                 iteration_timeout: float = ITERATION_TIMEOUT_SECONDS) -> None:
        self.msg = msg
        self.runner = runner
        self.settings = settings
        self.port_factory = port_factory
        self._notify = notify
        self.poll_seconds = poll_seconds
        self.iteration_timeout = iteration_timeout
        self.current_job_id = ""

    async def notify(self, text: str) -> None:
        await self._notify(text)

    async def cancel_current(self) -> None:
        if not self.current_job_id:
            return
        from handlers.job_queue import get_job_queue
        try:
            await get_job_queue().cancel(self.current_job_id)
        except Exception:  # noqa: BLE001
            logger.debug("goal cancel failed", exc_info=True)

    async def run_iteration(self, state: GoalState, prompt: str) -> IterationResult:
        from handlers.job_queue import get_job_queue
        from handlers.jobs import submit_codex_job
        from runner.types import JobMode

        port = RecordingPort(self.port_factory(prompt))
        msg = dataclasses.replace(self.msg, message_id=uuid.uuid4().hex, text=prompt)
        ok, message, queued = await submit_codex_job(
            msg, port, self.runner, mode=JobMode.FIX, prompt=prompt, wait=False)
        if not ok or queued is None:
            transient = any(k in message for k in ("rate limit", "queue full", "队列已满", "升级"))
            return IterationResult(False, error=message, transient=transient)
        self.current_job_id = queued.id
        queue = get_job_queue()
        # The iteration must not outlive the goal's own ``--hours`` deadline: a
        # 2h iteration cap would let the last iteration run long past it.
        remaining = state.deadline_budget()
        goal_budget = (self.iteration_timeout if remaining is None
                       else max(remaining, ITERATION_MIN_GRACE_SECONDS))
        budget = min(self.iteration_timeout, goal_budget)
        goal_limited = budget < self.iteration_timeout
        deadline = time.monotonic() + budget
        snap: dict[str, Any] | None = None
        while True:
            snap = queue.job_snapshot(queued.id)
            if snap is None:
                return IterationResult(False, job_id=queued.id, error="job record disappeared")
            if snap.get("state") in _JOB_TERMINAL:
                break
            if time.monotonic() > deadline:
                await self.cancel_current()
                error = GOAL_DEADLINE_ERROR if goal_limited else "iteration timed out"
                return IterationResult(False, job_id=queued.id, error=error)
            await asyncio.sleep(self.poll_seconds)
        self.current_job_id = ""
        await asyncio.sleep(1.0)  # let the port deliver the final message
        meta = snap.get("metadata") or {}
        worktree = str(meta.get("worktree_path") or "")
        if snap["state"] != "completed":
            return IterationResult(False, job_id=queued.id, worktree=worktree,
                                   error=str(snap.get("error") or snap["state"]))
        return IterationResult(True, text=port.last_text, worktree=worktree, job_id=queued.id)


# --------------------------------------------------------------------------
# Manager: start / status / stop / resume
# --------------------------------------------------------------------------

_TASKS: dict[str, asyncio.Task] = {}
_DRIVERS: dict[str, Any] = {}


def _store(settings: Any) -> GoalStore:
    return GoalStore(Path(settings.codex_memory_root))


def scope_of(msg: Any) -> str:
    return f"{msg.channel}:{msg.chat_id}"


def _spawn(state: GoalState, store: GoalStore, driver: Any, settings: Any) -> None:
    async def verifier(s: GoalState, worktree: str) -> list[CheckResult]:
        return await run_checks(settings, s, worktree)

    engine = GoalEngine(store, driver, verifier=verifier)
    _DRIVERS[state.id] = driver
    task = asyncio.get_running_loop().create_task(engine.run(state), name=f"goal-{state.id}")
    _TASKS[state.id] = task

    def _cleanup(_t: asyncio.Task) -> None:
        _TASKS.pop(state.id, None)
        _DRIVERS.pop(state.id, None)

    task.add_done_callback(_cleanup)


def describe(state: GoalState | None) -> str:
    if state is None:
        return "当前没有目标。用 /goal <目标> 开启。"
    label = "进行中" if state.status == "running" else (
        "正在停止" if state.status == "stopping" else _TERMINAL_LABEL.get(state.status, state.status))
    lines = [f"🎯 目标 {state.id}：{label}", f"目标：{state.objective}",
             f"进度：第 {state.iteration}/{state.max_iterations} 轮，无进展 {state.no_progress}/{NO_PROGRESS_LIMIT}，"
             f"截止 {state.deadline}"]
    if state.checks:
        lines.append("验收：" + "；".join(state.checks))
    if state.last_check_summary:
        lines.append("最近一次验收：\n" + state.last_check_summary)
    if state.last_next:
        lines.append(f"下一步：{state.last_next}")
    if state.worktree:
        lines.append(f"分支 {state.branch}，worktree：{state.worktree}")
    return "\n".join(lines)


async def handle_goal_command(msg: Any, settings: Any, command: GoalCommand,
                              driver_factory: Callable[[GoalState], Any]) -> str:
    """Run one parsed command; returns the text to reply with."""
    store = _store(settings)
    scope = scope_of(msg)
    if command.action == "help":
        return USAGE
    if command.action == "invalid":
        return f"{command.error}\n\n{USAGE}"
    if command.action == "status":
        return describe(store.active_for(scope) or store.latest_for(scope))
    if command.action == "stop":
        state = store.active_for(scope)
        if state is None:
            return "当前没有进行中的目标。"
        state.status = "stopping"
        store.save(state)
        driver = _DRIVERS.get(state.id)
        if driver is not None:
            await driver.cancel_current()
        if state.id not in _TASKS:  # orphaned (service restarted without resuming)
            state.status = "stopped"
            store.save(state)
            return f"目标 {state.id} 已停止。"
        return f"正在停止目标 {state.id}：当前这一轮会被取消，随后给出收尾报告。"

    existing = store.active_for(scope)
    if existing is not None:
        return f"已有进行中的目标 {existing.id}（第 {existing.iteration} 轮）。先 /goal stop，或用 /goal status 查看。"
    created = _now()
    state = GoalState(
        id=uuid.uuid4().hex[:12], scope=scope, objective=command.objective,
        checks=list(command.checks), created_at=_iso(created),
        deadline=_iso(created + timedelta(hours=command.hours)),
        max_iterations=command.max_iterations,
        ctx={"channel": msg.channel, "operator_id": msg.operator_id, "chat_id": msg.chat_id,
             "thread_id": getattr(msg, "thread_id", None)},
    )
    driver = driver_factory(state)  # may enrich state.ctx (e.g. durable session id)
    store.save(state)
    _spawn(state, store, driver, settings)
    checks = ("\n验收命令：" + "；".join(state.checks)) if state.checks else "\n未设验收命令：需要助手连续两轮声明完成才会收尾。"
    return (f"🎯 目标模式已开启（{state.id}）\n目标：{state.objective}{checks}\n"
            f"上限：{command.hours:g} 小时 / {command.max_iterations} 轮；连续 {NO_PROGRESS_LIMIT} 轮无进展自动停止。\n"
            "每轮在隔离 worktree 内改代码、测试、找问题；不会合并或部署。/goal status 看进度，/goal stop 停止。")


def resume_all(settings: Any, driver_for_ctx: Callable[[GoalState], Any | None]) -> int:
    """Restart loops that were running when the service went down. Needs a running loop."""
    store = _store(settings)
    resumed = 0
    for state in store.all():
        if state.status == "running" and state.id not in _TASKS:
            driver = driver_for_ctx(state)
            if driver is None:
                continue
            _spawn(state, store, driver, settings)
            resumed += 1
    return resumed
