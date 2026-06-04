#!/usr/bin/env python3
"""Regression guard for bot._handle_memo_fast_path.

The _handle_memo_fast_path handler (bot.py:73-101) is the only entry point
for /memo and the plain-text "记 x" / "记住 x" / "备忘 x" / etc. commands in
production. It writes to today's MEMORY.md directly without going through
codex. Contract:

  - Module-level AsyncFunctionDef (not a method), signature
    (update: Update, prompt: str) -> None
  - body has no Raise nodes (both classify_memo and append_memo failures
    are caught and surface as a reply; no crash path)
  - body calls runner.classify_memo(content) exactly once, in the
    no-tag branch only
  - body calls runner.append_memo(category, content, auto_timestamp=...)
    exactly once, where auto_ts is derived from category
  - body references MEMORY_KEYWORD_PATTERN and CATEGORY_PATTERN
  - prompt "记" alone -> reply contains "Usage", no I/O
  - prompt "记 [preference] X" -> category from tag, classifier skipped,
    content has the tag stripped, auto_timestamp=False
  - prompt "记 AAPL 310 USD" -> classifier called with content, returned
    category used, auto_timestamp=True for "fact"
  - append_memo raises -> reply contains "记下来的时候出了点问题" and the
    error text, handler does not crash

These tests are AST-only for the static shape, plus behavioral checks
using mock.patch on bot.runner.{classify_memo, append_memo}. No HTTP,
no real LLM, no real git workspace.

Run with:  .venv/bin/python scripts/memo_fastpath_smoke.py
Exit code: 0 if all pass, 1 otherwise.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.harness_common import CheckResult, print_results


BOT_PY = Path(__file__).resolve().parents[1] / "bot.py"


# ---- AST helpers ---------------------------------------------------------

def _parse_bot() -> ast.Module:
    return ast.parse(BOT_PY.read_text(encoding="utf-8"))


def _function_def(tree: ast.Module, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef | None:
    """Find a module-level function by name (not inside any class)."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _signature_str(func: ast.AsyncFunctionDef | ast.FunctionDef) -> str:
    """Render a function's full signature as `ast.unparse(args) -> ast.unparse(returns)`."""
    returns = ast.unparse(func.returns) if func.returns is not None else "None"
    return f"{ast.unparse(func.args)} -> {returns}"


def _walk_skip_nested_funcs(node):
    """ast.walk that does not descend into nested FunctionDef/AsyncFunctionDef.

    We only want to inspect the body's own nodes, not the inside of helper
    closures or nested functions.
    """
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield from _walk_skip_nested_funcs(child)


# ---- env + bot fixture ---------------------------------------------------

@contextmanager
def _bot_in_tmp_env(tmp: Path):
    """Import bot.py with env vars pointing at tmp paths; restore on exit.

    bot.py constructs `runner = CodexRunner(settings)` at module load, which
    calls load_settings() and (via load_dotenv + mkdir) creates
    memory_root/{JOURNAL,snapshots,state}. We override the env so all of
    that happens inside the temp dir.
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
        # Late import so load_settings() sees our overrides.
        import bot  # type: ignore[import-not-found]  # noqa: PLC0415
        yield bot


def _make_fake_update():
    """Build a fake telegram Update with a reply_text AsyncMock."""
    update = mock.MagicMock()
    update.effective_message = mock.MagicMock()
    update.effective_message.reply_text = mock.AsyncMock()
    return update


# ---- AST tests -----------------------------------------------------------

def _test_signature() -> CheckResult:
    name = "AST: _handle_memo_fast_path is module-level AsyncFunctionDef with signature (update: Update, prompt: str) -> None"
    try:
        tree = _parse_bot()
        func = _function_def(tree, "_handle_memo_fast_path")
        if not isinstance(func, ast.AsyncFunctionDef):
            return CheckResult(
                name, False,
                f"expected AsyncFunctionDef at module level, got {type(func).__name__ if func else 'None'}",
            )
        actual = _signature_str(func)
        # ast.unparse drops outer parens and keeps type annotations as-is.
        expected = "update: Update, prompt: str -> None"
        if actual != expected:
            return CheckResult(name, False, f"signature mismatch: got {actual!r}, expected {expected!r}")
        return CheckResult(name, True, actual)
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_no_raises() -> CheckResult:
    name = "AST: _handle_memo_fast_path body has no Raise nodes (all errors caught)"
    try:
        func = _function_def(_parse_bot(), "_handle_memo_fast_path")
        if not isinstance(func, ast.AsyncFunctionDef):
            return CheckResult(name, False, "function missing (caught by previous test)")
        raises = [
            n for n in _walk_skip_nested_funcs(func)
            if isinstance(n, ast.Raise) and n.exc is not None
        ]
        if raises:
            preview = "; ".join(f"line {r.lineno}" for r in raises[:5])
            return CheckResult(name, False, f"{len(raises)} raise(s): {preview}")
        return CheckResult(name, True, "0 Raise nodes in body (skipping nested defs)")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_calls_classify_memo() -> CheckResult:
    name = "AST: body calls runner.classify_memo(content) exactly once (no-tag branch only)"
    try:
        func = _function_def(_parse_bot(), "_handle_memo_fast_path")
        if not isinstance(func, ast.AsyncFunctionDef):
            return CheckResult(name, False, "function missing")
        calls = [
            n for n in _walk_skip_nested_funcs(func)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "runner"
            and n.func.attr == "classify_memo"
        ]
        if len(calls) != 1:
            return CheckResult(name, False, f"expected 1 classify_memo call, got {len(calls)}")
        return CheckResult(name, True, "runner.classify_memo(content) called once in body")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_calls_append_memo() -> CheckResult:
    name = "AST: body calls runner.append_memo(category, content, auto_timestamp=auto_ts) exactly once"
    try:
        func = _function_def(_parse_bot(), "_handle_memo_fast_path")
        if not isinstance(func, ast.AsyncFunctionDef):
            return CheckResult(name, False, "function missing")
        calls = [
            n for n in _walk_skip_nested_funcs(func)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "runner"
            and n.func.attr == "append_memo"
        ]
        if len(calls) != 1:
            return CheckResult(name, False, f"expected 1 append_memo call, got {len(calls)}")
        call = calls[0]
        if len(call.args) != 2:
            return CheckResult(name, False, f"expected 2 positional args, got {len(call.args)}")
        kw_names = {k.arg for k in call.keywords}
        if "auto_timestamp" not in kw_names:
            return CheckResult(name, False, f"missing auto_timestamp keyword; got {sorted(kw_names)}")
        return CheckResult(
            name, True,
            "runner.append_memo(category, content, auto_timestamp=auto_ts) called once",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_references_patterns() -> CheckResult:
    name = "AST: body references MEMORY_KEYWORD_PATTERN and CATEGORY_PATTERN"
    try:
        func = _function_def(_parse_bot(), "_handle_memo_fast_path")
        if not isinstance(func, ast.AsyncFunctionDef):
            return CheckResult(name, False, "function missing")
        names = {n.id for n in _walk_skip_nested_funcs(func) if isinstance(n, ast.Name)}
        needed = {"MEMORY_KEYWORD_PATTERN", "CATEGORY_PATTERN"}
        missing = needed - names
        if missing:
            return CheckResult(name, False, f"missing references: {sorted(missing)}")
        return CheckResult(name, True, "MEMORY_KEYWORD_PATTERN and CATEGORY_PATTERN both referenced")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- behavior tests ------------------------------------------------------

def _test_keyword_only() -> CheckResult:
    name = 'behavior: prompt "记" (keyword only) -> reply contains "Usage", no I/O calls'
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            for sub in ("ws", "task", "memory"):
                (tmp_p / sub).mkdir(parents=True, exist_ok=True)
            with _bot_in_tmp_env(tmp_p) as bot:
                update = _make_fake_update()
                fake_classify = mock.AsyncMock(return_value="fact")
                fake_append = mock.AsyncMock(return_value="记下了: x")
                with mock.patch.object(bot.runner, "classify_memo", fake_classify), \
                     mock.patch.object(bot.runner, "append_memo", fake_append):
                    asyncio.run(bot._handle_memo_fast_path(update, "记"))
                fake_classify.assert_not_called()
                fake_append.assert_not_called()
                if not update.effective_message.reply_text.call_args:
                    return CheckResult(name, False, "reply_text was not called")
                reply = update.effective_message.reply_text.call_args[0][0]
                if "Usage" not in reply:
                    return CheckResult(name, False, f"reply missing 'Usage': {reply!r}")
                return CheckResult(name, True, "reply says Usage, no classify_memo / append_memo calls")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_with_tag() -> CheckResult:
    name = 'behavior: prompt "记 [preference] 用 pnpm" -> category from tag, classifier skipped, auto_timestamp=False'
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            for sub in ("ws", "task", "memory"):
                (tmp_p / sub).mkdir(parents=True, exist_ok=True)
            with _bot_in_tmp_env(tmp_p) as bot:
                update = _make_fake_update()
                fake_classify = mock.AsyncMock(return_value="fact")
                fake_append = mock.AsyncMock(return_value="记下了: preference * 用 pnpm (1)")
                with mock.patch.object(bot.runner, "classify_memo", fake_classify), \
                     mock.patch.object(bot.runner, "append_memo", fake_append):
                    asyncio.run(bot._handle_memo_fast_path(update, "记 [preference] 用 pnpm"))
                fake_classify.assert_not_called()
                fake_append.assert_called_once_with("preference", "用 pnpm", auto_timestamp=False)
                reply = update.effective_message.reply_text.call_args[0][0]
                if "记下了" not in reply:
                    return CheckResult(name, False, f"reply missing 记下了: {reply!r}")
                return CheckResult(
                    name, True,
                    'category="preference", content="用 pnpm", auto_timestamp=False, classifier skipped',
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_no_tag_uses_classifier() -> CheckResult:
    name = 'behavior: prompt "记 AAPL 310 USD" -> classifier called with content, returned category used, auto_timestamp=True for "fact"'
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            for sub in ("ws", "task", "memory"):
                (tmp_p / sub).mkdir(parents=True, exist_ok=True)
            with _bot_in_tmp_env(tmp_p) as bot:
                update = _make_fake_update()
                fake_classify = mock.AsyncMock(return_value="fact")
                fake_append = mock.AsyncMock(return_value="记下了: fact * AAPL 310 USD (1)")
                with mock.patch.object(bot.runner, "classify_memo", fake_classify), \
                     mock.patch.object(bot.runner, "append_memo", fake_append):
                    asyncio.run(bot._handle_memo_fast_path(update, "记 AAPL 310 USD"))
                fake_classify.assert_called_once_with("AAPL 310 USD")
                fake_append.assert_called_once_with("fact", "AAPL 310 USD", auto_timestamp=True)
                reply = update.effective_message.reply_text.call_args[0][0]
                if "记下了" not in reply:
                    return CheckResult(name, False, f"reply missing 记下了: {reply!r}")
                return CheckResult(
                    name, True,
                    'classify_memo("AAPL 310 USD") -> "fact", append_memo("fact", "AAPL 310 USD", auto_timestamp=True)',
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_append_memo_raises() -> CheckResult:
    name = 'behavior: append_memo raises ValueError -> reply contains "记下来的时候出了点问题" + error text, no crash'
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            for sub in ("ws", "task", "memory"):
                (tmp_p / sub).mkdir(parents=True, exist_ok=True)
            with _bot_in_tmp_env(tmp_p) as bot:
                update = _make_fake_update()
                fake_classify = mock.AsyncMock(return_value="fact")
                fake_append = mock.AsyncMock(side_effect=ValueError("disk full"))
                with mock.patch.object(bot.runner, "classify_memo", fake_classify), \
                     mock.patch.object(bot.runner, "append_memo", fake_append):
                    asyncio.run(bot._handle_memo_fast_path(update, "记 AAPL 310 USD"))
                fake_append.assert_called_once()
                if not update.effective_message.reply_text.call_args:
                    return CheckResult(name, False, "reply_text was not called on error path")
                reply = update.effective_message.reply_text.call_args[0][0]
                if "记下来的时候出了点问题" not in reply:
                    return CheckResult(name, False, f"reply missing 记下来的时候出了点问题: {reply!r}")
                if "disk full" not in reply:
                    return CheckResult(name, False, f"reply missing error text 'disk full': {reply!r}")
                return CheckResult(
                    name, True,
                    "error caught, reply has 记下来的时候出了点问题 + 'disk full', handler did not crash",
                )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- main ----------------------------------------------------------------

def main() -> int:
    tests = [
        _test_signature,
        _test_no_raises,
        _test_calls_classify_memo,
        _test_calls_append_memo,
        _test_references_patterns,
        _test_keyword_only,
        _test_with_tag,
        _test_no_tag_uses_classifier,
        _test_append_memo_raises,
    ]
    results = [t() for t in tests]
    print_results(results)
    ok = all(r.ok for r in results)
    print("memo_fastpath smoke ok" if ok else "memo_fastpath smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
