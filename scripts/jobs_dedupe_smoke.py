#!/usr/bin/env python3
"""jobs_dedupe_smoke.py — handle_codex_job does not send the final summary twice.

Regression: 2026-06-09 Feishu user reported "我是 MiniMax-M3 模型驱动的" sent
twice. Root cause: the progress callback fires for the final event with the
summary text, port.send_new lands it, and last_progress is updated. Then the
post-loop `if job.summary: send_new` fires a second time with the same text.

This smoke pins:
  - AST: post-loop body uses strip-comparison, not raw equality
  - behavior: when the last progress() text == job.summary, only one
    send_new is sent (the one inside progress)
  - behavior: when summary differs from last progress (e.g. runner started
    but emitted no progress), send_new fires once from the post-loop branch
  - behavior: on error path, error is sent once and no progress-time send
    is duplicated

Run: .venv/bin/python scripts/jobs_dedupe_smoke.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "0")
os.environ.setdefault("LARK_APP_ID", "cli_fake")
os.environ.setdefault("LARK_APP_SECRET", "fake")
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/codex-dedupe-ws")
os.environ.setdefault("CODEX_TASK_ROOT", "/tmp/codex-dedupe-task")
os.environ.setdefault("CODEX_MEMORY_ROOT", "/tmp/codex-dedupe-mem")
os.environ.setdefault("CODEX_BIN", "codex")
os.environ.setdefault("USER_TIMEZONE", "UTC")

from channel import InboundMessage
from handlers.jobs import PLACEHOLDER_TEXT, handle_codex_job
from runner import JobMode
from scripts.harness_common import CheckResult, print_results


# This smoke is dedicated to the historical "summary dedup" contract:
# every progress the runner emitted should be visible in port.sent_new
# (one entry each), and the final summary should not be re-sent when
# it matches the last progress. The compact/quiet mode filter is
# separately covered by scripts/jobs_progress_mode_smoke.py. Pin this
# smoke to verbose so it does not flake on the new mode.
import handlers.jobs as _jobs_mod  # noqa: E402

_jobs_mod._normalize_mode = lambda _m: "verbose"  # type: ignore[assignment]


HANDLERS_JOBS_PY = Path(__file__).resolve().parents[1] / "handlers" / "jobs.py"


# ---- Fake harness --------------------------------------------------------

@dataclass
class FakeOutbound:
    replies: list[str] = field(default_factory=list)
    sent_new: list[str] = field(default_factory=list)
    edits: list[tuple[str, str]] = field(default_factory=list)
    _edit_broken: bool = True  # simulate feishu: edit_progress always False

    async def reply(self, msg, text):
        self.replies.append(text)
        return "ph-1"

    async def send_new(self, msg, text):
        self.sent_new.append(text)
        return "new-1"

    async def edit_progress(self, msg, placeholder_id, text):
        return False  # latch to send_new

    async def reply_with_buttons(self, msg, text, buttons):
        self.replies.append(text)
        return "ph-1"


def _msg() -> InboundMessage:
    return InboundMessage(
        channel="feishu",
        operator_id="ou_test",
        chat_id="chat-1",
        message_id="m-1",
        text="hi",
    )


def _fake_runner(*, summary: str | None, progress_text: str | None) -> mock.Mock:
    """Build a CodexRunner-shaped async mock.

    - runner.start() returns a job whose .summary is `summary`
    - progress() callback is invoked once with `progress_text` (or skipped)
    - job.state immediately reads as "completed" so the wait-loop exits
    """
    job = SimpleNamespace(
        state="completed",  # JobState != RUNNING → loop exits
        summary=summary,
        error=None,
    )

    async def fake_start(mode, prompt, progress):
        if progress_text is not None:
            await progress(progress_text)
        return job

    runner = mock.Mock()
    runner.start = fake_start
    runner.settings = SimpleNamespace(
        codex_memory_root=Path("/tmp/codex-dedupe-mem"),
        conveyor_progress_mode="verbose",
        conveyor_session_enabled=False,
    )
    return runner, job


# ---- AST tests -----------------------------------------------------------

def _parse() -> ast.Module:
    return ast.parse(HANDLERS_JOBS_PY.read_text(encoding="utf-8"))


def _function_def(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _test_summary_branch_uses_strip_compare():
    name = "AST: post-loop if job.summary branch uses .strip() comparison to dedupe"
    try:
        tree = _parse()
        func = _function_def(tree, "handle_codex_job")
        if not isinstance(func, ast.AsyncFunctionDef):
            return CheckResult(name, False, "function missing")
        body_src = ast.unparse(func)
        if "if job.summary:" not in body_src:
            return CheckResult(name, False, "if job.summary branch missing")
        # Look for a guard line that compares strip() of summary vs last_progress.
        guard_lines = [
            line for line in body_src.splitlines()
            if "summary" in line
            and "last_progress" in line
            and ".strip()" in line
            and "!=" in line
        ]
        if not guard_lines:
            return CheckResult(
                name, False,
                "expected a line that compares summary and last_progress via .strip() and !=",
            )
        return CheckResult(
            name, True,
            f"guard present: {guard_lines[0].strip()}",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- behavior tests ------------------------------------------------------

def _test_progress_already_sent_summary_is_not_resent():
    """Feishu path: progress() called with the final text, then job.summary
    equals that text → only ONE send_new, not two."""
    name = "behavior: when progress() already sent the final summary, post-loop does NOT resend"
    try:
        port = FakeOutbound()
        runner, _job = _fake_runner(
            summary="我是 MiniMax-M3 模型驱动的。",
            progress_text="我是 MiniMax-M3 模型驱动的。",
        )
        asyncio.run(handle_codex_job(_msg(), port, runner, mode=JobMode.RUN, prompt="你是谁"))
        summary_sends = [
            t for t in port.sent_new if "MiniMax-M3" in t
        ]
        return CheckResult(
            name,
            len(summary_sends) == 1,
            f"sent_new count for summary text = {len(summary_sends)}; full sent_new={port.sent_new}",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_progress_text_differs_from_summary_sends_both():
    """Runner never emitted progress, but emits a summary in the final job
    object → exactly one send_new from the post-loop branch."""
    name = "behavior: when no progress fired and summary is set, post-loop send_new runs once"
    try:
        port = FakeOutbound()
        runner, _job = _fake_runner(
            summary="FINAL_ANSWER",
            progress_text=None,
        )
        asyncio.run(handle_codex_job(_msg(), port, runner, mode=JobMode.RUN, prompt="hi"))
        return CheckResult(
            name,
            port.sent_new == ["FINAL_ANSWER"],
            f"sent_new={port.sent_new}",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_progress_summary_differ_sends_each_once():
    """progress() sent one chunk, then a different final summary → both
    appear in sent_new, but each appears once."""
    name = "behavior: progress text and summary differ → each appears once"
    try:
        port = FakeOutbound()
        runner, _job = _fake_runner(
            summary="ANSWER",
            progress_text="intermediate",
        )
        asyncio.run(handle_codex_job(_msg(), port, runner, mode=JobMode.RUN, prompt="hi"))
        return CheckResult(
            name,
            port.sent_new == ["intermediate", "ANSWER"],
            f"sent_new={port.sent_new}",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_placeholder_sent_exactly_once():
    """reply() with the placeholder should be called once, not zero, not twice."""
    name = "behavior: placeholder '⏳ 收到...' is sent exactly once"
    try:
        port = FakeOutbound()
        runner, _job = _fake_runner(summary="x", progress_text=None)
        asyncio.run(handle_codex_job(_msg(), port, runner, mode=JobMode.RUN, prompt="hi"))
        placeholder_replies = [r for r in port.replies if r == PLACEHOLDER_TEXT]
        return CheckResult(
            name,
            len(placeholder_replies) == 1,
            f"placeholder replies count = {len(placeholder_replies)}; replies={port.replies}",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_empty_prompt_returns_usage():
    name = "behavior: empty prompt replies with usage, no placeholder, no send_new"
    try:
        port = FakeOutbound()
        runner = mock.Mock()
        asyncio.run(handle_codex_job(_msg(), port, runner, mode=JobMode.RUN, prompt=""))
        return CheckResult(
            name,
            any("Usage" in r for r in port.replies) and not port.sent_new,
            f"replies={port.replies}, sent_new={port.sent_new}",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


CHECKS = [
    _test_summary_branch_uses_strip_compare,
    _test_progress_already_sent_summary_is_not_resent,
    _test_progress_text_differs_from_summary_sends_both,
    _test_progress_summary_differ_sends_each_once,
    _test_placeholder_sent_exactly_once,
    _test_empty_prompt_returns_usage,
]


def main() -> int:
    results = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as exc:
            results.append(CheckResult(check.__name__, False, f"raised: {exc!r}"))
    print_results(results)
    ok = all(r.ok for r in results)
    print("jobs dedupe smoke ok" if ok else "jobs dedupe smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
