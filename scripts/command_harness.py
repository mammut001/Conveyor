#!/usr/bin/env python3
"""command_harness.py — channel-agnostic command dispatch harness (003 P1.3).

Exercises handlers.dispatch against a FakeOutboundPort and a FakeRunner.
Every case used to live behind `bot.<name>_cmd`; the P1 refactor moved
the actual logic into handlers/commands.py and reduced bot.py's command
handlers to 3-line wrappers. This harness now targets the channel-agnostic
layer so it covers Telegram and Feishu uniformly and doesn't have to
build a fake python-telegram-bot update tree.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Tests will load settings; isolate them from a real .env first.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "0")
os.environ.setdefault("LARK_APP_ID", "cli_fake")
os.environ.setdefault("LARK_APP_SECRET", "fake")
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/codex-harness-ws")
os.environ.setdefault("CODEX_TASK_ROOT", "/tmp/codex-harness-tasks")
os.environ.setdefault("CODEX_MEMORY_ROOT", "/tmp/codex-harness-mem")
os.environ.setdefault("CODEX_BIN", "codex")
os.environ.setdefault("USER_TIMEZONE", "UTC")

from channel import InboundMessage
from config import load_settings
from handlers import dispatch
from redaction import truncate
from runner import JobMode
from scripts.harness_common import CheckResult, print_results


# ---- Fake outbound --------------------------------------------------------

@dataclass
class FakeOutbound:
    replies: list[str] = field(default_factory=list)
    sent_new: list[str] = field(default_factory=list)
    edits: list[tuple[str, str]] = field(default_factory=list)
    _edit_broken: bool = False

    async def reply(self, msg, text):
        self.replies.append(text)
        return "ph-1"

    async def send_new(self, msg, text):
        self.sent_new.append(text)
        return "new-1"

    async def edit_progress(self, msg, placeholder_id, text):
        if self._edit_broken:
            return False
        self.edits.append((placeholder_id, text))
        return True

    async def reply_with_buttons(self, msg, text, buttons):
        self.replies.append(text)
        return "ph-1"


# ---- Fake runner ----------------------------------------------------------

class FakeRunner:
    """Stub matching the surface handlers/commands.py touches."""

    def __init__(self) -> None:
        self.MEMO_CATEGORIES: tuple[str, ...] = (
            "preference", "fact", "tool-quirk", "convention", "unfiled",
        )
        self.started: list[tuple[JobMode, str]] = []
        self.appended: list[tuple[str, str, bool]] = []
        self.classified: list[str] = []
        self._next_classification: str = "fact"
        self.memory_reads: list[str | None] = []
        self.journal_reads: list[tuple[str, str | None]] = []
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

    async def start(self, mode: JobMode, prompt: str, progress: Callable[[str], Awaitable[None]]) -> Any:
        self.started.append((mode, prompt))
        await progress("PROGRESS_OK")
        return SimpleNamespace(
            id="job-harness",
            state="completed",
            summary="SUMMARY_OK",
            error=None,
        )

    async def append_memo(self, category: str, content: str, *, auto_timestamp: bool = False) -> str:
        self.appended.append((category, content, auto_timestamp))
        return f"记下了: {category} · {truncate(content, 60)}"

    async def classify_memo(self, content: str) -> str:
        self.classified.append(content)
        return self._next_classification

    def read_memory(self, category: str | None = None) -> str:
        self.memory_reads.append(category)
        if category is None:
            return self._memory_text
        marker = f"## {category}\n"
        if marker not in self._memory_text:
            return ""
        tail = self._memory_text.split(marker, 1)[1]
        lines: list[str] = []
        for line in tail.splitlines():
            if line.startswith("## ") and lines:
                break
            lines.append(line)
        return f"{marker}{chr(10).join(lines)}"

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
        return f"{marker}{chr(10).join(lines)}"

    def list_journal(self, limit: int = 10) -> list:
        # No real files in the harness; return a fake ordered list.
        return list(self._journal_text.keys())[:limit]


# ---- module patches for handlers/commands.py dependencies -----------------

def _ok_result(name: str) -> CheckResult:
    return CheckResult(name, True, "OK")


def _install_module_fakes() -> list[Callable[[], None]]:
    """Patch scripts.* in handlers.commands so reports return canned data.

    handlers.commands imports the script functions at module load, so
    monkey-patching handlers.commands.<name> is what the runtime sees.
    """
    import handlers.commands as hc

    async def fake_maintain(*_: Any, **__: Any) -> Any:
        return SimpleNamespace(summary="MAINTAIN_OK")

    async def fake_smoke(*_: Any, **__: Any) -> int:
        return 0

    async def fake_edit(*_: Any, **__: Any) -> Any:
        return SimpleNamespace(code=0, summary="EDITCHECK_OK")

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

    def _patch(obj: Any, name: str, value: Any) -> Callable[[], None]:
        original = getattr(obj, name)
        setattr(obj, name, value)

        def restore() -> None:
            setattr(obj, name, original)

        return restore

    return [
        _patch(hc, "run_maintenance", fake_maintain),
        _patch(hc, "diagnostics_report", lambda *_a, **_k: "DIAG_OK"),
        _patch(hc, "run_security_audit", lambda *_a, **_k: [_ok_result("security")]),
        _patch(hc, "rate_limit_report", lambda *_a, **_k: "RATELIMIT_OK"),
        _patch(hc, "run_job_audit", lambda *_a, **_k: [_ok_result("audit")]),
        _patch(hc, "summarize_log", lambda *_a, **_k: "LOG_OK"),
        _patch(hc, "metadata_report", lambda *_a, **_k: "META_OK"),
        _patch(hc, "metrics_report", lambda *_a, **_k: "METRICS_OK"),
        _patch(hc, "health_snapshot", fake_health_snapshot),
        _patch(hc, "run_smoke", fake_smoke),
        _patch(hc, "run_edit_harness", fake_edit),
        _patch(hc, "check_systemd_active", lambda *_a, **_k: _ok_result("systemd")),
        _patch(hc, "check_workspace", lambda *_a, **_k: _ok_result("workspace")),
        _patch(hc, "check_minimax_models", lambda *_a, **_k: _ok_result("minimax")),
        _patch(hc, "check_disk", lambda *_a, **_k: _ok_result("disk")),
        _patch(hc, "check_runtime_dirs", lambda *_a, **_k: [_ok_result("runtime")]),
        _patch(hc, "check_latest_job", lambda *_a, **_k: [_ok_result("latest")]),
    ]


# ---- harness core ---------------------------------------------------------

def _msg(text: str, *, operator_id: str = "0") -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        operator_id=operator_id,
        chat_id="4242",
        message_id="m-1",
        text=text,
    )


async def _run_case(
    name: str,
    port: FakeOutbound,
    settings,
    runner: FakeRunner,
    text: str,
    *,
    authorized: bool = True,
    expect: str = "",
) -> CheckResult:
    op = str(settings.telegram_allowed_user_id) if authorized else "999999"
    msg = _msg(text, operator_id=op)
    try:
        await dispatch(msg, port, settings, runner)
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")
    replies = port.replies + port.sent_new
    detail = " | ".join(replies) if replies else "(no replies)"
    if expect and not any(expect in r for r in replies):
        return CheckResult(name, False, f"expected {expect!r}; replies={detail}")
    return CheckResult(name, True, detail or "(no replies expected)")


async def run_command_harness() -> int:
    settings = load_settings()
    fake_runner = FakeRunner()
    restorers = _install_module_fakes()

    try:
        # 003 P1.3: every command flows through handlers.dispatch.
        cases = [
            ("unauthorized", "/status", False, "Unauthorized."),
            ("run usage", "/run", True, "用法：/run"),
            ("fix usage", "/fix", True, "用法：/fix"),
            ("run starts", "/run say hi", True, ""),
            ("fix starts", "/fix change", True, ""),
            ("text starts", "plain prompt", True, ""),
            # memo fast-path
            ("memo tagged", "/memo [preference] 用 pnpm", True, "记下了: preference"),
            ("memo untagged", "/memo AAPL $310", True, "记下了: fact"),
            ("memo empty usage", "/memo", True, "用法：/memo"),
            ("text memo tagged", "记 [tool-quirk] codex 沙箱无网", True, "记下了: tool-quirk"),
            ("text memo untagged", "记 AAPL $310", True, "记下了: fact"),
            ("text memo empty", "记", True, "Usage"),
            # /memory reads
            ("memory default", "/memory", True, "MEMORY.md @"),
            ("memory preference", "/memory preference", True, "## preference"),
            ("memory fact", "/memory fact", True, "## fact"),
            ("memory missing section", "/memory convention", True, "## convention 段"),
            ("memory date", "/memory 2026-06-03", True, "Journal 2026-06-03"),
            ("memory date+cat", "/memory 2026-06-03 fact", True, "Journal 2026-06-03 · fact"),
            ("memory date missing", "/memory 1999-01-01", True, "没找到或为空"),
            ("memory date+missing", "/memory 2026-06-03 convention", True, "没找到或为空"),
            # status / diff / cancel
            ("status", "/status", True, "STATUS_OK"),
            ("diff", "/diff", True, "DIFF_OK"),
            ("cancel", "/cancel", True, "CANCEL_OK"),
            ("jobs clamp", "/jobs 999", True, "JOBS_OK_30"),
            ("jobs invalid", "/jobs nope", True, "JOBS_OK_8"),
            ("last", "/last", True, "LAST_OK"),
            ("clean clamp", "/clean 9999", True, "CLEAN_OK_200"),
            ("discard", "/discard", True, "DISCARD_OK"),
            ("apply", "/apply", True, "APPLY_OK"),
            ("maintain", "/maintain 25", True, "MAINTAIN_OK"),
            ("doctor", "/doctor", True, "[ok] systemd"),
            ("diag", "/diag 1 hour ago", True, "DIAG_OK"),
            ("security", "/security 1 hour ago", True, "[ok] security"),
            ("ratelimit", "/ratelimit bad", True, "RATELIMIT_OK"),
            ("audit", "/audit bad", True, "[ok] audit"),
            ("log", "/log latest", True, "LOG_OK"),
            ("meta", "/meta latest", True, "META_OK"),
            ("metrics", "/metrics bad", True, "METRICS_OK"),
            ("health", "/health", True, "Offline: none"),
            ("smoke", "/smoke", True, "smoke 通过"),
            ("editcheck", "/editcheck", True, "EDITCHECK_OK"),
            ("help", "/help", True, "Codex Bot"),
            ("unknown", "/nonsense", True, "未知命令"),
        ]
        results: list[CheckResult] = []
        ports: list[FakeOutbound] = []
        for name, text, authorized, expect in cases:
            port = FakeOutbound()
            ports.append(port)
            results.append(
                await _run_case(name, port, settings, fake_runner, text, authorized=authorized, expect=expect)
            )

        # Side-effect checks (still 1 runner shared across cases).
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
                fake_runner.classified == ["AAPL $310", "AAPL $310"],
                repr(fake_runner.classified),
            )
        )
        results.append(
            CheckResult(
                "memory reads",
                fake_runner.memory_reads == [None, "preference", "fact", "convention"],
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
    finally:
        for restore in reversed(restorers):
            restore()

    ok = print_results(results)
    print("command harness ok" if ok else "command harness failed")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline channel-agnostic command dispatch harness.")
    parser.parse_args()
    raise SystemExit(asyncio.run(run_command_harness()))


if __name__ == "__main__":
    main()
