#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import JobMode
from scripts.edit_harness import EditHarnessOutcome
from scripts.harness_common import CheckResult, print_results
from redaction import truncate


@dataclass
class FakeMessage:
    text: str = ""
    replies: list[str] | None = None

    async def reply_text(self, text: str, **_: Any) -> SimpleNamespace:
        if self.replies is None:
            self.replies = []
        self.replies.append(text)
        return SimpleNamespace(message_id=1)


@dataclass
class FakeBot:
    # Each entry: (chat_id, text, message_id) in send order. message_id
    # matches the SimpleNamespace returned to the caller, so test code can
    # assert the placeholder id and then check the edits addressed to it.
    sent: list[tuple[int, str, int]]
    # Each entry: (chat_id, message_id, text) in edit order.
    edits: list[tuple[int, int, str]]
    # Each entry: (chat_id, action) - e.g. "typing". No-op for the bot.
    chat_actions: list[tuple[int, str]]
    _next_message_id: int = 1

    async def send_message(self, chat_id: int, text: str, **_: Any) -> SimpleNamespace:
        mid = self._next_message_id
        self._next_message_id += 1
        self.sent.append((chat_id, text, mid))
        return SimpleNamespace(message_id=mid)

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, **_: Any) -> None:
        self.edits.append((chat_id, message_id, text))

    async def send_chat_action(self, chat_id: int, action: str, **_: Any) -> None:
        self.chat_actions.append((chat_id, action))


@dataclass
class FakeUpdate:
    user_id: int
    text: str = ""
    username: str = "harness"

    def __post_init__(self) -> None:
        self.effective_user = SimpleNamespace(id=self.user_id, username=self.username)
        self.effective_chat = SimpleNamespace(id=4242)
        self.effective_message = FakeMessage(self.text, [])
        self._bot = FakeBot([], [], [])

    def get_bot(self) -> FakeBot:
        return self._bot


@dataclass
class FakeContext:
    args: list[str]


@dataclass
class FakeJob:
    id: str


class FakeRunner:
    def __init__(self) -> None:
        # Mirrors CodexRunner.MEMO_CATEGORIES so bot.memory_cmd's arg
        # classification can run without touching the real runner.
        self.MEMO_CATEGORIES: tuple[str, ...] = (
            "preference", "fact", "tool-quirk", "convention", "unfiled",
        )
        self.started: list[tuple[JobMode, str]] = []
        # Memo fast-path observability. (category, content, auto_timestamp)
        self.appended: list[tuple[str, str, bool]] = []
        # Things the classifier was asked to label. Hard-coded to return
        # "fact" for the untagged default; harness can't exercise the
        # urllib error path without mocking stdlib.
        self.classified: list[str] = []
        self._next_classification: str = "fact"
        # Read paths
        self.memory_reads: list[str | None] = []
        self.journal_reads: list[tuple[str, str | None]] = []
        # Fake on-disk content for read_memory / read_journal
        self._memory_text: str = (
            "# MEMORY.md — 2026-06-04\n\n"
            "## preference\n- 用 pnpm\n- dark mode\n\n"
            "## fact\n- AAPL $310\n\n"
            "## unfiled\n- 这是一条悬而未决的备忘\n"
        )
        self._journal_text: dict[str, str] = {
            "2026-06-03": (
                "# Journal 2026-06-03\n\n"
                "## fact\n- TSLA close $248\n\n"
                "## tool-quirk\n- codex --json 需要 - 最后\n"
            ),
        }

    def status_text(self) -> str:
        return "STATUS_OK"

    async def diff_text(self) -> str:
        return "DIFF_OK"

    async def cancel(self) -> str:
        return "CANCEL_OK"

    def jobs_text(self, limit: int = 8) -> str:
        return f"JOBS_OK_{limit}"

    def last_text(self) -> str:
        return "LAST_OK"

    async def clean_old_jobs(self, keep: int = 20) -> str:
        return f"CLEAN_OK_{keep}"

    async def discard_last_job(self) -> str:
        return "DISCARD_OK"

    async def apply_last_job(self) -> str:
        return "APPLY_OK"

    async def start(self, mode: JobMode, prompt: str, progress: Callable[[str], Awaitable[None]]) -> FakeJob:
        self.started.append((mode, prompt))
        await progress("PROGRESS_OK")
        return FakeJob("job-harness")

    # --- memo fast-path stand-ins ---
    async def append_memo(self, category: str, content: str, *, auto_timestamp: bool = False) -> str:
        self.appended.append((category, content, auto_timestamp))
        return f"记下了: {category} · {truncate(content, 60)}"

    async def classify_memo(self, content: str) -> str:
        self.classified.append(content)
        return self._next_classification

    async def reclassify_unfiled(self, content: str) -> tuple[str, int]:
        # Stand-in: in the harness we don't exercise the LLM, so this returns
        # the content untouched and reports zero moves. Real behavior is
        # covered by scripts/memo_smoke.py.
        return content, 0

    def read_memory(self, category: str | None = None) -> str:
        self.memory_reads.append(category)
        if category is None:
            return self._memory_text
        marker = f"## {category}\n"
        if marker not in self._memory_text:
            return ""
        tail = self._memory_text.split(marker, 1)[1]
        # Cut at next "## " heading
        lines: list[str] = []
        for line in tail.splitlines():
            if line.startswith("## ") and lines:
                break
            lines.append(line)
        return f"{marker}{''.join(line + chr(10) for line in lines).rstrip()}"

    def read_journal(self, date_str: str, category: str | None = None) -> str:
        self.journal_reads.append((date_str, category))
        text = self._journal_text.get(date_str, "")
        if not text:
            return ""
        if category is None:
            return text
        marker = f"## {category}\n"
        if marker not in text:
            return ""
        tail = text.split(marker, 1)[1]
        lines: list[str] = []
        for line in tail.splitlines():
            if line.startswith("## ") and lines:
                break
            lines.append(line)
        return f"{marker}{''.join(line + chr(10) for line in lines).rstrip()}"

def _patch(module: Any, name: str, value: Any) -> Callable[[], None]:
    original = getattr(module, name)
    setattr(module, name, value)

    def restore() -> None:
        setattr(module, name, original)

    return restore


def _ok_result(name: str) -> CheckResult:
    return CheckResult(name, True, "COMMAND_HARNESS_OK")


async def _run_case(
    name: str,
    handler: Callable[[Any, Any], Awaitable[None]],
    module: Any,
    args: list[str] | None = None,
    text: str = "",
    authorized: bool = True,
    expect: str = "",
) -> CheckResult:
    user_id = module.settings.telegram_allowed_user_id if authorized else module.settings.telegram_allowed_user_id + 1
    update = FakeUpdate(user_id=user_id, text=text)
    context = FakeContext(args or [])
    try:
        await handler(update, context)
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")
    replies = update.effective_message.replies or []
    detail = " | ".join(replies) if replies else "(no replies)"
    if expect and not any(expect in reply for reply in replies):
        return CheckResult(name, False, f"expected {expect!r}; replies={detail}")
    # `expect` empty means "verify the handler did not raise" — first-reply is
    # dispatched asynchronously by the runner's progress callback, not by the
    # command handler itself, so FakeUpdate has no replies to inspect here.
    if not expect:
        return CheckResult(name, True, "no expectation (handler ran without raising)")
    return CheckResult(name, True, detail)


async def run_command_harness() -> int:
    try:
        import bot
    except ModuleNotFoundError as exc:
        print(f"command harness failed: missing dependency while importing bot: {exc}")
        return 1

    fake_runner = FakeRunner()

    async def fake_maintain(*_: Any, **__: Any) -> SimpleNamespace:
        return SimpleNamespace(summary="MAINTAIN_OK")

    async def fake_smoke(*_: Any, **__: Any) -> int:
        return 0

    async def fake_edit(*_: Any, **__: Any) -> EditHarnessOutcome:
        return EditHarnessOutcome(0, "EDITCHECK_OK")

    def fake_health_snapshot(*_args: Any, **_kwargs: Any) -> dict:
        return {
            "ok": True,
            "mode": "full" if _kwargs.get("include_offline") else "fast",
            "latest_job": {"id": "job-health", "state": "completed", "summary": "HEALTH_OK"},
            "metrics": {"count": 3, "success_rate": 100, "rate_limit_hits": 0},
            "checks": {
                "offline_harnesses": (
                    [{"name": "replay", "ok": True}, {"name": "fault_harness", "ok": True}]
                    if _kwargs.get("include_offline")
                    else []
                )
            },
            "triage": [],
        }

    restorers = [
        _patch(bot, "runner", fake_runner),
        _patch(bot, "run_maintenance", fake_maintain),
        _patch(bot, "diagnostics_report", lambda *_args, **_kwargs: "DIAG_OK"),
        _patch(bot, "run_security_audit", lambda *_args, **_kwargs: [_ok_result("security")]),
        _patch(bot, "rate_limit_report", lambda *_args, **_kwargs: "RATELIMIT_OK"),
        _patch(bot, "run_job_audit", lambda *_args, **_kwargs: [_ok_result("audit")]),
        _patch(bot, "summarize_log", lambda *_args, **_kwargs: "LOG_OK"),
        _patch(bot, "metadata_report", lambda *_args, **_kwargs: "META_OK"),
        _patch(bot, "metrics_report", lambda *_args, **_kwargs: "METRICS_OK"),
        _patch(bot, "health_snapshot", fake_health_snapshot),
        _patch(bot, "run_smoke", fake_smoke),
        _patch(bot, "run_edit_harness", fake_edit),
        _patch(bot, "check_systemd_active", lambda *_args, **_kwargs: _ok_result("systemd")),
        _patch(bot, "check_workspace", lambda *_args, **_kwargs: _ok_result("workspace")),
        _patch(bot, "check_minimax_models", lambda *_args, **_kwargs: _ok_result("minimax")),
        _patch(bot, "check_disk", lambda *_args, **_kwargs: _ok_result("disk")),
        _patch(bot, "check_runtime_dirs", lambda *_args, **_kwargs: [_ok_result("runtime")]),
        _patch(bot, "check_latest_job", lambda *_args, **_kwargs: [_ok_result("latest")]),
    ]
    try:
        cases = [
            ("unauthorized", bot.status_cmd, [], "", False, "Unauthorized."),
            ("run usage", bot.run_cmd, [], "", True, "Usage: /run <prompt>"),
            ("fix usage", bot.fix_cmd, [], "", True, "Usage: /fix <prompt>"),
            ("run starts", bot.run_cmd, ["say", "hi"], "", True, ""),
            ("fix starts", bot.fix_cmd, ["change"], "", True, ""),
            ("text starts", bot.text_cmd, [], "plain prompt", True, ""),
            # --- memo fast-path ---
            ("memo tagged", bot.memo_cmd, ["[preference]", "用", "pnpm"], "", True, "记下了: preference"),
            ("memo untagged", bot.memo_cmd, ["AAPL", "$310"], "", True, "记下了: fact"),
            ("memo empty usage", bot.memo_cmd, [], "", True, "Usage: /memo"),
            ("text memo tagged", bot.text_cmd, [], "记 [tool-quirk] codex 沙箱无网", True, "记下了: tool-quirk"),
            ("text memo untagged", bot.text_cmd, [], "记 AAPL $310", True, "记下了: fact"),
            ("text memo empty", bot.text_cmd, [], "记", True, "Usage: 记"),
            # --- /memory reads ---
            ("memory default", bot.memory_cmd, [], "", True, "MEMORY.md @"),
            ("memory preference", bot.memory_cmd, ["preference"], "", True, "## preference"),
            ("memory fact", bot.memory_cmd, ["fact"], "", True, "## fact"),
            ("memory missing section", bot.memory_cmd, ["convention"], "", True, "## convention 段"),
            ("memory date", bot.memory_cmd, ["2026-06-03"], "", True, "Journal 2026-06-03"),
            ("memory date+cat", bot.memory_cmd, ["2026-06-03", "fact"], "", True, "Journal 2026-06-03 · fact"),
            ("memory date missing", bot.memory_cmd, ["1999-01-01"], "", True, "没找到或为空"),
            ("memory date+missing", bot.memory_cmd, ["2026-06-03", "convention"], "", True, "没找到或为空"),
            ("status", bot.status_cmd, [], "", True, "STATUS_OK"),
            ("diff", bot.diff_cmd, [], "", True, "DIFF_OK"),
            ("cancel", bot.cancel_cmd, [], "", True, "CANCEL_OK"),
            ("jobs clamp", bot.jobs_cmd, ["999"], "", True, "JOBS_OK_30"),
            ("jobs invalid", bot.jobs_cmd, ["nope"], "", True, "JOBS_OK_8"),
            ("last", bot.last_cmd, [], "", True, "LAST_OK"),
            ("clean clamp", bot.clean_cmd, ["9999"], "", True, "CLEAN_OK_200"),
            ("discard", bot.discard_cmd, [], "", True, "DISCARD_OK"),
            ("apply", bot.apply_cmd, [], "", True, "APPLY_OK"),
            ("maintain", bot.maintain_cmd, ["25"], "", True, "MAINTAIN_OK"),
            ("doctor", bot.doctor_cmd, [], "", True, "[ok] systemd"),
            ("diag", bot.diag_cmd, ["1", "hour", "ago"], "", True, "DIAG_OK"),
            ("security", bot.security_cmd, ["1", "hour", "ago"], "", True, "[ok] security"),
            ("ratelimit", bot.ratelimit_cmd, ["bad"], "", True, "RATELIMIT_OK"),
            ("audit", bot.audit_cmd, ["bad"], "", True, "[ok] audit"),
            ("log", bot.log_cmd, ["latest"], "", True, "LOG_OK"),
            ("meta", bot.meta_cmd, ["latest"], "", True, "META_OK"),
            ("metrics", bot.metrics_cmd, ["bad"], "", True, "METRICS_OK"),
            ("health", bot.health_cmd, [], "", True, "Offline: none"),
            ("health full", bot.health_cmd, ["full"], "", True, "fault_harness=ok"),
            ("health json", bot.health_cmd, ["json", "nosecurity"], "", True, '"ok":true'),
            ("smoke", bot.smoke_cmd, [], "", True, "smoke 通过"),
            ("editcheck", bot.editcheck_cmd, [], "", True, "EDITCHECK_OK"),
            ("start help", bot.start_cmd, [], "", True, "直接发消息给我"),
        ]
        results = [
            await _run_case(name, handler, bot, args, text, authorized, expect)
            for name, handler, args, text, authorized, expect in cases
        ]
        results.append(
            CheckResult(
                "started jobs",
                fake_runner.started
                == [
                    (JobMode.RUN, "say hi"),
                    (JobMode.FIX, "change"),
                    (JobMode.RUN, "plain prompt"),
                ],
                repr(fake_runner.started),
            )
        )
        # Memo fast-path side effects: 5 memos recorded (tagged + untagged x2
        # sources = 4 successful + 1 empty-short-circuit). 1 classifier call
        # because the rest were tagged.
        results.append(
            CheckResult(
                "memo appended",
                fake_runner.appended
                == [
                    ("preference", "用 pnpm", False),
                    ("fact", "AAPL $310", True),
                    ("tool-quirk", "codex 沙箱无网", False),
                    ("fact", "AAPL $310", True),
                ],
                repr(fake_runner.appended),
            )
        )
        results.append(
            CheckResult(
                "memo classified",
                # Both `memo_cmd(["AAPL", "$310"])` and
                # `text_cmd("记 AAPL $310")` route to the untagged path
                # and ask the classifier to label the same content.
                fake_runner.classified == ["AAPL $310", "AAPL $310"],
                repr(fake_runner.classified),
            )
        )
        # Read paths: every /memory call (8 of them above) should have
        # recorded exactly one entry on its respective list.
        results.append(
            CheckResult(
                "memory reads",
                fake_runner.memory_reads
                == [None, "preference", "fact", "convention"],
                repr(fake_runner.memory_reads),
            )
        )
        results.append(
            CheckResult(
                "journal reads",
                fake_runner.journal_reads
                == [
                    ("2026-06-03", None),
                    ("2026-06-03", "fact"),
                    ("1999-01-01", None),
                    ("2026-06-03", "convention"),
                ],
                repr(fake_runner.journal_reads),
            )
        )
        failed_summary = bot._health_summary(
            {
                "ok": False,
                "latest_job": {"id": "job-health", "state": "completed", "summary": "HEALTH_OK"},
                "metrics": {"count": 3, "success_rate": 67, "rate_limit_hits": 1},
                "checks": {
                    "doctor": [{"name": "systemd", "ok": False, "detail": "codex-telegram-bot is failed"}],
                    "offline_harnesses": [],
                    "security": [],
                    "job_audit": [],
                },
                "triage": ["- systemd: check service"],
            }
        )
        results.append(CheckResult("health failed summary", "Failing checks:" in failed_summary and "Latest:" not in failed_summary, failed_summary))
    finally:
        for restore in reversed(restorers):
            restore()

    ok = print_results(results)
    if ok:
        print("command harness ok")
    else:
        print("command harness failed")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline Telegram command-handler harness.")
    parser.parse_args()
    raise SystemExit(asyncio.run(run_command_harness()))


if __name__ == "__main__":
    main()
