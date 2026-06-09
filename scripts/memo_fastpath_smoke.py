#!/usr/bin/env python3
"""Regression guard for the channel-agnostic memo fast path.

Pins the contract of handlers.memo.handle_memo (003 P0.1):

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
using FakeOutboundPort + mock.patch on runner.{classify_memo, append_memo}.
No HTTP, no real LLM, no real git workspace.

Run with:  .venv/bin/python scripts/memo_fastpath_smoke.py
Exit code: 0 if all pass, 1 otherwise.
"""
from __future__ import annotations

import ast
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channel import InboundMessage
from handlers.memo import handle_memo
from scripts.harness_common import CheckResult, print_results


HANDLERS_MEMO_PY = Path(__file__).resolve().parents[1] / "handlers" / "memo.py"


# ---- AST helpers ---------------------------------------------------------

def _parse_handlers_memo() -> ast.Module:
    return ast.parse(HANDLERS_MEMO_PY.read_text(encoding="utf-8"))


def _function_def(tree: ast.Module, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _signature_str(func: ast.AsyncFunctionDef | ast.FunctionDef) -> str:
    returns = ast.unparse(func.returns) if func.returns is not None else "None"
    return f"{ast.unparse(func.args)} -> {returns}"


def _walk_skip_nested_funcs(node):
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield from _walk_skip_nested_funcs(child)


# ---- Fake harness --------------------------------------------------------

@dataclass
class FakeOutbound:
    replies: list[str] = field(default_factory=list)

    async def reply(self, msg, text):
        self.replies.append(text)
        return None

    async def send_new(self, msg, text):
        self.replies.append(text)
        return None

    async def edit_progress(self, msg, placeholder_id, text):
        return False

    async def reply_with_buttons(self, msg, text, buttons):
        self.replies.append(text)
        return None


def _msg(text: str) -> InboundMessage:
    return InboundMessage(
        channel="feishu",
        operator_id="ou_test",
        chat_id="chat-1",
        message_id="m-1",
        text=text,
    )


def _make_runner_stub() -> SimpleNamespace:
    return SimpleNamespace(
        classify_memo=mock.AsyncMock(return_value="fact"),
        append_memo=mock.AsyncMock(return_value="记下了: x"),
    )


# ---- AST tests -----------------------------------------------------------

def _test_signature() -> CheckResult:
    name = "AST: handlers.memo.handle_memo is module-level AsyncFunctionDef with channel-agnostic signature"
    try:
        func = _function_def(_parse_handlers_memo(), "handle_memo")
        if not isinstance(func, ast.AsyncFunctionDef):
            return CheckResult(name, False, "function missing or wrong kind")
        sig = _signature_str(func)
        if "msg: InboundMessage" not in sig or "port: OutboundPort" not in sig or "runner: CodexRunner" not in sig:
            return CheckResult(name, False, f"signature mismatch: {sig!r}")
        return CheckResult(name, True, sig)
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_no_raises() -> CheckResult:
    name = "AST: handle_memo body has no Raise nodes (all errors caught)"
    try:
        func = _function_def(_parse_handlers_memo(), "handle_memo")
        if not isinstance(func, ast.AsyncFunctionDef):
            return CheckResult(name, False, "function missing")
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
        func = _function_def(_parse_handlers_memo(), "handle_memo")
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
        func = _function_def(_parse_handlers_memo(), "handle_memo")
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
    name = "AST: module references MEMORY_KEYWORD_PATTERN and CATEGORY_PATTERN"
    try:
        tree = _parse_handlers_memo()
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
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
        port = FakeOutbound()
        runner = _make_runner_stub()
        asyncio.run(handle_memo(_msg("记"), port, runner))
        runner.classify_memo.assert_not_called()
        runner.append_memo.assert_not_called()
        if not port.replies:
            return CheckResult(name, False, "port.reply was not called")
        reply = port.replies[0]
        if "Usage" not in reply:
            return CheckResult(name, False, f"reply missing 'Usage': {reply!r}")
        return CheckResult(name, True, "reply says Usage, no classify_memo / append_memo calls")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_with_tag() -> CheckResult:
    name = 'behavior: prompt "记 [preference] 用 pnpm" -> category from tag, classifier skipped, auto_timestamp=False'
    try:
        port = FakeOutbound()
        runner = _make_runner_stub()
        asyncio.run(handle_memo(_msg("记 [preference] 用 pnpm"), port, runner))
        runner.classify_memo.assert_not_called()
        runner.append_memo.assert_called_once_with("preference", "用 pnpm", auto_timestamp=False)
        reply = port.replies[0]
        if "记下了" not in reply:
            return CheckResult(name, False, f"reply missing 记下了: {reply!r}")
        return CheckResult(
            name, True,
            'category="preference", content="用 pnpm", auto_timestamp=False, classifier skipped',
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_no_tag_uses_classifier() -> CheckResult:
    name = 'behavior: prompt "记 AAPL 310 USD" -> classifier called, returned category used, auto_timestamp=True for "fact"'
    try:
        port = FakeOutbound()
        runner = _make_runner_stub()
        asyncio.run(handle_memo(_msg("记 AAPL 310 USD"), port, runner))
        runner.classify_memo.assert_called_once_with("AAPL 310 USD")
        runner.append_memo.assert_called_once_with("fact", "AAPL 310 USD", auto_timestamp=True)
        reply = port.replies[0]
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
        port = FakeOutbound()
        runner = SimpleNamespace(
            classify_memo=mock.AsyncMock(return_value="fact"),
            append_memo=mock.AsyncMock(side_effect=ValueError("disk full")),
        )
        asyncio.run(handle_memo(_msg("记 AAPL 310 USD"), port, runner))
        runner.append_memo.assert_called_once()
        reply = port.replies[0]
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
