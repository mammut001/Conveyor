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
                # function_call name) still gets JSON-dumped.
                opaque_line = json.dumps(
                    {"type": "item.updated", "item": {"type": "custom_thing", "payload": 1}}
                )
                opaque_summary = r._event_summary(opaque_line)
                if "custom_thing" not in opaque_summary:
                    return CheckResult(
                        name, False,
                        f"opaque item should be JSON-dumped; got {opaque_summary!r}",
                    )
                return CheckResult(
                    name, True,
                    "agent_message -> prose; function_call -> 🔧 indicator; opaque -> JSON",
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
    name = 'behavior: _event_summary renders function_call as "🔧 name..." progress line'
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
                if not summary.startswith("item.updated:"):
                    return CheckResult(
                        name, False,
                        f"summary should be prefixed with the event type; got {summary!r}",
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
    ]
    results = [t() for t in tests]
    print_results(results)
    ok = all(r.ok for r in results)
    print("progress smoke ok" if ok else "progress smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
