#!/usr/bin/env python3
"""jobs_progress_mode_smoke.py — CONVEYOR_PROGRESS_MODE filter tests.

Pins:
  - Config: default is "compact"; invalid values fall back to
    "compact" with a logged warning; verbose/quiet recognized.
  - Streaming filter: in compact mode, agent prose is dropped from
    the chat (no on_progress call); in quiet mode, every
    intermediate is dropped.
  - handlers/jobs progress(): in compact mode, prose progress is
    not surfaced to the port; in quiet mode, NOTHING except the
    final summary is surfaced.
  - Fallback: when edit_progress fails once in compact mode, at
    most one COMPACT_FALLBACK_TEXT is sent. In quiet mode, NO
    fallback. In verbose mode, every progress becomes a fresh
    send_new (legacy behavior preserved).
  - Feishu-like port (edit_progress always False): compact + quiet
    do not spam. The final summary still goes out exactly once.

Run: .venv/bin/python scripts/jobs_progress_mode_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from channel import InboundMessage  # noqa: E402
from config import (  # noqa: E402
    DEFAULT_PROGRESS_MODE,
    VALID_PROGRESS_MODES,
    _progress_mode_env,
)
from handlers.jobs import (  # noqa: E402
    COMPACT_FALLBACK_TEXT,
    PLACEHOLDER_TEXT,
    _is_prose_progress_text,
    _normalize_mode,
    handle_codex_job,
)
from runner import JobMode  # noqa: E402
from scripts.harness_common import CheckResult, print_results  # noqa: E402


# ---- helpers -------------------------------------------------------------


@dataclass
class FakeOutbound:
    """Outbound port stand-in. ``edit_works`` toggles the edit_progress
    contract: True (Telegram-like) vs False (Feishu-like)."""

    replies: list[str] = field(default_factory=list)
    sent_new: list[str] = field(default_factory=list)
    edits: list[tuple[str, str]] = field(default_factory=list)
    edit_works: bool = False

    async def reply(self, msg, text):
        self.replies.append(text)
        return "placeholder-1"

    async def send_new(self, msg, text):
        self.sent_new.append(text)
        return f"sent-{len(self.sent_new)}"

    async def edit_progress(self, msg, placeholder_id, text):
        self.edits.append((placeholder_id, text))
        return self.edit_works

    async def reply_with_buttons(self, msg, text, buttons):
        self.sent_new.append(text)
        return "btn-1"


def _msg() -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        operator_id="12345",
        chat_id="67890",
        message_id=None,
        text="",
    )


def _fake_runner(*, summary: str, progress_text: str | None,
                 progress_mode: str) -> tuple[object, object]:
    """Build a stub CodexRunner that returns a Job with the given
    summary and fires the given progress_text once via on_progress.
    Honors ``progress_mode`` so we can flip it per test without
    monkey-patching the module."""
    job = SimpleNamespace(
        state="COMPLETED",
        summary=summary,
        error=None,
        last_event=None,
    )
    settings = SimpleNamespace(
        conveyor_progress_mode=progress_mode,
        codex_memory_root=Path("/tmp/codex-progress-mem"),
        conveyor_session_enabled=False,
    )

    class _Stub:
        def __init__(self) -> None:
            self.settings = settings

        async def start(self, mode, prompt, on_progress):
            if progress_text is not None:
                await on_progress(progress_text)
            return job

    return _Stub(), job


def _run(coro):
    return asyncio.run(coro)


# ---- config tests --------------------------------------------------------


def _test_default_mode() -> CheckResult:
    return CheckResult(
        "config: DEFAULT_PROGRESS_MODE is 'compact'",
        DEFAULT_PROGRESS_MODE == "compact",
        f"got {DEFAULT_PROGRESS_MODE!r}",
    )


def _test_valid_modes() -> CheckResult:
    return CheckResult(
        "config: VALID_PROGRESS_MODES is ('verbose', 'compact', 'quiet')",
        set(VALID_PROGRESS_MODES) == {"verbose", "compact", "quiet"},
        f"got {set(VALID_PROGRESS_MODES)}",
    )


def _test_progress_mode_env_recognized() -> CheckResult:
    name = "config: _progress_mode_env: verbose/compact/quiet recognized"
    for v in ("verbose", "compact", "quiet"):
        os.environ["CONVEYOR_PROGRESS_MODE"] = v
        got = _progress_mode_env("CONVEYOR_PROGRESS_MODE", DEFAULT_PROGRESS_MODE)
        if got != v:
            return CheckResult(name, False, f"{v!r} -> {got!r}")
    return CheckResult(name, True, "all 3 recognized")


def _test_progress_mode_env_invalid_falls_back() -> CheckResult:
    name = "config: _progress_mode_env: unknown value falls back to default (with warning)"
    os.environ["CONVEYOR_PROGRESS_MODE"] = "noisy"
    got = _progress_mode_env("CONVEYOR_PROGRESS_MODE", DEFAULT_PROGRESS_MODE)
    return CheckResult(name, got == DEFAULT_PROGRESS_MODE, f"got {got!r}")


def _test_progress_mode_env_empty() -> CheckResult:
    name = "config: _progress_mode_env: empty/missing → default"
    os.environ.pop("CONVEYOR_PROGRESS_MODE", None)
    got = _progress_mode_env("CONVEYOR_PROGRESS_MODE", DEFAULT_PROGRESS_MODE)
    return CheckResult(name, got == DEFAULT_PROGRESS_MODE, f"got {got!r}")


def _test_normalize_mode() -> CheckResult:
    name = "handlers.jobs: _normalize_mode: unknown → 'compact'"
    for raw, want in [
        (None, "compact"),
        ("", "compact"),
        ("verbose", "verbose"),
        ("compact", "compact"),
        ("quiet", "quiet"),
        ("nonsense", "compact"),
    ]:
        got = _normalize_mode(raw)
        if got != want:
            return CheckResult(name, False, f"{raw!r} -> {got!r}, want {want!r}")
    return CheckResult(name, True, "all 6 cases match")


# ---- prose classification tests ------------------------------------------


def _test_prose_classifier() -> CheckResult:
    name = "handlers.jobs: _is_prose_progress_text: tool / thought are not prose"
    cases = [
        ("我这就帮你查一下。", True),
        ("⏳ 收到", True),
        ("🔧 curl...", False),
        ("💭 thinking...", False),
        ("🔧 curl (3s)...", False),
        ("", False),
    ]
    for text, want in cases:
        got = _is_prose_progress_text(text)
        if got != want:
            return CheckResult(name, False, f"{text!r} -> {got}, want {want}")
    return CheckResult(name, True, f"6 cases matched")


# ---- handlers/jobs mode behavior tests ----------------------------------


def _test_compact_drops_prose_progress() -> CheckResult:
    name = "handlers.jobs: compact mode drops prose progress (no send_new)"
    port = FakeOutbound()
    runner, _ = _fake_runner(
        summary="ANSWER", progress_text="我这就帮你查一下。", progress_mode="compact",
    )
    _run(handle_codex_job(_msg(), port, runner, mode=JobMode.RUN, prompt="x"))
    return CheckResult(
        name,
        port.sent_new == ["ANSWER"],
        f"sent_new={port.sent_new}",
    )


def _test_compact_keeps_tool_indicator() -> CheckResult:
    name = "handlers.jobs: compact mode keeps tool/thinking indicators (via edit_progress)"
    port = FakeOutbound(edit_works=True)  # Telegram-like: edit succeeds
    runner, _ = _fake_runner(
        summary="ANSWER", progress_text="🔧 curl...", progress_mode="compact",
    )
    _run(handle_codex_job(_msg(), port, runner, mode=JobMode.RUN, prompt="x"))
    # When edit_progress succeeds, the tool indicator is recorded
    # in ``edits`` (not ``sent_new``). The final summary still
    # goes out via send_new. So sent_new has exactly the final
    # summary and edits has the tool indicator.
    return CheckResult(
        name,
        port.sent_new == ["ANSWER"] and port.edits == [("placeholder-1", "🔧 curl...")],
        f"sent_new={port.sent_new} edits={port.edits}",
    )


def _test_quiet_drops_all_intermediate() -> CheckResult:
    name = "handlers.jobs: quiet mode drops all intermediate progress"
    port = FakeOutbound()
    runner, _ = _fake_runner(
        summary="ANSWER", progress_text="我这就帮你查一下。", progress_mode="quiet",
    )
    _run(handle_codex_job(_msg(), port, runner, mode=JobMode.RUN, prompt="x"))
    return CheckResult(
        name,
        port.sent_new == ["ANSWER"],
        f"sent_new={port.sent_new}",
    )


def _test_verbose_preserves_old_behavior() -> CheckResult:
    name = "handlers.jobs: verbose mode preserves old 'prose becomes send_new' behavior"
    port = FakeOutbound()
    runner, _ = _fake_runner(
        summary="ANSWER", progress_text="我这就帮你查一下。", progress_mode="verbose",
    )
    _run(handle_codex_job(_msg(), port, runner, mode=JobMode.RUN, prompt="x"))
    return CheckResult(
        name,
        port.sent_new == ["我这就帮你查一下。", "ANSWER"],
        f"sent_new={port.sent_new}",
    )


def _test_compact_fallback_one_message() -> CheckResult:
    name = "handlers.jobs: compact mode sends at most one fallback after edit failure"
    port = FakeOutbound(edit_works=False)  # Feishu-like: every edit fails
    runner, _ = _fake_runner(
        summary="ANSWER",
        progress_text="我这就帮你查一下。",  # prose, would be dropped in compact
        progress_mode="compact",
    )
    _run(handle_codex_job(_msg(), port, runner, mode=JobMode.RUN, prompt="x"))
    # In compact mode the prose is dropped before we even try to edit
    # or send_new. So the only send_new is the final ANSWER.
    return CheckResult(
        name,
        port.sent_new == ["ANSWER"],
        f"sent_new={port.sent_new}",
    )


def _test_verbose_spams_after_edit_failure() -> CheckResult:
    name = "handlers.jobs: verbose mode still spams send_new after edit failure (legacy behavior)"
    port = FakeOutbound(edit_works=False)
    runner, _ = _fake_runner(
        summary="ANSWER",
        progress_text="intermediate",
        progress_mode="verbose",
    )
    _run(handle_codex_job(_msg(), port, runner, mode=JobMode.RUN, prompt="x"))
    # verbose + edit failure + 1 progress = 1 send_new of the progress,
    # then post-loop dedupe compares the (truncated) progress vs the
    # (truncated) summary. They differ, so the final ANSWER is also
    # sent. The takeaway is: verbose is the *noisy* mode by design.
    return CheckResult(
        name,
        port.sent_new == ["intermediate", "ANSWER"],
        f"sent_new={port.sent_new}",
    )


def _test_feishu_compact_no_spam() -> CheckResult:
    name = "handlers/jobs: Feishu-like port (edit never works) + compact → no spam"
    port = FakeOutbound(edit_works=False)
    runner, _ = _fake_runner(
        summary="ANSWER", progress_text="intermediate", progress_mode="compact",
    )
    _run(handle_codex_job(_msg(), port, runner, mode=JobMode.RUN, prompt="x"))
    # compact drops the prose 'intermediate' before any send happens,
    # so we get exactly one send_new: the final summary.
    return CheckResult(
        name,
        port.sent_new == ["ANSWER"],
        f"sent_new={port.sent_new}",
    )


def _test_feishu_quiet_no_spam() -> CheckResult:
    name = "handlers/jobs: Feishu-like port + quiet → only the final summary"
    port = FakeOutbound(edit_works=False)
    runner, _ = _fake_runner(
        summary="ANSWER", progress_text="intermediate", progress_mode="quiet",
    )
    _run(handle_codex_job(_msg(), port, runner, mode=JobMode.RUN, prompt="x"))
    return CheckResult(
        name,
        port.sent_new == ["ANSWER"],
        f"sent_new={port.sent_new}",
    )


def _test_placeholder_still_sent() -> CheckResult:
    name = "handlers/jobs: placeholder is always sent first, regardless of mode"
    for mode in ("verbose", "compact", "quiet"):
        port = FakeOutbound()
        runner, _ = _fake_runner(summary="A", progress_text="x", progress_mode=mode)
        _run(handle_codex_job(_msg(), port, runner, mode=JobMode.RUN, prompt="hi"))
        if not port.replies or port.replies[0] != PLACEHOLDER_TEXT:
            return CheckResult(name, False, f"mode={mode} replies={port.replies}")
    return CheckResult(name, True, "placeholder sent in all 3 modes")


# ---- compact fallback after edit failure on a tool indicator ------------


def _test_compact_tool_indicator_fallback() -> CheckResult:
    """Compact mode keeps tool indicators. If the channel cannot
    edit_progress, the first tool indicator should still surface
    (compact does not drop tool), but as exactly one send_new of
    the COMPACT_FALLBACK_TEXT, not a fresh bubble per call."""
    name = "handlers/jobs: compact + Feishu + tool indicator → one fallback line, no spam"
    port = FakeOutbound(edit_works=False)
    # We need TWO progress calls to exercise the spam: first turns
    # on edit_broken, second would normally spam.
    job = SimpleNamespace(state="COMPLETED", summary="ANSWER", error=None, last_event=None)
    settings = SimpleNamespace(
        conveyor_progress_mode="compact",
        codex_memory_root=Path("/tmp/codex-progress-mem"),
        conveyor_session_enabled=False,
    )

    call_count = {"n": 0}

    class _Stub:
        def __init__(self) -> None:
            self.settings = settings

        async def start(self, mode, prompt, on_progress):
            await on_progress("🔧 curl...")
            await on_progress("🔧 curl...")
            await on_progress("🔧 curl...")
            return job

    _run(handle_codex_job(_msg(), port, _Stub(), mode=JobMode.RUN, prompt="x"))
    # The first tool call: edit fails → latch + send COMPACT_FALLBACK_TEXT.
    # The second + third tool calls: edit_broken already True, so we
    # would normally resend. The new logic must cap at one fallback.
    return CheckResult(
        name,
        port.sent_new == [COMPACT_FALLBACK_TEXT, "ANSWER"],
        f"sent_new={port.sent_new}",
    )


def _test_quiet_no_fallback_after_edit_failure() -> CheckResult:
    name = "handlers/jobs: quiet + Feishu → no fallback line at all, only final"
    port = FakeOutbound(edit_works=False)
    job = SimpleNamespace(state="COMPLETED", summary="ANSWER", error=None, last_event=None)
    settings = SimpleNamespace(
        conveyor_progress_mode="quiet",
        codex_memory_root=Path("/tmp/codex-progress-mem"),
        conveyor_session_enabled=False,
    )

    class _Stub:
        def __init__(self) -> None:
            self.settings = settings

        async def start(self, mode, prompt, on_progress):
            await on_progress("🔧 curl...")
            await on_progress("🔧 curl...")
            return job

    _run(handle_codex_job(_msg(), port, _Stub(), mode=JobMode.RUN, prompt="x"))
    return CheckResult(
        name,
        port.sent_new == ["ANSWER"],
        f"sent_new={port.sent_new}",
    )


# ---- compact fallback text constant --------------------------------------


def _test_fallback_text_constant() -> CheckResult:
    name = "handlers.jobs: COMPACT_FALLBACK_TEXT is a short Chinese status line"
    return CheckResult(
        name,
        COMPACT_FALLBACK_TEXT == "仍在处理...",
        f"got {COMPACT_FALLBACK_TEXT!r}",
    )


# ---- streaming-layer mode filter tests -----------------------------------
# These exercise runner/streaming.py::_read_jsonl_stdout directly,
# without the handlers/jobs.py progress() wrapper. They verify that
# the streaming layer itself respects CONVEYOR_PROGRESS_MODE.


def _run_streaming(mode: str, lines: list[dict]) -> list[str]:
    """Run ``_read_jsonl_stdout`` with the given progress mode and
    return the list of texts that were forwarded to ``on_progress``."""
    import runner as runner_mod  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        for sub in ("ws", "task", "memory"):
            (tmp_p / sub).mkdir(parents=True, exist_ok=True)
        overrides = {
            "TELEGRAM_BOT_TOKEN": "fake-token",
            "TELEGRAM_ALLOWED_USER_ID": "0",
            "CODEX_WORKSPACE_ROOT": str(tmp_p / "ws"),
            "CODEX_TASK_ROOT": str(tmp_p / "task"),
            "CODEX_MEMORY_ROOT": str(tmp_p / "memory"),
            "CODEX_BIN": "codex",
            "USER_TIMEZONE": "UTC",
        }
        with mock.patch.dict(os.environ, overrides, clear=False):
            settings = runner_mod.load_settings()
            object.__setattr__(settings, "telegram_progress_seconds", 0.0)
            object.__setattr__(settings, "conveyor_progress_mode", mode)
            r = runner_mod.CodexRunner(settings)

            log_path = tmp_p / "job.jsonl"
            job = SimpleNamespace(log_path=log_path, last_event="starting")

            chunks = []
            for obj in lines:
                chunks.append((json.dumps(obj) + "\n").encode("utf-8"))

            class _FakeStdout:
                def __init__(self, data):
                    self._data = list(data)

                async def readline(self):
                    return self._data.pop(0) if self._data else b""

            process = SimpleNamespace(stdout=_FakeStdout(chunks))
            progress_calls: list[str] = []

            async def _on_progress(text):
                progress_calls.append(text)

            asyncio.run(r._read_jsonl_stdout(job, process, _on_progress))
            return progress_calls


_TOOL_LINE = {
    "type": "item.completed",
    "item": {"type": "function_call", "name": "curl"},
}
_PROSE_LINE = {
    "type": "item.updated",
    "item": {"type": "agent_message", "text": "我这就帮你查一下。"},
}


def _test_streaming_quiet_suppresses_tool() -> CheckResult:
    name = "streaming: quiet mode suppresses tool indicator (no on_progress call)"
    calls = _run_streaming("quiet", [_TOOL_LINE])
    return CheckResult(
        name,
        len(calls) == 0,
        f"expected 0 calls, got {len(calls)}: {calls!r}",
    )


def _test_streaming_compact_keeps_tool() -> CheckResult:
    name = "streaming: compact mode keeps tool indicator (on_progress called)"
    calls = _run_streaming("compact", [_TOOL_LINE])
    return CheckResult(
        name,
        len(calls) == 1 and "🔧 curl" in calls[0],
        f"expected 1 call with tool indicator, got {len(calls)}: {calls!r}",
    )


def _test_streaming_verbose_keeps_prose() -> CheckResult:
    name = "streaming: verbose mode keeps prose (on_progress called)"
    calls = _run_streaming("verbose", [_PROSE_LINE])
    return CheckResult(
        name,
        len(calls) == 1 and "我这就帮你查一下" in calls[0],
        f"expected 1 call with prose, got {len(calls)}: {calls!r}",
    )


def _test_streaming_compact_drops_prose() -> CheckResult:
    name = "streaming: compact mode drops prose (no on_progress call)"
    calls = _run_streaming("compact", [_PROSE_LINE])
    return CheckResult(
        name,
        len(calls) == 0,
        f"expected 0 calls, got {len(calls)}: {calls!r}",
    )


CHECKS = [
    _test_default_mode,
    _test_valid_modes,
    _test_progress_mode_env_recognized,
    _test_progress_mode_env_invalid_falls_back,
    _test_progress_mode_env_empty,
    _test_normalize_mode,
    _test_prose_classifier,
    _test_compact_drops_prose_progress,
    _test_compact_keeps_tool_indicator,
    _test_quiet_drops_all_intermediate,
    _test_verbose_preserves_old_behavior,
    _test_compact_fallback_one_message,
    _test_verbose_spams_after_edit_failure,
    _test_feishu_compact_no_spam,
    _test_feishu_quiet_no_spam,
    _test_placeholder_still_sent,
    _test_compact_tool_indicator_fallback,
    _test_quiet_no_fallback_after_edit_failure,
    _test_fallback_text_constant,
    _test_streaming_quiet_suppresses_tool,
    _test_streaming_compact_keeps_tool,
    _test_streaming_verbose_keeps_prose,
    _test_streaming_compact_drops_prose,
]


def main() -> int:
    # Clear CONVEYOR_PROGRESS_MODE so env-var tests exercise
    # the loader with predictable values, not whatever the
    # developer's .env has.
    os.environ.pop("CONVEYOR_PROGRESS_MODE", None)
    results = [t() for t in CHECKS]
    print_results(results)
    ok = all(r.ok for r in results)
    print("jobs progress mode smoke ok" if ok else "jobs progress mode smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
