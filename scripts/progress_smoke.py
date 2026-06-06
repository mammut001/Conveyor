#!/usr/bin/env python3
"""Regression guard for the Tier 1 chat-bot feel contract.

Tier 1 (bot.py:_start_job) promises the user a sub-second ack, evolving
in-place progress, and a final answer that lands on the same placeholder
message. Tier 1 (runner.py:CodexRunner) promises to forward only
user-readable codex events to that progress callback.

These tests are AST-only for the static shape, plus behavioral checks
using mocks on the bot module and a stubbed runner. No HTTP, no real
LLM, no real codex, no Telegram network.

Run with:  .venv/bin/python scripts/progress_smoke.py
Exit code: 0 if all pass, 1 otherwise.
"""
from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.harness_common import CheckResult, print_results


RUNNER_PY = Path(__file__).resolve().parents[1] / "runner.py"
BOT_PY = Path(__file__).resolve().parents[1] / "bot.py"


# ---- AST helpers ---------------------------------------------------------

def _parse_runner() -> ast.Module:
    return ast.parse(RUNNER_PY.read_text(encoding="utf-8"))


def _class_method(tree: ast.Module, class_name: str, method_name: str) -> ast.FunctionDef | None:
    """Find a method by name on a class definition at module level."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return item
    return None


def _signature_str(func: ast.FunctionDef) -> str:
    returns = ast.unparse(func.returns) if func.returns is not None else "None"
    return f"{ast.unparse(func.args)} -> {returns}"


# ---- env + bot fixture ---------------------------------------------------

@contextmanager
def _bot_in_tmp_env(tmp: Path):
    """Import bot.py with env vars pointing at tmp paths; restore on exit.

    Mirrors the pattern in scripts/memo_fastpath_smoke.py.
    """
    overrides = {
        "TELEGRAM_BOT_TOKEN": "fake-token",
        "TELEGRAM_ALLOWED_USER_ID": "0",
        "CODEX_WORKSPACE_ROOT": str(tmp / "ws"),
        "CODEX_TASK_ROOT": str(tmp / "task"),
        "CODEX_MEMORY_ROOT": str(tmp / "memory"),
        "CODEX_BIN": "codex",
        "USER_TIMEZONE": "UTC",
    }
    with mock.patch.dict(os.environ, overrides, clear=False):
        import bot  # type: ignore[import-not-found]  # noqa: PLC0415
        yield bot


def _make_fake_app() -> mock.MagicMock:
    """Build a fake Telegram bot app with async send/edit/action methods."""
    app = mock.MagicMock()
    app.send_message = mock.AsyncMock(return_value=SimpleNamespace(message_id=1))
    app.edit_message_text = mock.AsyncMock()
    app.send_chat_action = mock.AsyncMock()
    return app


def _make_fake_update(
    app: mock.MagicMock,
    placeholder_id: int = 99,
    chat_id: int = 4242,
) -> mock.MagicMock:
    """Build a fake telegram Update with chat_id, reply_text, and get_bot."""
    update = mock.MagicMock()
    update.effective_chat = mock.MagicMock()
    update.effective_chat.id = chat_id
    update.effective_message = mock.MagicMock()
    update.effective_message.reply_text = mock.AsyncMock(
        return_value=SimpleNamespace(message_id=placeholder_id),
    )
    update.get_bot = mock.MagicMock(return_value=app)
    return update


def _build_fake_runner(progress_texts: list[str] | None = None) -> mock.MagicMock:
    """Build a fake runner whose start() calls progress(text) for each text."""
    texts = list(progress_texts or [])

    async def fake_start(mode, prompt, progress):
        for text in texts:
            await progress(text)
        return SimpleNamespace(id="job-fake")

    runner = mock.MagicMock()
    runner.start = fake_start
    return runner


# ---- AST tests on runner.py ---------------------------------------------

def _test_is_user_visible_event_exists() -> CheckResult:
    name = "AST: _is_user_visible_event is FunctionDef on CodexRunner"
    try:
        method = _class_method(_parse_runner(), "CodexRunner", "_is_user_visible_event")
        if not isinstance(method, ast.FunctionDef):
            return CheckResult(name, False, "method missing on CodexRunner class")
        return CheckResult(name, True, f"signature: {_signature_str(method)}")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_is_user_visible_event_keys() -> CheckResult:
    name = 'AST: _is_user_visible_event body references "message", "summary", "text", "delta"'
    try:
        method = _class_method(_parse_runner(), "CodexRunner", "_is_user_visible_event")
        if not isinstance(method, ast.FunctionDef):
            return CheckResult(name, False, "method missing (caught by previous test)")
        # Walk only the body, not nested defs (none expected here, but be safe).
        found: set[str] = set()
        for node in ast.walk(method):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in ("message", "summary", "text", "delta"):
                    found.add(node.value)
        needed = {"message", "summary", "text", "delta"}
        missing = needed - found
        if missing:
            return CheckResult(name, False, f"missing string keys: {sorted(missing)}; found {sorted(found)}")
        return CheckResult(name, True, "all 4 string keys present in body")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_should_send_event_progress_signature() -> CheckResult:
    name = "AST: _should_send_event_progress signature is (self, event_text: str, event_obj: dict | None = None) -> bool"
    try:
        method = _class_method(_parse_runner(), "CodexRunner", "_should_send_event_progress")
        if not isinstance(method, ast.FunctionDef):
            return CheckResult(name, False, "method missing on CodexRunner class")
        actual = _signature_str(method)
        # ast.unparse normalises default-value spacing to PEP 8 (no spaces
        # around =), so the rendered signature has `dict | None=None`.
        expected = "self, event_text: str, event_obj: dict | None=None -> bool"
        if actual != expected:
            return CheckResult(name, False, f"signature mismatch: got {actual!r}, expected {expected!r}")
        return CheckResult(name, True, actual)
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_should_send_event_progress_delegates() -> CheckResult:
    name = "AST: _should_send_event_progress body delegates to self._is_user_visible_event"
    try:
        method = _class_method(_parse_runner(), "CodexRunner", "_should_send_event_progress")
        if not isinstance(method, ast.FunctionDef):
            return CheckResult(name, False, "method missing")
        calls = [
            n for n in ast.walk(method)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "self"
            and n.func.attr == "_is_user_visible_event"
        ]
        if not calls:
            return CheckResult(name, False, "no self._is_user_visible_event(...) call in body")
        if len(calls) > 1:
            return CheckResult(name, False, f"expected 1 delegation call, got {len(calls)}")
        return CheckResult(name, True, "body delegates via self._is_user_visible_event(...)")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- behavior tests on bot._start_job -----------------------------------

def _test_placeholder_sent_once() -> CheckResult:
    name = 'behavior: _start_job sends exactly one reply_text("⏳ Got it, working on it...")'
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            for sub in ("ws", "task", "memory"):
                (tmp_p / sub).mkdir(parents=True, exist_ok=True)
            with _bot_in_tmp_env(tmp_p) as bot:
                app = _make_fake_app()
                update = _make_fake_update(app, placeholder_id=99)
                fake_runner = _build_fake_runner(progress_texts=[])
                with mock.patch.object(bot, "runner", fake_runner):
                    asyncio.run(bot._start_job(update, bot.JobMode.RUN, "test prompt"))
                # reply_text was called once with the placeholder text.
                rt = update.effective_message.reply_text
                if rt.call_count != 1:
                    return CheckResult(
                        name, False,
                        f"expected 1 reply_text call, got {rt.call_count}; calls={rt.call_args_list}",
                    )
                sent_text = rt.call_args[0][0]
                if sent_text != "⏳ Got it, working on it...":
                    return CheckResult(name, False, f"wrong text: {sent_text!r}")
                return CheckResult(
                    name, True,
                    "1 reply_text(\"⏳ Got it, working on it...\") call",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_progress_uses_edit() -> CheckResult:
    name = "behavior: 3 progress() calls -> 3 edit_message_text to (chat_id=4242, message_id=99), 0 send_message"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            for sub in ("ws", "task", "memory"):
                (tmp_p / sub).mkdir(parents=True, exist_ok=True)
            with _bot_in_tmp_env(tmp_p) as bot:
                app = _make_fake_app()
                update = _make_fake_update(app, placeholder_id=99, chat_id=4242)
                fake_runner = _build_fake_runner(progress_texts=["P1", "P2", "P3"])
                with mock.patch.object(bot, "runner", fake_runner):
                    asyncio.run(bot._start_job(update, bot.JobMode.RUN, "test prompt"))
                if app.edit_message_text.call_count != 3:
                    return CheckResult(
                        name, False,
                        f"expected 3 edit_message_text calls, got {app.edit_message_text.call_count}",
                    )
                if app.send_message.call_count != 0:
                    return CheckResult(
                        name, False,
                        f"expected 0 send_message calls, got {app.send_message.call_count}",
                    )
                # Each edit addressed to (chat_id=4242, message_id=99)
                for idx, call in enumerate(app.edit_message_text.call_args_list):
                    kwargs = call.kwargs
                    if kwargs.get("chat_id") != 4242 or kwargs.get("message_id") != 99:
                        return CheckResult(
                            name, False,
                            f"edit #{idx} wrong target: {kwargs}",
                        )
                return CheckResult(
                    name, True,
                    "3 edits to (4242, 99), 0 sends",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_typing_loop_cancelled() -> CheckResult:
    name = "behavior: typing loop task created and done() after _start_job returns; send_chat_action called"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            for sub in ("ws", "task", "memory"):
                (tmp_p / sub).mkdir(parents=True, exist_ok=True)
            with _bot_in_tmp_env(tmp_p) as bot:
                app = _make_fake_app()
                update = _make_fake_update(app, placeholder_id=99)
                # Slow the placeholder reply so the typing loop has time to
                # call send_chat_action at least once before being cancelled.

                async def slow_reply(*args, **kwargs):
                    await asyncio.sleep(0.05)
                    return SimpleNamespace(message_id=99)

                update.effective_message.reply_text = mock.AsyncMock(side_effect=slow_reply)
                # Track every task the bot spawns via asyncio.create_task.
                tracked: list[asyncio.Task] = []
                real_create_task = asyncio.create_task

                def tracking_create_task(coro, *args, **kwargs):
                    task = real_create_task(coro, *args, **kwargs)
                    tracked.append(task)
                    return task

                fake_runner = _build_fake_runner(progress_texts=["P1"])
                with mock.patch.object(bot, "runner", fake_runner), \
                     mock.patch.object(asyncio, "create_task", tracking_create_task):
                    asyncio.run(bot._start_job(update, bot.JobMode.RUN, "test prompt"))
                # At least one task was created.
                if not tracked:
                    return CheckResult(name, False, "asyncio.create_task was never called")
                # All created tasks are done() (cancelled or finished).
                not_done = [t for t in tracked if not t.done()]
                if not_done:
                    return CheckResult(
                        name, False,
                        f"{len(not_done)} of {len(tracked)} created tasks still running",
                    )
                # send_chat_action was called at least once while we slept.
                if app.send_chat_action.call_count < 1:
                    return CheckResult(
                        name, False,
                        f"send_chat_action not called; calls={app.send_chat_action.call_args_list}",
                    )
                return CheckResult(
                    name, True,
                    f"{len(tracked)} task(s) created, all done(); "
                    f"send_chat_action called {app.send_chat_action.call_count} time(s)",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_final_answer_via_edit() -> CheckResult:
    name = "behavior: last progress() text appears as the final edit_message_text"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            for sub in ("ws", "task", "memory"):
                (tmp_p / sub).mkdir(parents=True, exist_ok=True)
            with _bot_in_tmp_env(tmp_p) as bot:
                app = _make_fake_app()
                update = _make_fake_update(app, placeholder_id=99)
                fake_runner = _build_fake_runner(
                    progress_texts=["earlier", "middle", "FINAL_ANSWER_TEXT"],
                )
                with mock.patch.object(bot, "runner", fake_runner):
                    asyncio.run(bot._start_job(update, bot.JobMode.RUN, "test prompt"))
                if app.edit_message_text.call_count != 3:
                    return CheckResult(
                        name, False,
                        f"expected 3 edits, got {app.edit_message_text.call_count}",
                    )
                last_call = app.edit_message_text.call_args_list[-1]
                last_text = last_call.kwargs.get("text")
                if last_text != "FINAL_ANSWER_TEXT":
                    return CheckResult(
                        name, False,
                        f"last edit text mismatch: {last_text!r}",
                    )
                return CheckResult(
                    name, True,
                    "last edit_message_text.text == 'FINAL_ANSWER_TEXT'",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_edit_failure_falls_back_to_send() -> CheckResult:
    name = "behavior: edit_message_text raises -> progress falls back to send_message with the same text"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            for sub in ("ws", "task", "memory"):
                (tmp_p / sub).mkdir(parents=True, exist_ok=True)
            with _bot_in_tmp_env(tmp_p) as bot:
                app = _make_fake_app()
                update = _make_fake_update(app, placeholder_id=99)
                # Force edit to fail; send should be called instead.
                app.edit_message_text = mock.AsyncMock(side_effect=Exception("edit boom"))
                fake_runner = _build_fake_runner(progress_texts=["FALLBACK_TEXT"])
                with mock.patch.object(bot, "runner", fake_runner):
                    asyncio.run(bot._start_job(update, bot.JobMode.RUN, "test prompt"))
                if app.send_message.call_count != 1:
                    return CheckResult(
                        name, False,
                        f"expected 1 send_message fallback, got {app.send_message.call_count}",
                    )
                sent_text = app.send_message.call_args.kwargs.get("text")
                if sent_text != "FALLBACK_TEXT":
                    return CheckResult(
                        name, False,
                        f"fallback text mismatch: {sent_text!r}",
                    )
                return CheckResult(
                    name, True,
                    "edit raised -> 1 send_message(text='FALLBACK_TEXT')",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_placeholder_send_failure_falls_back() -> CheckResult:
    name = "behavior: placeholder reply_text raises -> progress uses send_message, not edit"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            for sub in ("ws", "task", "memory"):
                (tmp_p / sub).mkdir(parents=True, exist_ok=True)
            with _bot_in_tmp_env(tmp_p) as bot:
                app = _make_fake_app()
                update = _make_fake_update(app, placeholder_id=99)
                # Force placeholder send to fail. _start_job must NOT
                # edit_message_text (placeholder_id stays None), and any
                # progress() must fall through to send_message.
                update.effective_message.reply_text = mock.AsyncMock(
                    side_effect=Exception("placeholder boom"),
                )
                fake_runner = _build_fake_runner(progress_texts=["PLAINTEXT_PROGRESS"])
                with mock.patch.object(bot, "runner", fake_runner):
                    asyncio.run(bot._start_job(update, bot.JobMode.RUN, "test prompt"))
                if app.edit_message_text.call_count != 0:
                    return CheckResult(
                        name, False,
                        f"edit_message_text called {app.edit_message_text.call_count} time(s); expected 0",
                    )
                if app.send_message.call_count != 1:
                    return CheckResult(
                        name, False,
                        f"expected 1 send_message, got {app.send_message.call_count}",
                    )
                sent_text = app.send_message.call_args.kwargs.get("text")
                if sent_text != "PLAINTEXT_PROGRESS":
                    return CheckResult(
                        name, False,
                        f"send text mismatch: {sent_text!r}",
                    )
                return CheckResult(
                    name, True,
                    "placeholder raise -> 0 edits, 1 send_message(text='PLAINTEXT_PROGRESS')",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- chat-feel round-7: no-op edit skip -------------------------------

def _test_no_op_edit_skipped() -> CheckResult:
    """Two consecutive ``progress()`` calls with byte-identical text
    must result in exactly one ``edit_message_text`` call and zero
    ``send_message`` calls. Telegram's ``editMessageText`` 400s with
    "Message is not modified" when the new content matches the
    current content, and the existing ``except`` latch flips the rest
    of the job into ``send_message`` mode (scattering one-off
    messages through the chat). The round-7 ``last_progress_text``
    guard short-circuits the second call before the wire."""
    name = "behavior: progress() skips a no-op edit when the new text matches the last delivered text"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            for sub in ("ws", "task", "memory"):
                (tmp_p / sub).mkdir(parents=True, exist_ok=True)
            with _bot_in_tmp_env(tmp_p) as bot:
                app = _make_fake_app()
                update = _make_fake_update(app, placeholder_id=99)
                # Two identical texts. The first call should edit;
                # the second should be a no-op and NOT touch the
                # wire at all (no edit, no send).
                fake_runner = _build_fake_runner(
                    progress_texts=["SAME", "SAME"],
                )
                with mock.patch.object(bot, "runner", fake_runner):
                    asyncio.run(bot._start_job(update, bot.JobMode.RUN, "test prompt"))
                if app.edit_message_text.call_count != 1:
                    return CheckResult(
                        name, False,
                        f"expected exactly 1 edit_message_text; got {app.edit_message_text.call_count}",
                    )
                if app.send_message.call_count != 0:
                    return CheckResult(
                        name, False,
                        f"expected 0 send_message (no-op should not fall through); got {app.send_message.call_count}",
                    )
                return CheckResult(
                    name, True,
                    "2 identical progress() calls -> 1 edit, 0 send (2nd dedup'd by last_progress_text guard)",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_no_op_edit_skipped_after_truncation_collision() -> CheckResult:
    """Two different ``progress()`` payloads that produce the same
    wire text after ``truncate()`` must be deduped too. The wire
    format (post-truncation) is what Telegram compares, so the
    guard compares post-truncation text. This pins the contract
    that long-prose truncation collisions (a real risk when a
    long message lands at exactly the 3900-char cap twice) do
    not surface as ``BadRequest: Message is not modified`` storms."""
    name = "behavior: progress() skips a no-op edit when truncate() produces the same wire text"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            for sub in ("ws", "task", "memory"):
                (tmp_p / sub).mkdir(parents=True, exist_ok=True)
            with _bot_in_tmp_env(tmp_p) as bot:
                app = _make_fake_app()
                update = _make_fake_update(app, placeholder_id=99)
                # Two distinct inputs that the test's stubbed
                # truncate() collapses to the same wire string.
                fake_runner = _build_fake_runner(
                    progress_texts=["alpha", "beta"],
                )
                with mock.patch.object(
                    bot, "truncate",
                    lambda s, *a, **k: "SAME_TRUNCATED",
                ), mock.patch.object(bot, "runner", fake_runner):
                    asyncio.run(bot._start_job(update, bot.JobMode.RUN, "test prompt"))
                if app.edit_message_text.call_count != 1:
                    return CheckResult(
                        name, False,
                        f"expected exactly 1 edit_message_text; got {app.edit_message_text.call_count}",
                    )
                if app.send_message.call_count != 0:
                    return CheckResult(
                        name, False,
                        f"expected 0 send_message; got {app.send_message.call_count}",
                    )
                # The single edit must carry the post-truncation
                # text, not the original input.
                sent_text = app.edit_message_text.call_args.kwargs.get("text")
                if sent_text != "SAME_TRUNCATED":
                    return CheckResult(
                        name, False,
                        f"edit text mismatch: {sent_text!r}",
                    )
                return CheckResult(
                    name, True,
                    "2 distinct inputs collide on truncate() -> 1 edit ('SAME_TRUNCATED'), 0 send; "
                    "wire-format dedup pins the contract",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- main ----------------------------------------------------------------

def _test_agent_message_text_exists() -> CheckResult:
    name = "AST: _agent_message_text is FunctionDef on CodexRunner"
    try:
        method = _class_method(_parse_runner(), "CodexRunner", "_agent_message_text")
        if not isinstance(method, ast.FunctionDef):
            return CheckResult(name, False, "method missing on CodexRunner class")
        return CheckResult(name, True, f"signature: {_signature_str(method)}")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_agent_message_text_extraction() -> CheckResult:
    name = 'behavior: _agent_message_text surfaces item.text for agent_message, ignores other types'
    try:
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
                r = runner_mod.CodexRunner(settings)
                cases = [
                    ({"item": {"type": "agent_message", "text": "hello"}}, "hello"),
                    ({"item": {"type": "agent_message", "text": "  spaced  "}}, "spaced"),
                    ({"type": "item.updated", "item": {"type": "agent_message", "text": "chunk"}}, "chunk"),
                    ({"item": {"type": "reasoning", "text": "thinking"}}, None),
                    ({"item": {"type": "agent_message", "text": ""}}, None),
                    ({"item": {"type": "agent_message"}}, None),
                    ({"item": "not a dict"}, None),
                    ({"type": "turn.completed"}, None),
                    (None, None),
                ]
                results = []
                for event_obj, expected in cases:
                    actual = r._agent_message_text(event_obj)
                    results.append((event_obj, expected, actual))
                mismatches = [c for c in results if c[2] != c[1]]
                if mismatches:
                    return CheckResult(
                        name, False,
                        f"{len(mismatches)} mismatch(es): " + ", ".join(
                            f"in={c[0]!r} expected={c[1]!r} got={c[2]!r}"
                            for c in mismatches[:3]
                        ),
                    )
                return CheckResult(
                    name, True,
                    f"{len(cases)} cases (agent_message yes / other types no / None)",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_event_summary_streams_prose() -> CheckResult:
    name = 'behavior: _event_summary returns "item.updated: <prose>" for agent_message items, not item JSON'
    try:
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
                r = runner_mod.CodexRunner(settings)
                prose_line = json.dumps(
                    {"type": "item.updated", "item": {"type": "agent_message", "text": "streaming answer chunk"}}
                )
                summary = r._event_summary(prose_line)
                if "streaming answer chunk" not in summary:
                    return CheckResult(
                        name, False,
                        f"prose not surfaced; got {summary!r}",
                    )
                if summary.startswith("item.updated: {"):
                    return CheckResult(
                        name, False,
                        f"summary is still the raw item JSON: {summary!r}",
                    )
                # Non-prose items: tool calls now surface as a "🔧 name..."
                # progress line so the user sees the model is doing
                # something between prose events, not just frozen.
                tool_line = json.dumps(
                    {"type": "item.updated", "item": {"type": "function_call", "name": "shell"}}
                )
                tool_summary = r._event_summary(tool_line)
                if "🔧" not in tool_summary or "shell" not in tool_summary:
                    return CheckResult(
                        name, False,
                        f"tool item should surface as 🔧 indicator; got {tool_summary!r}",
                    )
                # And a truly opaque item (no agent_message text, no
                # function_call name) returns "" instead of dumping JSON.
                # The raw line still lives in job.log_path for debugging;
                # the chat surface stays clean.
                opaque_line = json.dumps(
                    {"type": "item.updated", "item": {"type": "custom_thing", "payload": 1}}
                )
                opaque_summary = r._event_summary(opaque_line)
                if opaque_summary != "":
                    return CheckResult(
                        name, False,
                        f"opaque item should return ''; got {opaque_summary!r}",
                    )
                return CheckResult(
                    name, True,
                    "agent_message -> prose; function_call -> 🔧 indicator; opaque -> ''",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_typing_loop_stays_alive() -> CheckResult:
    name = "behavior: typing loop continues past placeholder landing; send_chat_action called more than once during a slow job"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            for sub in ("ws", "task", "memory"):
                (tmp_p / sub).mkdir(parents=True, exist_ok=True)
            with _bot_in_tmp_env(tmp_p) as bot:
                app = _make_fake_app()
                update = _make_fake_update(app, placeholder_id=99)
                # Slow the placeholder reply so the typing loop has time
                # to call send_chat_action AT LEAST ONCE before the
                # placeholder lands. We then run a slow job so the typing
                # loop gets to call send_chat_action multiple more times.

                placeholder_count = 0

                async def slow_reply(*args, **kwargs):
                    nonlocal placeholder_count
                    await asyncio.sleep(0.05)
                    placeholder_count += 1
                    return SimpleNamespace(message_id=99)

                update.effective_message.reply_text = mock.AsyncMock(side_effect=slow_reply)

                # Slow runner: each progress() call yields to the loop so
                # the typing task can fire send_chat_action. After the
 # placeholder lands, the typing task should keep firing for the
 # rest of the job's lifetime (the change we're guarding).
                async def slow_progress_job(mode, prompt, progress):
                    # 3 progress calls with 0.6s gaps: total job time is
                    # ~1.8s after the placeholder lands, which is more
                    # than the 1.5s typing-loop interval so the loop
                    # fires at least one extra send_chat_action past the
                    # initial one.
                    for i in range(3):
                        await asyncio.sleep(0.6)  # let typing task run
                        await progress(f"step {i}")
                    return SimpleNamespace(id="job-fake")

                fake_runner = mock.MagicMock()
                fake_runner.start = slow_progress_job

                with mock.patch.object(bot, "runner", fake_runner):
                    asyncio.run(bot._start_job(update, bot.JobMode.RUN, "test prompt"))
                # With 3 progress calls and ~0.4s between them, the typing
 # task (which sleeps 4s per loop) will have fired send_chat_action
 # at least 2-3 times. Pre-fix this was 1 (only during the
 # placeholder phase). Allow some slack.
                calls = app.send_chat_action.call_count
                if calls < 2:
                    return CheckResult(
                        name, False,
                        f"send_chat_action called {calls} time(s); expected >= 2 (typing loop should outlive placeholder)",
                    )
                return CheckResult(
                    name, True,
                    f"send_chat_action called {calls} time(s) across placeholder + 3 progress steps",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_tool_call_name_exists() -> CheckResult:
    name = "AST: _tool_call_name is FunctionDef on CodexRunner"
    try:
        method = _class_method(_parse_runner(), "CodexRunner", "_tool_call_name")
        if not isinstance(method, ast.FunctionDef):
            return CheckResult(name, False, "method missing on CodexRunner class")
        return CheckResult(name, True, f"signature: {_signature_str(method)}")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_tool_call_name_extraction() -> CheckResult:
    name = "behavior: _tool_call_name extracts function_call/tool_call name, None for others"
    try:
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
                r = runner_mod.CodexRunner(settings)
                cases = [
                    # The common case: function_call item with a name.
                    ({"item": {"type": "function_call", "name": "shell"}}, "shell"),
                    # Tool name lives under "tool" or "function" instead.
                    ({"item": {"type": "tool_call", "tool": "memorize"}}, "memorize"),
                    ({"item": {"type": "tool_call", "function": "read_file"}}, "read_file"),
                    # Wrapped in a turn event envelope.
                    ({"type": "item.updated", "item": {"type": "function_call", "name": "web_search"}}, "web_search"),
                    # Not a tool event.
                    ({"item": {"type": "agent_message", "text": "hi"}}, None),
                    ({"item": {"type": "reasoning", "text": "..."}}, None),
                    ({"type": "turn.completed"}, None),
                    # Tool with no name should not surface a vague indicator.
                    ({"item": {"type": "function_call"}}, None),
                    ({"item": "not a dict"}, None),
                    (None, None),
                ]
                results = []
                for event_obj, expected in cases:
                    actual = r._tool_call_name(event_obj)
                    results.append((event_obj, expected, actual))
                mismatches = [c for c in results if c[2] != c[1]]
                if mismatches:
                    return CheckResult(
                        name, False,
                        f"{len(mismatches)} mismatch(es): " + ", ".join(
                            f"in={c[0]!r} expected={c[1]!r} got={c[2]!r}"
                            for c in mismatches[:3]
                        ),
                    )
                return CheckResult(
                    name, True,
                    f"{len(cases)} cases (function_call yes / other types no / None)",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_tool_call_event_is_user_visible() -> CheckResult:
    name = "behavior: _is_user_visible_event returns True for a function_call item with a name"
    try:
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
                r = runner_mod.CodexRunner(settings)
                visible = r._is_user_visible_event(
                    {"type": "item.updated", "item": {"type": "function_call", "name": "shell"}}
                )
                if not visible:
                    return CheckResult(
                        name, False,
                        "function_call with name should be user-visible; got False",
                    )
                # No name -> not user-visible (don't surface vague indicators).
                hidden = r._is_user_visible_event(
                    {"type": "item.updated", "item": {"type": "function_call"}}
                )
                if hidden:
                    return CheckResult(
                        name, False,
                        "function_call without a name should NOT be user-visible; got True",
                    )
                return CheckResult(
                    name, True,
                    "function_call+name -> True; function_call without name -> False",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_event_summary_tool_indicator() -> CheckResult:
    name = 'behavior: _event_summary renders function_call as "🔧 name..." (no event_type prefix)'
    try:
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
                r = runner_mod.CodexRunner(settings)
                line = json.dumps(
                    {"type": "item.updated", "item": {"type": "function_call", "name": "shell"}}
                )
                summary = r._event_summary(line)
                if "🔧 shell" not in summary:
                    return CheckResult(name, False, f"missing 🔧 shell in {summary!r}")
                if summary.startswith("item.updated:") or summary.startswith("item.completed:"):
                    return CheckResult(
                        name, False,
                        f"summary should NOT carry an event_type prefix; got {summary!r}",
                    )
                return CheckResult(
                    name, True,
                    f'summary: {summary!r}',
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_edit_broken_latch() -> CheckResult:
    """After the first edit_message_text fails, subsequent progress() calls
    must NOT try editing again — they go straight to send_message. Without
    this latch, hitting Telegram's 20 edits/min/message ceiling during a
    long job would scatter one-off fallback messages through the chat."""
    name = "behavior: edit_message_text raises once -> edit_broken latches, no more edit attempts"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            for sub in ("ws", "task", "memory"):
                (tmp_p / sub).mkdir(parents=True, exist_ok=True)
            with _bot_in_tmp_env(tmp_p) as bot:
                app = _make_fake_app()
                update = _make_fake_update(app, placeholder_id=99)
                # Every edit attempt raises (e.g. Telegram rate limit).
                app.edit_message_text = mock.AsyncMock(side_effect=Exception("rate limit"))
                fake_runner = _build_fake_runner(
                    progress_texts=["PROGRESS_1", "PROGRESS_2", "PROGRESS_3"],
                )
                with mock.patch.object(bot, "runner", fake_runner):
                    asyncio.run(bot._start_job(update, bot.JobMode.RUN, "test prompt"))
                # Exactly 1 edit attempt (the first one). The latch stops
                # the next two from trying again.
                if app.edit_message_text.call_count != 1:
                    return CheckResult(
                        name, False,
                        f"expected 1 edit attempt, got {app.edit_message_text.call_count}",
                    )
                # 3 sends: one per progress() call after the latch tripped.
                if app.send_message.call_count != 3:
                    return CheckResult(
                        name, False,
                        f"expected 3 send_message calls, got {app.send_message.call_count}",
                    )
                sent_texts = [
                    call.kwargs.get("text") for call in app.send_message.call_args_list
                ]
                if sent_texts != ["PROGRESS_1", "PROGRESS_2", "PROGRESS_3"]:
                    return CheckResult(
                        name, False,
                        f"send text order mismatch: {sent_texts!r}",
                    )
                return CheckResult(
                    name, True,
                    "1 edit attempt + 3 sends; latch stopped retry storm",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_command_execution_tool_indicator() -> CheckResult:
    name = 'behavior: _event_summary renders command_execution as "🔧 <binary>..." (real binary, not the generic shell fallback)'
    try:
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
                r = runner_mod.CodexRunner(settings)
                # Real codex command_execution items do not carry a
                # "name" field. Round 1 collapsed them to "🔧 shell..."
                # which was technically safe but non-informative; the
                # user wants to know *what* is actually running, not
                # just that something shell-shaped happened. We now
                # extract the leading binary name from the command
                # string (handling the /bin/bash -lc '...' wrapper)
                # and surface "🔧 curl..." etc. The raw command body
                # must still NOT leak into the chat.
                line = json.dumps(
                    {"type": "item.completed", "item": {"type": "command_execution", "command": "curl -sS https://example.com/ -o /dev/null -w '%{http_code}\\n'"}}
                )
                summary = r._event_summary(line)
                if "🔧 curl" not in summary:
                    return CheckResult(
                        name, False,
                        f"command_execution should surface as 🔧 curl...; got {summary!r}",
                    )
                # Command body must not leak; the binary name is fine.
                for needle in ("example.com", "-w", "http_code", "/dev/null"):
                    if needle in summary:
                        return CheckResult(
                            name, False,
                            f"command body leaked into summary ({needle!r}); got {summary!r}",
                        )
                # Fallback contract: when no command (or empty string)
                # is present, we still surface "🔧 shell..." rather
                # than a vague empty indicator.
                for empty_cmd in ("", "   "):
                    empty_line = json.dumps(
                        {"type": "item.completed", "item": {"type": "command_execution", "command": empty_cmd}}
                    )
                    empty_summary = r._event_summary(empty_line)
                    if "🔧 shell" not in empty_summary:
                        return CheckResult(
                            name, False,
                            f"empty/missing command should fall back to 🔧 shell...; got {empty_summary!r}",
                        )
                missing_cmd_line = json.dumps(
                    {"type": "item.completed", "item": {"type": "command_execution"}}
                )
                missing_summary = r._event_summary(missing_cmd_line)
                if "🔧 shell" not in missing_summary:
                    return CheckResult(
                        name, False,
                        f"missing command field should fall back to 🔧 shell...; got {missing_summary!r}",
                    )
                return CheckResult(
                    name, True,
                    f"command_execution -> {summary!r} (with 🔧 shell fallback for empty/missing)",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_lifecycle_events_suppressed() -> CheckResult:
    name = 'behavior: _event_summary returns "" for thread.started / turn.completed (usage captured separately)'
    try:
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
                r = runner_mod.CodexRunner(settings)
                cases = [
                    {"type": "thread.started", "thread_id": "abc-123"},
                    {"type": "thread.completed", "thread_id": "abc-123"},
                    {"type": "turn.started"},
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 1234, "output_tokens": 56},
                    },
                    {"type": "turn.failed", "error": {"message": "boom"}},
                ]
                results = []
                for event in cases:
                    line = json.dumps(event)
                    actual = r._event_summary(line)
                    results.append((event["type"], actual))
                non_empty = [c for c in results if c[1] != ""]
                if non_empty:
                    return CheckResult(
                        name, False,
                        "lifecycle events should all return ""; non-empty: " + ", ".join(
                            f"{c[0]} -> {c[1]!r}" for c in non_empty
                        ),
                    )
                return CheckResult(
                    name, True,
                    f"{len(cases)} lifecycle events all return '' (usage captured via _capture_usage)",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_no_event_type_prefix() -> CheckResult:
    name = "behavior: _event_summary does NOT prefix summaries with event_type (chat surface, not log)"
    try:
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
                r = runner_mod.CodexRunner(settings)
                bad_prefixes = ("item.updated:", "item.completed:", "thread.started:", "turn.completed:")
                cases = [
                    # Prose path
                    ({"type": "item.updated", "item": {"type": "agent_message", "text": "real answer"}}, "real answer"),
                    ({"type": "item.completed", "item": {"type": "agent_message", "text": "tail chunk"}}, "tail chunk"),
                    # Top-level text-like field on a non-lifecycle event
                    ({"type": "item.updated", "summary": "tight recap"}, "tight recap"),
                    ({"type": "item.updated", "message": "early note"}, "early note"),
                    # Tool indicator
                    ({"type": "item.updated", "item": {"type": "function_call", "name": "shell"}}, "🔧 shell"),
                ]
                offenders = []
                for event_obj, expected_text in cases:
                    line = json.dumps(event_obj)
                    actual = r._event_summary(line)
                    if not actual:
                        offenders.append((event_obj, f"empty summary; expected {expected_text!r}"))
                        continue
                    if any(actual.startswith(p) for p in bad_prefixes):
                        offenders.append((event_obj, f"starts with event_type prefix: {actual!r}"))
                        continue
                    if expected_text not in actual:
                        offenders.append((event_obj, f"expected {expected_text!r} substring; got {actual!r}"))
                if offenders:
                    return CheckResult(
                        name, False,
                        f"{len(offenders)} offender(s): " + ", ".join(
                            f"in={c[0]!r} -> {c[1]}" for c in offenders[:3]
                        ),
                    )
                return CheckResult(
                    name, True,
                    f"{len(cases)} cases, none prefixed with event_type",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_consecutive_dedup() -> CheckResult:
    """Feed two identical command_execution lines to _read_jsonl_stdout; the
    second must NOT trigger an on_progress call. The cooldown check stays,
    but is bypassed here (telegram_progress_seconds=0) so only the
    consecutive-same-text dedup is being exercised. The raw line is still
    written to job.log_path; only the user-facing edit is suppressed."""
    name = "behavior: _read_jsonl_stdout suppresses an exact-repeat summary on the next line"
    try:
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
                # 0-second cooldown so the test only exercises the
                # consecutive-same-text dedup, not the time-based gate.
                # Settings is a frozen dataclass, so we bypass the
                # __setattr__ guard rather than mutating the field
                # through the public API (which would raise FrozenInstanceError).
                object.__setattr__(settings, "telegram_progress_seconds", 0.0)
                r = runner_mod.CodexRunner(settings)

                log_path = tmp_p / "job.jsonl"
                # Use SimpleNamespace: the reader only touches .log_path
                # and assigns to .last_event. Avoid full Job so the test
                # does not pull in any worktree / metadata side effects.
                job = SimpleNamespace(log_path=log_path, last_event="starting")

                line = json.dumps(
                    {"type": "item.completed", "item": {"type": "command_execution", "command": "true"}}
                ) + "\n"

                class _FakeStdout:
                    def __init__(self, chunks: list[bytes]) -> None:
                        self._chunks = list(chunks)

                    async def readline(self) -> bytes:
                        if self._chunks:
                            return self._chunks.pop(0)
                        return b""

                process = SimpleNamespace(stdout=_FakeStdout([line.encode("utf-8"), line.encode("utf-8")]))

                progress_calls: list[str] = []

                async def _on_progress(text: str) -> None:
                    progress_calls.append(text)

                asyncio.run(r._read_jsonl_stdout(job, process, _on_progress))

                if len(progress_calls) != 1:
                    return CheckResult(
                        name, False,
                        f"expected 1 on_progress call (2nd dedup'd); got {len(progress_calls)}: {progress_calls!r}",
                    )
                if "🔧 true" not in progress_calls[0]:
                    return CheckResult(
                        name, False,
                        f"progress text should be the binary-name indicator (🔧 true...); got {progress_calls[0]!r}",
                    )
                # Raw line was still written to the log file (dedup is for
                # the user-facing edit, not the audit trail).
                logged = log_path.read_bytes().decode("utf-8")
                if logged.count("command_execution") != 2:
                    return CheckResult(
                        name, False,
                        f"raw log should contain 2 command_execution lines; got {logged.count('command_execution')}",
                    )
                return CheckResult(
                    name, True,
                    f"2 raw lines, 1 on_progress call ({progress_calls[0]!r}); 2nd dedup'd",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_is_prose_event_exists() -> CheckResult:
    name = "AST: _is_prose_event is FunctionDef on CodexRunner with signature (self, event_obj: dict | None -> bool)"
    try:
        method = _class_method(_parse_runner(), "CodexRunner", "_is_prose_event")
        if not isinstance(method, ast.FunctionDef):
            return CheckResult(name, False, "method missing on CodexRunner class")
        actual = _signature_str(method)
        expected = "self, event_obj: dict | None -> bool"
        if actual != expected:
            return CheckResult(name, False, f"signature mismatch: got {actual!r}, expected {expected!r}")
        return CheckResult(name, True, actual)
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_is_prose_event_classification() -> CheckResult:
    """Classify a spread of event shapes against the round-4 contract:
    agent_message items and top-level text-like fields are prose;
    lifecycle, reasoning, function_call, and command_execution are NOT
    prose (so they bypass the per-stream growing gate and surface as
    the current state). Non-dict / malformed payloads are not prose."""
    name = "behavior: _is_prose_event classifies agent_message / top-level text as prose; tool calls / lifecycle / malformed as not"
    try:
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
                r = runner_mod.CodexRunner(settings)
                cases = [
                    # agent_message item.updated / item.completed -> True
                    ({"type": "item.updated", "item": {"type": "agent_message", "text": "hello"}}, True),
                    ({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}, True),
                    # top-level message / summary / text / delta -> True
                    ({"message": "early note"}, True),
                    ({"summary": "tight recap"}, True),
                    ({"text": "verbatim quote"}, True),
                    ({"delta": "stream chunk"}, True),
                    # thread.started / turn.completed / reasoning -> False
                    ({"type": "thread.started"}, False),
                    ({"type": "turn.completed", "usage": {}}, False),
                    ({"item": {"type": "reasoning", "text": "..."}}, False),
                    # function_call / command_execution must NOT be prose
                    # (the growing gate only applies to user-readable chat text)
                    ({"item": {"type": "function_call", "name": "shell"}}, False),
                    ({"item": {"type": "command_execution", "command": "curl ..."}}, False),
                    # non-dict / malformed -> False
                    (None, False),
                    ({"item": "not a dict"}, False),
                ]
                offenders = []
                for event_obj, expected in cases:
                    actual = r._is_prose_event(event_obj)
                    if actual is not expected:
                        offenders.append((event_obj, expected, actual))
                if offenders:
                    return CheckResult(
                        name, False,
                        f"{len(offenders)} misclassified: " + ", ".join(
                            f"in={c[0]!r} expected={c[1]} got={c[2]}" for c in offenders[:5]
                        ),
                    )
                return CheckResult(
                    name, True,
                    f"{len(cases)} cases classified correctly",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_growing_gate_on_prose() -> CheckResult:
    """Per-stream prose gate: only forward edits that strictly extend
    the last sent prose. A mid-stream shrink is suppressed so the chat
    does not visibly re-write to a shorter string. ``item.completed``
    is exempt so the final text always wins; the tracker resets on
    complete so the next item can start a new growing chain (even if
    its first update is shorter than the previous item's last edit)."""
    name = "behavior: _read_jsonl_stdout suppresses mid-stream prose shrinks; item.completed wins; new items start a new chain"
    try:
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
                # 0-second cooldown so only the growing gate is exercised.
                # Settings is a frozen dataclass, so we bypass the
                # __setattr__ guard rather than mutating the field
                # through the public API (which would raise FrozenInstanceError).
                object.__setattr__(settings, "telegram_progress_seconds", 0.0)
                r = runner_mod.CodexRunner(settings)

                log_path = tmp_p / "job.jsonl"
                # Use SimpleNamespace: the reader only touches .log_path
                # and assigns to .last_event. Avoid full Job so the test
                # does not pull in any worktree / metadata side effects.
                job = SimpleNamespace(log_path=log_path, last_event="starting")

                lines = [
                    json.dumps({"type": "item.updated", "item": {"type": "agent_message", "id": "a", "text": "Hello"}}),
                    json.dumps({"type": "item.updated", "item": {"type": "agent_message", "id": "a", "text": "Hello world"}}),
                    json.dumps({"type": "item.updated", "item": {"type": "agent_message", "id": "a", "text": "Hi"}}),
                    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "id": "a", "text": "Hi there"}}),
                    json.dumps({"type": "item.updated", "item": {"type": "agent_message", "id": "b", "text": "Bye"}}),
                ]
                encoded = [ln.encode("utf-8") + b"\n" for ln in lines]

                class _FakeStdout:
                    def __init__(self, chunks: list[bytes]) -> None:
                        self._chunks = list(chunks)

                    async def readline(self) -> bytes:
                        if self._chunks:
                            return self._chunks.pop(0)
                        return b""

                process = SimpleNamespace(stdout=_FakeStdout(encoded))

                progress_calls: list[str] = []

                async def _on_progress(text: str) -> None:
                    progress_calls.append(text)

                asyncio.run(r._read_jsonl_stdout(job, process, _on_progress))

                expected = ["Hello", "Hello world", "Hi there", "Bye"]
                if progress_calls != expected:
                    return CheckResult(
                        name, False,
                        f"progress calls mismatch: expected {expected!r}; got {progress_calls!r}",
                    )
                # All 5 raw lines were still written to the log (growing
                # gate is for the user-facing edit, not the audit trail).
                logged = log_path.read_text()
                if logged.count("agent_message") != 5:
                    return CheckResult(
                        name, False,
                        f"raw log should contain 5 agent_message lines; got {logged.count('agent_message')}",
                    )
                return CheckResult(
                    name, True,
                    "4 progress calls (mid-stream 'Hi' shrink suppressed; "
                    "item.completed 'Hi there' wins; new item 'Bye' starts a new chain); 5 raw lines logged",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_thinking_indicator_appears_after_threshold() -> CheckResult:
    """Three reasoning events in a row with the production threshold
    lowered to 0.0 (test-only override) must surface exactly one
    ``💭 thinking...`` edit. Per-chain dedup: the second and
    third reasoning events do NOT re-fire the indicator. The raw
    lines are still written to ``job.log_path`` so the audit trail
    is preserved."""
    name = "behavior: _read_jsonl_stdout surfaces one 'thinking...' indicator per sustained-reasoning chain"
    original_threshold = None
    try:
        import runner as runner_mod  # noqa: PLC0415
        # Lower the threshold to 0.0 so the first reasoning event of
        # the chain immediately satisfies it. This is the test
        # override hook (re-binding a module global) and mirrors the
        # frozen-Settings bypass used by _test_growing_gate_on_prose.
        original_threshold = runner_mod.THINKING_THRESHOLD_SECONDS
        runner_mod.THINKING_THRESHOLD_SECONDS = 0.0
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
                # 0-second cooldown so the indicator isn't gated by
                # telegram_progress_seconds (which defaults to 1.5s).
                # Bypass the frozen __setattr__ guard.
                object.__setattr__(settings, "telegram_progress_seconds", 0.0)
                r = runner_mod.CodexRunner(settings)

                log_path = tmp_p / "job.jsonl"
                job = SimpleNamespace(log_path=log_path, last_event="starting")

                lines = [
                    json.dumps({"item": {"type": "reasoning", "text": "step 1"}}),
                    json.dumps({"item": {"type": "reasoning", "text": "step 2"}}),
                    json.dumps({"item": {"type": "reasoning", "text": "step 3"}}),
                ]
                encoded = [ln.encode("utf-8") + b"\n" for ln in lines]

                class _FakeStdout:
                    def __init__(self, chunks: list[bytes]) -> None:
                        self._chunks = list(chunks)

                    async def readline(self) -> bytes:
                        if self._chunks:
                            return self._chunks.pop(0)
                        return b""

                process = SimpleNamespace(stdout=_FakeStdout(encoded))

                progress_calls: list[str] = []

                async def _on_progress(text: str) -> None:
                    progress_calls.append(text)

                asyncio.run(r._read_jsonl_stdout(job, process, _on_progress))

                if progress_calls != [runner_mod.THINKING_INDICATOR]:
                    return CheckResult(
                        name, False,
                        f"expected exactly 1 thinking-indicator call; got {progress_calls!r}",
                    )
                if runner_mod.THINKING_INDICATOR not in progress_calls[0]:
                    return CheckResult(
                        name, False,
                        f"indicator text mismatch: got {progress_calls[0]!r}",
                    )
                logged = log_path.read_text()
                if logged.count("reasoning") != 3:
                    return CheckResult(
                        name, False,
                        f"raw log should contain 3 reasoning lines; got {logged.count('reasoning')}",
                    )
                return CheckResult(
                    name, True,
                    f"3 reasoning lines, 1 indicator call ({progress_calls[0]!r}); 2nd and 3rd dedup'd per chain",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")
    finally:
        if original_threshold is not None:
            import runner as runner_mod  # noqa: PLC0415
            runner_mod.THINKING_THRESHOLD_SECONDS = original_threshold


def _test_thinking_indicator_clears_on_prose() -> CheckResult:
    """A reasoning chain that gets interrupted by an agent_message
    item must clear the chain (thinking_since=None, thinking_indicator_sent=False)
    and the prose event replaces the indicator. The new chain starts
    fresh so a second reasoning burst would be eligible to fire again."""
    name = "behavior: _read_jsonl_stdout clears the thinking chain when a prose event arrives"
    original_threshold = None
    try:
        import runner as runner_mod  # noqa: PLC0415
        original_threshold = runner_mod.THINKING_THRESHOLD_SECONDS
        runner_mod.THINKING_THRESHOLD_SECONDS = 0.0
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
                r = runner_mod.CodexRunner(settings)

                log_path = tmp_p / "job.jsonl"
                job = SimpleNamespace(log_path=log_path, last_event="starting")

                lines = [
                    json.dumps({"item": {"type": "reasoning", "text": "deep thought"}}),
                    json.dumps({"type": "item.updated", "item": {"type": "agent_message", "id": "a", "text": "Hello"}}),
                ]
                encoded = [ln.encode("utf-8") + b"\n" for ln in lines]

                class _FakeStdout:
                    def __init__(self, chunks: list[bytes]) -> None:
                        self._chunks = list(chunks)

                    async def readline(self) -> bytes:
                        if self._chunks:
                            return self._chunks.pop(0)
                        return b""

                process = SimpleNamespace(stdout=_FakeStdout(encoded))

                progress_calls: list[str] = []

                async def _on_progress(text: str) -> None:
                    progress_calls.append(text)

                asyncio.run(r._read_jsonl_stdout(job, process, _on_progress))

                if progress_calls != [runner_mod.THINKING_INDICATOR, "Hello"]:
                    return CheckResult(
                        name, False,
                        f"expected [thinking, 'Hello']; got {progress_calls!r}",
                    )
                return CheckResult(
                    name, True,
                    f"chain cleared on prose: indicator then prose '{progress_calls[1]}'; chain state reset for next burst",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")
    finally:
        if original_threshold is not None:
            import runner as runner_mod  # noqa: PLC0415
            runner_mod.THINKING_THRESHOLD_SECONDS = original_threshold


def _test_thinking_indicator_clears_on_tool_call() -> CheckResult:
    """A reasoning chain that gets interrupted by a function_call
    item (which surfaces as '🔧 shell...' on this test's payload since
    no name is set) must clear the chain. The tool indicator replaces
    the thinking indicator, so the user sees the model is doing
    something concrete instead of just thinking."""
    name = "behavior: _read_jsonl_stdout clears the thinking chain when a tool-call event arrives"
    original_threshold = None
    try:
        import runner as runner_mod  # noqa: PLC0415
        original_threshold = runner_mod.THINKING_THRESHOLD_SECONDS
        runner_mod.THINKING_THRESHOLD_SECONDS = 0.0
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
                r = runner_mod.CodexRunner(settings)

                log_path = tmp_p / "job.jsonl"
                job = SimpleNamespace(log_path=log_path, last_event="starting")

                lines = [
                    json.dumps({"item": {"type": "reasoning", "text": "planning"}}),
                    json.dumps({"item": {"type": "function_call", "name": "shell"}}),
                ]
                encoded = [ln.encode("utf-8") + b"\n" for ln in lines]

                class _FakeStdout:
                    def __init__(self, chunks: list[bytes]) -> None:
                        self._chunks = list(chunks)

                    async def readline(self) -> bytes:
                        if self._chunks:
                            return self._chunks.pop(0)
                        return b""

                process = SimpleNamespace(stdout=_FakeStdout(encoded))

                progress_calls: list[str] = []

                async def _on_progress(text: str) -> None:
                    progress_calls.append(text)

                asyncio.run(r._read_jsonl_stdout(job, process, _on_progress))

                if len(progress_calls) != 2:
                    return CheckResult(
                        name, False,
                        f"expected 2 progress calls; got {progress_calls!r}",
                    )
                if progress_calls[0] != runner_mod.THINKING_INDICATOR:
                    return CheckResult(
                        name, False,
                        f"first call should be the thinking indicator; got {progress_calls[0]!r}",
                    )
                if not progress_calls[1].startswith("\U0001f527"):
                    return CheckResult(
                        name, False,
                        f"second call should be the tool indicator (\U0001f527 shell...); got {progress_calls[1]!r}",
                    )
                return CheckResult(
                    name, True,
                    f"chain cleared on tool call: indicator then tool indicator '{progress_calls[1]}'",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")
    finally:
        if original_threshold is not None:
            import runner as runner_mod  # noqa: PLC0415
            runner_mod.THINKING_THRESHOLD_SECONDS = original_threshold


def _test_thinking_indicator_skipped_for_short_reasoning() -> CheckResult:
    """A reasoning chain shorter than ``THINKING_THRESHOLD_SECONDS``
    (test raises the threshold to 1000s so the unit-test elapsed time
    of microseconds can never satisfy it) must NOT fire the indicator.
    The next prose event lands directly without a thinking call in
    between. This pins the round-5 contract that short reasoning
    bursts are not surfaced to the user."""
    name = "behavior: _read_jsonl_stdout skips the thinking indicator when reasoning is shorter than the threshold"
    original_threshold = None
    try:
        import runner as runner_mod  # noqa: PLC0415
        original_threshold = runner_mod.THINKING_THRESHOLD_SECONDS
        # Raise the threshold so the unit test's microsecond inter-
        # event spacing can never satisfy it. This proves the
        # threshold is honored, not just that the constant is wired
        # in.
        runner_mod.THINKING_THRESHOLD_SECONDS = 1000.0
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
                r = runner_mod.CodexRunner(settings)

                log_path = tmp_p / "job.jsonl"
                job = SimpleNamespace(log_path=log_path, last_event="starting")

                lines = [
                    json.dumps({"item": {"type": "reasoning", "text": "quick thought"}}),
                    json.dumps({"type": "item.updated", "item": {"type": "agent_message", "id": "a", "text": "Hello"}}),
                ]
                encoded = [ln.encode("utf-8") + b"\n" for ln in lines]

                class _FakeStdout:
                    def __init__(self, chunks: list[bytes]) -> None:
                        self._chunks = list(chunks)

                    async def readline(self) -> bytes:
                        if self._chunks:
                            return self._chunks.pop(0)
                        return b""

                process = SimpleNamespace(stdout=_FakeStdout(encoded))

                progress_calls: list[str] = []

                async def _on_progress(text: str) -> None:
                    progress_calls.append(text)

                asyncio.run(r._read_jsonl_stdout(job, process, _on_progress))

                if progress_calls != ["Hello"]:
                    return CheckResult(
                        name, False,
                        f"expected ['Hello'] (no thinking call); got {progress_calls!r}",
                    )
                return CheckResult(
                    name, True,
                    f"short reasoning chain: prose '{progress_calls[0]}' lands directly; no thinking indicator at 1000s threshold",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")
    finally:
        if original_threshold is not None:
            import runner as runner_mod  # noqa: PLC0415


# ---- chat-feel round-6: tool-call pulse -------------------------------

def _test_tool_pulse_appears_after_threshold() -> CheckResult:
    """A long tool call (network fetch, big shell pipeline) leaves
    the placeholder sitting on the one-line ``🔧 name...`` indicator
    from round 2 for 5-30s with no further edits, which reads as
    frozen. Round 6 arms a periodic "still working" pulse that
    updates the indicator in place with the elapsed seconds. With
    the production threshold and interval lowered to 0.0, an
    ``item.started`` for a function_call must surface the original
    ``🔧 curl...`` indicator AND a subsequent ``🔧 curl (0s)...``
    pulse on the same iteration. The pulse shares the
    ``telegram_progress_seconds`` cooldown with the rest of the
    gate ladder."""
    name = "behavior: _read_jsonl_stdout surfaces a '🔧 name (Ns)...' pulse after the tool-call threshold"
    original_threshold = None
    original_interval = None
    try:
        import runner as runner_mod  # noqa: PLC0415
        # Test override hook: re-binding the module-level constants
        # is the only way to bypass the frozen-Settings guard. This
        # mirrors the THINKING_THRESHOLD_SECONDS pattern above.
        original_threshold = runner_mod.TOOL_PULSE_THRESHOLD_SECONDS
        original_interval = runner_mod.TOOL_PULSE_INTERVAL_SECONDS
        runner_mod.TOOL_PULSE_THRESHOLD_SECONDS = 0.0
        runner_mod.TOOL_PULSE_INTERVAL_SECONDS = 0.0
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
                # 0-second cooldown so the pulse isn't gated by
                # telegram_progress_seconds (which defaults to 1.5s).
                # Bypass the frozen __setattr__ guard.
                object.__setattr__(settings, "telegram_progress_seconds", 0.0)
                r = runner_mod.CodexRunner(settings)

                log_path = tmp_p / "job.jsonl"
                job = SimpleNamespace(log_path=log_path, last_event="starting")

                lines = [
                    json.dumps({"type": "item.started", "item": {"type": "function_call", "name": "curl"}}),
                ]
                encoded = [ln.encode("utf-8") + b"\n" for ln in lines]

                class _FakeStdout:
                    def __init__(self, chunks: list[bytes]) -> None:
                        self._chunks = list(chunks)

                    async def readline(self) -> bytes:
                        if self._chunks:
                            return self._chunks.pop(0)
                        return b""

                process = SimpleNamespace(stdout=_FakeStdout(encoded))

                progress_calls: list[str] = []

                async def _on_progress(text: str) -> None:
                    progress_calls.append(text)

                asyncio.run(r._read_jsonl_stdout(job, process, _on_progress))

                if progress_calls != ["🔧 curl...", "🔧 curl (0s)..."]:
                    return CheckResult(
                        name, False,
                        f"expected ['🔧 curl...', '🔧 curl (0s)...']; got {progress_calls!r}",
                    )
                return CheckResult(
                    name, True,
                    f"item.started -> 2 calls: tool summary + immediate pulse at 0s threshold: {progress_calls!r}",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")
    finally:
        if original_threshold is not None:
            import runner as runner_mod  # noqa: PLC0415
            runner_mod.TOOL_PULSE_THRESHOLD_SECONDS = original_threshold
        if original_interval is not None:
            import runner as runner_mod  # noqa: PLC0415
            runner_mod.TOOL_PULSE_INTERVAL_SECONDS = original_interval


def _test_tool_pulse_clears_on_completion() -> CheckResult:
    """The round-6 pulse window must disarm on the matching
    ``item.completed`` event for the same tool name so a stale
    complete from a prior call cannot wipe a fresh arm. The
    sequence is: ``item.started`` (arm + summary + pulse) ->
    ``item.completed`` (disarm, the consecutive-same-text dedup
    suppresses the second ``🔧 curl...``) -> ``agent_message``
    prose. Expected: 3 progress calls (tool summary, pulse, prose)
    in that order, with no pulse after the disarm."""
    name = "behavior: _read_jsonl_stdout disarms the tool-pulse window on the matching item.completed"
    original_threshold = None
    original_interval = None
    try:
        import runner as runner_mod  # noqa: PLC0415
        original_threshold = runner_mod.TOOL_PULSE_THRESHOLD_SECONDS
        original_interval = runner_mod.TOOL_PULSE_INTERVAL_SECONDS
        runner_mod.TOOL_PULSE_THRESHOLD_SECONDS = 0.0
        runner_mod.TOOL_PULSE_INTERVAL_SECONDS = 0.0
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
                r = runner_mod.CodexRunner(settings)

                log_path = tmp_p / "job.jsonl"
                job = SimpleNamespace(log_path=log_path, last_event="starting")

                lines = [
                    json.dumps({"type": "item.started", "item": {"type": "function_call", "name": "curl"}}),
                    json.dumps({"type": "item.completed", "item": {"type": "function_call", "name": "curl"}}),
                    json.dumps({"type": "item.updated", "item": {"type": "agent_message", "id": "a", "text": "Hello"}}),
                ]
                encoded = [ln.encode("utf-8") + b"\n" for ln in lines]

                class _FakeStdout:
                    def __init__(self, chunks: list[bytes]) -> None:
                        self._chunks = list(chunks)

                    async def readline(self) -> bytes:
                        if self._chunks:
                            return self._chunks.pop(0)
                        return b""

                process = SimpleNamespace(stdout=_FakeStdout(encoded))

                progress_calls: list[str] = []

                async def _on_progress(text: str) -> None:
                    progress_calls.append(text)

                asyncio.run(r._read_jsonl_stdout(job, process, _on_progress))

                if progress_calls != ["🔧 curl...", "🔧 curl (0s)...", "Hello"]:
                    return CheckResult(
                        name, False,
                        f"expected ['🔧 curl...', '🔧 curl (0s)...', 'Hello']; got {progress_calls!r}",
                    )
                return CheckResult(
                    name, True,
                    f"start -> pulse -> disarm -> prose: 3 calls in order, post-disarm no more pulses: {progress_calls!r}",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")
    finally:
        if original_threshold is not None:
            import runner as runner_mod  # noqa: PLC0415
            runner_mod.TOOL_PULSE_THRESHOLD_SECONDS = original_threshold
        if original_interval is not None:
            import runner as runner_mod  # noqa: PLC0415
            runner_mod.TOOL_PULSE_INTERVAL_SECONDS = original_interval


def _test_tool_pulse_skipped_for_short_call() -> CheckResult:
    """A short tool call (e.g. ``true``) finishes in microseconds,
    well below the 4.0s production threshold. The pulse must NOT
    fire. With the production threshold raised to 1000s, the unit
    test's microsecond inter-event spacing can never satisfy it.
    Expected: 2 progress calls (tool summary, prose) and no pulse.
    This pins the round-6 contract that short tool calls do not
    add a pulse tick on top of the existing one-line indicator."""
    name = "behavior: _read_jsonl_stdout skips the tool-pulse when the call is shorter than the threshold"
    original_threshold = None
    original_interval = None
    try:
        import runner as runner_mod  # noqa: PLC0415
        original_threshold = runner_mod.TOOL_PULSE_THRESHOLD_SECONDS
        original_interval = runner_mod.TOOL_PULSE_INTERVAL_SECONDS
        # Raise the threshold so the unit test's microsecond
        # inter-event spacing can never satisfy it. This proves
        # the threshold is honored, not just that the constant
        # is wired in.
        runner_mod.TOOL_PULSE_THRESHOLD_SECONDS = 1000.0
        runner_mod.TOOL_PULSE_INTERVAL_SECONDS = 0.0
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
                r = runner_mod.CodexRunner(settings)

                log_path = tmp_p / "job.jsonl"
                job = SimpleNamespace(log_path=log_path, last_event="starting")

                lines = [
                    json.dumps({"type": "item.started", "item": {"type": "function_call", "name": "true"}}),
                    json.dumps({"type": "item.updated", "item": {"type": "agent_message", "id": "a", "text": "Hello"}}),
                ]
                encoded = [ln.encode("utf-8") + b"\n" for ln in lines]

                class _FakeStdout:
                    def __init__(self, chunks: list[bytes]) -> None:
                        self._chunks = list(chunks)

                    async def readline(self) -> bytes:
                        if self._chunks:
                            return self._chunks.pop(0)
                        return b""

                process = SimpleNamespace(stdout=_FakeStdout(encoded))

                progress_calls: list[str] = []

                async def _on_progress(text: str) -> None:
                    progress_calls.append(text)

                asyncio.run(r._read_jsonl_stdout(job, process, _on_progress))

                if progress_calls != ["🔧 true...", "Hello"]:
                    return CheckResult(
                        name, False,
                        f"expected ['🔧 true...', 'Hello'] (no pulse at 1000s threshold); got {progress_calls!r}",
                    )
                return CheckResult(
                    name, True,
                    f"short tool call: tool summary + prose, no pulse at 1000s threshold: {progress_calls!r}",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")
    finally:
        if original_threshold is not None:
            import runner as runner_mod  # noqa: PLC0415
            runner_mod.TOOL_PULSE_THRESHOLD_SECONDS = original_threshold
        if original_interval is not None:
            import runner as runner_mod  # noqa: PLC0415
            runner_mod.TOOL_PULSE_INTERVAL_SECONDS = original_interval


def _test_tool_pulse_respects_interval() -> CheckResult:
    """The pulse must respect ``TOOL_PULSE_INTERVAL_SECONDS`` between
    re-fires, not just the first-fire ``TOOL_PULSE_THRESHOLD_SECONDS``.
    With the production interval raised to 1000s, the second and
    third pulse checks (which fire on every loop iteration while
    a tool call is in flight) must be suppressed. The expected
    sequence is: tool summary -> ONE pulse -> prose1 -> prose2,
    with no additional pulses despite the threshold being 0.
    This pins the round-6 contract that the pulse is a periodic
    heartbeat, not a per-iteration tick."""
    name = "behavior: _read_jsonl_stdout re-arms the tool-pulse only after TOOL_PULSE_INTERVAL_SECONDS"
    original_threshold = None
    original_interval = None
    try:
        import runner as runner_mod  # noqa: PLC0415
        original_threshold = runner_mod.TOOL_PULSE_THRESHOLD_SECONDS
        original_interval = runner_mod.TOOL_PULSE_INTERVAL_SECONDS
        runner_mod.TOOL_PULSE_THRESHOLD_SECONDS = 0.0
        # Raise the interval so the unit test's microsecond
        # inter-event spacing can never satisfy it. This proves
        # the interval gate is honored, not just that the constant
        # is wired in.
        runner_mod.TOOL_PULSE_INTERVAL_SECONDS = 1000.0
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
                r = runner_mod.CodexRunner(settings)

                log_path = tmp_p / "job.jsonl"
                job = SimpleNamespace(log_path=log_path, last_event="starting")

                lines = [
                    json.dumps({"type": "item.started", "item": {"type": "function_call", "name": "curl"}}),
                    json.dumps({"type": "item.updated", "item": {"type": "agent_message", "id": "a", "text": "Hello"}}),
                    json.dumps({"type": "item.updated", "item": {"type": "agent_message", "id": "a", "text": "Hello world"}}),
                ]
                encoded = [ln.encode("utf-8") + b"\n" for ln in lines]

                class _FakeStdout:
                    def __init__(self, chunks: list[bytes]) -> None:
                        self._chunks = list(chunks)

                    async def readline(self) -> bytes:
                        if self._chunks:
                            return self._chunks.pop(0)
                        return b""

                process = SimpleNamespace(stdout=_FakeStdout(encoded))

                progress_calls: list[str] = []

                async def _on_progress(text: str) -> None:
                    progress_calls.append(text)

                asyncio.run(r._read_jsonl_stdout(job, process, _on_progress))

                if progress_calls != ["🔧 curl...", "🔧 curl (0s)...", "Hello", "Hello world"]:
                    return CheckResult(
                        name, False,
                        f"expected ['🔧 curl...', '🔧 curl (0s)...', 'Hello', 'Hello world']; got {progress_calls!r}",
                    )
                return CheckResult(
                    name, True,
                    f"interval gate suppresses re-fire: 1 pulse despite threshold=0: {progress_calls!r}",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")
    finally:
        if original_threshold is not None:
            import runner as runner_mod  # noqa: PLC0415
            runner_mod.TOOL_PULSE_THRESHOLD_SECONDS = original_threshold
        if original_interval is not None:
            import runner as runner_mod  # noqa: PLC0415
            runner_mod.TOOL_PULSE_INTERVAL_SECONDS = original_interval


# ---- chat-feel round-8: first-event cooldown bypass ---------------------
# Round-8: the first event in the stream must pass the
# ``telegram_progress_seconds`` cooldown immediately, not wait 3s
# after the loop starts. The bypass is a one-shot seed of ``last_sent``
# at ``-telegram_progress_seconds`` in ``_read_jsonl_stdout`` (runner.py:731).
# All 3 gates (thinking indicator, prose, tool-pulse) share the same
# ``last_sent`` cooldown, so the bypass benefits all three. The 3 tests
# pin the contract: the first event fires (bypass works), the second
# event within the cooldown is gated (bypass is one-shot), and the
# second event after the cooldown fires (normal cooldown still applies
# for the rest of the stream).
#
# Test override pattern: ``object.__setattr__(settings,
# "telegram_progress_seconds", 3.0)`` to bypass the frozen-Settings
# guard with a non-zero value (mirrors the round-5/6 pattern). The
# 3.0s cooldown is intentional — it proves the bypass is real, not just
# that the constant is wired in.

def _test_first_prose_fires_immediately_after_placeholder() -> CheckResult:
    """Round-8 first-event cooldown bypass. With
    ``telegram_progress_seconds=3.0`` (the production default), the
    first prose event in the stream must pass the cooldown
    immediately, not wait 3s. With the old code (``last_sent = 0.0``)
    the first event would be gated because ``now - 0.0 < 3.0``. With
    the round-8 fix (``last_sent = -3.0``) the first event passes
    because ``now - (-3.0) >= 3.0``. This pins the new contract that
    the chat shows the first prose as soon as it arrives, not after a
    3-second gap."""
    name = "behavior: _read_jsonl_stdout fires the first prose immediately after the placeholder (round 8)"
    try:
        import runner as runner_mod  # noqa: PLC0415
        # Test override hook: re-binding the module-level constants
        # is the only way to bypass the frozen-Settings guard. This
        # mirrors the THINKING_THRESHOLD_SECONDS pattern.
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
                # Production-like cooldown: 3.0s. NOT zeroed.
                # Bypass the frozen __setattr__ guard.
                object.__setattr__(settings, "telegram_progress_seconds", 3.0)
                r = runner_mod.CodexRunner(settings)

                log_path = tmp_p / "job.jsonl"
                job = SimpleNamespace(log_path=log_path, last_event="starting")

                lines = [
                    json.dumps({"type": "item.updated", "item": {"type": "agent_message", "id": "a", "text": "Hello"}}),
                ]
                encoded = [ln.encode("utf-8") + b"\n" for ln in lines]

                class _FakeStdout:
                    def __init__(self, chunks: list[bytes]) -> None:
                        self._chunks = list(chunks)

                    async def readline(self) -> bytes:
                        if self._chunks:
                            return self._chunks.pop(0)
                        return b""

                process = SimpleNamespace(stdout=_FakeStdout(encoded))

                progress_calls: list[str] = []

                async def _on_progress(text: str) -> None:
                    progress_calls.append(text)

                asyncio.run(r._read_jsonl_stdout(job, process, _on_progress))

                if progress_calls != ["Hello"]:
                    return CheckResult(
                        name, False,
                        f"expected ['Hello'] (first prose fires immediately at 3.0s cooldown); got {progress_calls!r}",
                    )
                return CheckResult(
                    name, True,
                    f"first prose fires immediately at 3.0s cooldown: {progress_calls!r}",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_subsequent_prose_respects_cooldown() -> CheckResult:
    """Round-8 first-event cooldown bypass must be a one-shot bypass.
    With ``telegram_progress_seconds=3.0``, two events emitted microseconds
    apart must produce only one progress call: the first fires (bypass),
    the second is gated (normal cooldown applies after the first send
    updates ``last_sent`` to ``now``). This pins the round-8 contract
    that the bypass does not lower the cooldown for the rest of the
    stream."""
    name = "behavior: _read_jsonl_stdout gates the second prose within the cooldown after the first (round 8)"
    try:
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
                object.__setattr__(settings, "telegram_progress_seconds", 3.0)
                r = runner_mod.CodexRunner(settings)

                log_path = tmp_p / "job.jsonl"
                job = SimpleNamespace(log_path=log_path, last_event="starting")

                # Two agent_message events microseconds apart. The
                # first passes via the round-8 bypass; the second is
                # gated by the 3.0s cooldown. Note the second event's
                # text is strictly longer (growing_ok) and different
                # from the first (last_sent_text dedup), so only the
                # cooldown gate can suppress it.
                lines = [
                    json.dumps({"type": "item.updated", "item": {"type": "agent_message", "id": "a", "text": "Hello"}}),
                    json.dumps({"type": "item.updated", "item": {"type": "agent_message", "id": "a", "text": "Hello world"}}),
                ]
                encoded = [ln.encode("utf-8") + b"\n" for ln in lines]

                class _FakeStdout:
                    def __init__(self, chunks: list[bytes]) -> None:
                        self._chunks = list(chunks)

                    async def readline(self) -> bytes:
                        if self._chunks:
                            return self._chunks.pop(0)
                        return b""

                process = SimpleNamespace(stdout=_FakeStdout(encoded))

                progress_calls: list[str] = []

                async def _on_progress(text: str) -> None:
                    progress_calls.append(text)

                asyncio.run(r._read_jsonl_stdout(job, process, _on_progress))

                if progress_calls != ["Hello"]:
                    return CheckResult(
                        name, False,
                        f"expected ['Hello'] (2nd gated by 3.0s cooldown); got {progress_calls!r}",
                    )
                return CheckResult(
                    name, True,
                    f"bypass is one-shot: 1st fires via bypass, 2nd gated by cooldown: {progress_calls!r}",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_subsequent_prose_fires_after_cooldown() -> CheckResult:
    """Round-8 first-event cooldown bypass must not lower the cooldown
    for the rest of the stream. With ``telegram_progress_seconds=3.0``,
    two events separated by >3s must produce two progress calls: the
    first fires (bypass), the second fires after the cooldown elapses
    (normal cooldown). This pins the round-8 contract that the bypass
    is exactly one-shot and the normal cooldown still applies to 2nd+
    events. The _FakeStdout sleeps 3.1s before the 2nd chunk to
    advance the wall clock past the cooldown; total test time ~3.1s."""
    name = "behavior: _read_jsonl_stdout fires the second prose after the cooldown elapses (round 8)"
    try:
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
                object.__setattr__(settings, "telegram_progress_seconds", 3.0)
                r = runner_mod.CodexRunner(settings)

                log_path = tmp_p / "job.jsonl"
                job = SimpleNamespace(log_path=log_path, last_event="starting")

                # Two agent_message events. The 2nd read sleeps 3.1s
                # so the wall clock advances past the 3.0s cooldown.
                lines = [
                    json.dumps({"type": "item.updated", "item": {"type": "agent_message", "id": "a", "text": "Hello"}}),
                    json.dumps({"type": "item.updated", "item": {"type": "agent_message", "id": "a", "text": "Hello world"}}),
                ]
                encoded = [ln.encode("utf-8") + b"\n" for ln in lines]
                # delays[i] is the sleep BEFORE returning chunks[i].
                # chunks[-1] is the b"" EOF sentinel; only the first
                # len(chunks)-1 delays matter for the events.
                delays = [0.0, 3.1]

                class _FakeStdout:
                    def __init__(self, chunks: list[bytes], chunk_delays: list[float]) -> None:
                        self._chunks = list(chunks)
                        self._delays = list(chunk_delays) + [0.0] * (len(chunks) - len(chunk_delays))

                    async def readline(self) -> bytes:
                        if self._delays:
                            await asyncio.sleep(self._delays.pop(0))
                        if self._chunks:
                            return self._chunks.pop(0)
                        return b""

                process = SimpleNamespace(stdout=_FakeStdout(encoded, delays))

                progress_calls: list[str] = []

                async def _on_progress(text: str) -> None:
                    progress_calls.append(text)

                asyncio.run(r._read_jsonl_stdout(job, process, _on_progress))

                if progress_calls != ["Hello", "Hello world"]:
                    return CheckResult(
                        name, False,
                        f"expected ['Hello', 'Hello world'] (2nd fires after 3.1s sleep past 3.0s cooldown); got {progress_calls!r}",
                    )
                return CheckResult(
                    name, True,
                    f"normal cooldown applies to 2nd+: 1st via bypass, 2nd after 3.1s: {progress_calls!r}",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- onboarding round-A: operator profile block -----------------------
# Onboarding-A: every prompt is prefixed with an <operator-profile>
# block that gives the agent a stable identity for the operator
# (name, language, style, standing) so it does not have to
# re-discover those on each session. The test pins that the block
# is in _prefetch_memory output and carries the four attrs sourced
# from Settings. The override pattern (object.__setattr__ on frozen
# Settings) mirrors the round-5/6/8 test convention.

def _test_operator_profile_block_in_prefetch() -> CheckResult:
    """Onboarding-A: <operator-profile> block is prepended to every
    prefetch output. The four attrs (name, language, style, standing)
    are present and the prose body restates language + style + setup
    so the directive is harder to lose in a long context."""
    name = "behavior: _prefetch_memory prepends <operator-profile> with the 4 attrs (onboarding-A)"
    try:
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
                # Bypass the frozen-Settings guard to inject the test
                # values. Mirrors the round-5/6/8 pattern.
                object.__setattr__(settings, "operator_name", "TestOp")
                object.__setattr__(settings, "operator_language", "en")
                object.__setattr__(settings, "operator_style", "verbose")
                object.__setattr__(settings, "operator_standing", "test-rig")
                # Pre-write the day-brief state file with today's UTC
                # date so _day_brief_text returns "" - keeps the test
                # focused on the profile block only.
                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                state_path = tmp_p / "memory" / "state" / "last_day_brief.txt"
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(today_str, encoding="utf-8")
                r = runner_mod.CodexRunner(settings)

                # _tool_registry_text reads job.mode, so a RUN-mode
                # namespace is needed even though the profile block
                # itself is mode-agnostic.
                # _memory_context_text reads job.worktree_path +
                # MEMORY.md; stub a tiny worktree with an empty
                # MEMORY.md so the full prefetch pipeline runs and the
                # ordering assertion (<operator-profile> before
                # <memory-context>) is meaningful.
                ws = tmp_p / "wt"
                ws.mkdir(parents=True, exist_ok=True)
                (ws / "MEMORY.md").write_text("# stub\n", encoding="utf-8")
                job = SimpleNamespace(
                    worktree_path=ws, last_event="starting", mode=runner_mod.JobMode.RUN,
                )
                prefetched = r._prefetch_memory(job)

                for needle in (
                    '<operator-profile name="TestOp"',
                    'language="en"',
                    'style="verbose"',
                    'standing="test-rig"',
                    "Default reply language: en",
                    "Default tone: verbose",
                    "Setup: test-rig",
                ):
                    if needle not in prefetched:
                        return CheckResult(
                            name, False,
                            f"missing {needle!r} in prefetch output",
                        )
                # Block must come before <memory-context> so the agent
                # sees identity first, then todays memories.
                profile_idx = prefetched.find("<operator-profile")
                memory_idx = prefetched.find("<memory-context")
                if profile_idx == -1 or memory_idx == -1 or profile_idx > memory_idx:
                    return CheckResult(
                        name, False,
                        f"<operator-profile> must precede <memory-context>; profile_idx={profile_idx} memory_idx={memory_idx}",
                    )
                return CheckResult(
                    name, True,
                    f"<operator-profile> is prepended with all 4 attrs and prose body, before <memory-context>",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- onboarding round-B: day-brief ------------------------------------
# Onboarding-B: the first job of each user-local day gets a <day-brief>
# block with yesterdays journal preview, todays MEMORY.md preview, and
# the last 3 jobs summaries. State is a one-line date stamp in
# codex_memory_root/state/; subsequent jobs the same day get "" (no
# recap). The 2 tests pin the first-of-day / second-of-day contract.

def _test_day_brief_fires_on_first_job_of_day() -> CheckResult:
    """Onboarding-B: on the first call (no state file), _day_brief_text
    returns a <day-brief> block with all 3 sections (yesterdays
    journal, todays MEMORY.md, recent jobs) and writes the state
    file with todays date. This pins the warm-start contract: a fresh
    days first job gets a snapshot, not a cold start."""
    name = "behavior: _day_brief_text returns a 3-section brief on the first job of the day and writes state (onboarding-B)"
    try:
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
                r = runner_mod.CodexRunner(settings)

                # No state file pre-written - this is the first call.
                state_path = r._day_brief_state_path()
                if state_path.exists():
                    return CheckResult(
                        name, False,
                        f"precondition: state file should not exist yet; found at {state_path}",
                    )

                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                brief = r._day_brief_text()

                for needle in (
                    f'<day-brief date="{today_str}" first-job-of-day="true">',
                    "## Yesterday\u0027s journal (",
                    "## Today\u0027s MEMORY.md",
                    "## Recent jobs (last 3)",
                    "</day-brief>",
                ):
                    if needle not in brief:
                        return CheckResult(
                            name, False,
                            f"missing {needle!r} in first-of-day brief",
                        )
                # State file must now contain todays date.
                if not state_path.exists():
                    return CheckResult(
                        name, False,
                        f"state file should be written at {state_path} but was not created",
                    )
                written_date = state_path.read_text(encoding="utf-8").strip()
                if written_date != today_str:
                    return CheckResult(
                        name, False,
                        f"state file date mismatch: expected {today_str!r}, got {written_date!r}",
                    )
                return CheckResult(
                    name, True,
                    f"first-of-day brief delivered, state file written with {today_str}: {len(brief)} chars",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_day_brief_skipped_on_second_job_of_day() -> CheckResult:
    """Onboarding-B: on the second call (state file with todays date),
    _day_brief_text returns "" so we dont repeat the recap on every
    message. This pins the one-brief-per-day contract; the agent
    should not see the day-brief twice in a single user-local day."""
    name = "behavior: _day_brief_text returns empty on the second job of the same day (onboarding-B)"
    try:
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
                r = runner_mod.CodexRunner(settings)

                # Pre-write the state file with todays UTC date so
                # _day_brief_texts check fires and returns "".
                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                state_path = r._day_brief_state_path()
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(today_str, encoding="utf-8")

                brief = r._day_brief_text()

                if brief != "":
                    return CheckResult(
                        name, False,
                        f"expected '' (second-of-day is skipped); got {brief!r}",
                    )
                # State file must still be todays date (not clobbered).
                if state_path.read_text(encoding="utf-8").strip() != today_str:
                    return CheckResult(
                        name, False,
                        f"state file should be unchanged at {today_str!r} but was modified",
                    )
                return CheckResult(
                    name, True,
                    f"second-of-day returns empty (one-brief-per-day contract): {brief!r}",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")

# ---- onboarding round-C: operator.json overrides load_settings ----
# Onboarding-C: load_settings reads codex_memory_root/operator.json
# at startup and uses its 4 operator_* fields to override the
# deployment-time .env values. The JSON file is written by the
# /onboard Telegram conversation handler in bot.py; this loader
# contract is the persistence half of the round (the bot half is
# smoke-tested by the deploy gate on the VPS, not by the env-free
# chain). The 2 tests pin both paths: operator.json present
# (overrides win) and operator.json absent (env defaults survive).

def _test_load_settings_reads_operator_json_overrides() -> CheckResult:
    """Onboarding-C: when codex_memory_root/operator.json exists, its
    4 operator_* fields override the .env values at load_settings
    time. This pins the persistence-wins semantics: the operator's
    /onboard choices survive across bot restarts and take precedence
    over the deployer's defaults."""
    name = "behavior: load_settings reads operator.json and overrides the 4 operator_* fields (onboarding-C)"
    try:
        import config as config_mod
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            memory_root = tmp_p / "memory"
            memory_root.mkdir(parents=True, exist_ok=True)
            (memory_root / "operator.json").write_text(
                json.dumps({
                    "operator_name": "Alice",
                    "operator_language": "en",
                    "operator_style": "verbose",
                    "operator_standing": "team-lead",
                }),
                encoding="utf-8",
            )
            overrides = {
                "TELEGRAM_BOT_TOKEN": "fake-token",
                "TELEGRAM_ALLOWED_USER_ID": "0",
                "CODEX_WORKSPACE_ROOT": str(tmp_p / "ws"),
                "CODEX_BIN": "codex",
                "CODEX_MEMORY_ROOT": str(memory_root),
                "USER_TIMEZONE": "UTC",
                # .env is set to conflicting values on purpose to
                # prove operator.json wins.
                "OPERATOR_NAME": "EnvName",
                "OPERATOR_LANGUAGE": "env-lang",
                "OPERATOR_STYLE": "env-style",
                "OPERATOR_STANDING": "env-standing",
            }
            with mock.patch.dict(os.environ, overrides, clear=False):
                settings = config_mod.load_settings()
                checks = {
                    "operator_name": ("Alice", settings.operator_name),
                    "operator_language": ("en", settings.operator_language),
                    "operator_style": ("verbose", settings.operator_style),
                    "operator_standing": ("team-lead", settings.operator_standing),
                }
                for label, (expected, got) in checks.items():
                    if got != expected:
                        return CheckResult(
                            name, False,
                            f"{label}: expected {expected!r}, got {got!r} (operator.json should win over .env)",
                        )
                return CheckResult(
                    name, True,
                    f"operator.json wins over .env for all 4 fields: name=Alice, language=en, style=verbose, standing=team-lead",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_load_settings_falls_back_to_env_when_no_operator_json() -> CheckResult:
    """Onboarding-C: when codex_memory_root/operator.json does NOT
    exist (fresh deploy, pre-/onboard), load_settings uses the
    .env values (or the project defaults if .env is empty). This
    pins the no-profile-yet path so a fresh deploy starts with
    sensible defaults before the user has run /onboard."""
    name = "behavior: load_settings falls back to env defaults when codex_memory_root/operator.json is absent (onboarding-C)"
    try:
        import config as config_mod
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            memory_root = tmp_p / "memory"
            memory_root.mkdir(parents=True, exist_ok=True)
            # Pre-condition: no operator.json
            assert not (memory_root / "operator.json").exists()
            overrides = {
                "TELEGRAM_BOT_TOKEN": "fake-token",
                "TELEGRAM_ALLOWED_USER_ID": "0",
                "CODEX_WORKSPACE_ROOT": str(tmp_p / "ws"),
                "CODEX_BIN": "codex",
                "CODEX_MEMORY_ROOT": str(memory_root),
                "USER_TIMEZONE": "UTC",
                "OPERATOR_NAME": "EnvName",
                "OPERATOR_LANGUAGE": "en",
                "OPERATOR_STYLE": "balanced",
            }
            with mock.patch.dict(os.environ, overrides, clear=False):
                settings = config_mod.load_settings()
                checks = {
                    "operator_name": ("EnvName", settings.operator_name),
                    "operator_language": ("en", settings.operator_language),
                    "operator_style": ("balanced", settings.operator_style),
                }
                for label, (expected, got) in checks.items():
                    if got != expected:
                        return CheckResult(
                            name, False,
                            f"{label}: expected {expected!r} (from .env), got {got!r}",
                        )
                # operator_standing has no .env override here, so it
                # should fall through to the project default.
                if settings.operator_standing != "personal-scale, single operator":
                    return CheckResult(
                        name, False,
                        f"operator_standing: expected project default 'personal-scale, single operator', got {settings.operator_standing!r}",
                    )
                return CheckResult(
                    name, True,
                    f"no operator.json -> .env values used; project default for operator_standing: name=EnvName, language=en, style=balanced, standing=personal-scale, single operator",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")

def main() -> int:
    tests = [
        # AST contracts
        _test_is_user_visible_event_exists,
        _test_is_user_visible_event_keys,
        _test_should_send_event_progress_signature,
        _test_should_send_event_progress_delegates,
        _test_agent_message_text_exists,
        _test_agent_message_text_extraction,
        _test_event_summary_streams_prose,
        _test_typing_loop_stays_alive,
        _test_tool_call_name_exists,
        _test_tool_call_name_extraction,
        _test_tool_call_event_is_user_visible,
        _test_event_summary_tool_indicator,
        # Behavior contracts
        _test_placeholder_sent_once,
        _test_progress_uses_edit,
        _test_typing_loop_cancelled,
        _test_final_answer_via_edit,
        _test_edit_failure_falls_back_to_send,
        _test_edit_broken_latch,
        _test_placeholder_send_failure_falls_back,
        # Chat-feel round-7 contracts
        _test_no_op_edit_skipped,
        _test_no_op_edit_skipped_after_truncation_collision,
        # Chat-feel round-2 contracts
        _test_command_execution_tool_indicator,
        _test_lifecycle_events_suppressed,
        _test_no_event_type_prefix,
        _test_consecutive_dedup,
        # Chat-feel round-4 contracts
        _test_is_prose_event_exists,
        _test_is_prose_event_classification,
        _test_growing_gate_on_prose,
        # Chat-feel round-5 contracts
        _test_thinking_indicator_appears_after_threshold,
        _test_thinking_indicator_clears_on_prose,
        _test_thinking_indicator_clears_on_tool_call,
        _test_thinking_indicator_skipped_for_short_reasoning,
        # Chat-feel round-6 contracts
        _test_tool_pulse_appears_after_threshold,
        _test_tool_pulse_clears_on_completion,
        _test_tool_pulse_skipped_for_short_call,
        _test_tool_pulse_respects_interval,
        # Chat-feel round-8 contracts
        _test_first_prose_fires_immediately_after_placeholder,
        _test_subsequent_prose_respects_cooldown,
        _test_subsequent_prose_fires_after_cooldown,
        # Onboarding-A contracts
        _test_operator_profile_block_in_prefetch,
        # Onboarding-B contracts
        _test_day_brief_fires_on_first_job_of_day,
        _test_day_brief_skipped_on_second_job_of_day,
        # Onboarding-C contracts
        _test_load_settings_reads_operator_json_overrides,
        _test_load_settings_falls_back_to_env_when_no_operator_json,
    ]
    results = [t() for t in tests]
    print_results(results)
    ok = all(r.ok for r in results)
    print("progress smoke ok" if ok else "progress smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
