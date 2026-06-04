#!/usr/bin/env python3
"""Regression guard for CodexRunner.classify_memo's "never raise" contract.

classify_memo is called from the 12pm reclassify-unfiled path and the
inline append-memo flow. Its job is to land anything it can't classify
in "unfiled" so the curator picks it up later. If it ever raises, the
caller (a 12pm cron or a hot user path) crashes mid-batch. The
contract is documented at runner.py:997-1054 and is the one invariant
that absolutely must not regress.

These tests are AST-only for the "never raise" / signature / return
contract, plus a tiny env-gated behavioral check for the no-key
fast-fail (no HTTP). The full HTTP path is exercised by memo_smoke in
make smoke-all.

Run with:  .venv/bin/python scripts/classify_memo_smoke.py
Exit code: 0 if all pass, 1 otherwise.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from runner import CodexRunner
from scripts.harness_common import CheckResult, print_results


RUNNER_PY = Path(__file__).resolve().parents[1] / "runner.py"


# ---- AST helpers ---------------------------------------------------------

def _class_def(tree: ast.Module, name: str) -> ast.ClassDef | None:
    return next(
        (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == name),
        None,
    )


def _method_def(class_node: ast.ClassDef, name: str) -> ast.AsyncFunctionDef | None:
    return next(
        (n for n in class_node.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name),
        None,
    )


def _parse_runner() -> ast.Module:
    return ast.parse(RUNNER_PY.read_text(encoding="utf-8"))


def _walk_skip_nested_funcs(node):
    """Like ast.walk, but does not descend into nested FunctionDef/AsyncFunctionDef.

    classify_memo's body contains a small inner helper `def _post()`
    that returns a decoded HTTP body. We don't want its Return to
    count toward classify_memo's return contract -- it's an internal
    detail, not a category the caller will ever see.
    """
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield from _walk_skip_nested_funcs(child)



# ---- branch tests --------------------------------------------------------

def _test_method_exists_and_async() -> CheckResult:
    """classify_memo must be an async method on CodexRunner (not module-level)."""
    name = "AST: classify_memo is an AsyncFunctionDef on CodexRunner"
    try:
        tree = _parse_runner()
        cls = _class_def(tree, "CodexRunner")
        if cls is None:
            return CheckResult(name, False, "class CodexRunner not found")
        method = _method_def(cls, "classify_memo")
        if method is None:
            return CheckResult(name, False, "method classify_memo not found on CodexRunner")
        if not isinstance(method, ast.AsyncFunctionDef):
            return CheckResult(name, False, f"classify_memo is {type(method).__name__}, not AsyncFunctionDef")
        return CheckResult(name, True, "AsyncFunctionDef on CodexRunner")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_signature() -> CheckResult:
    """Pin (self, content: str) -> str. Renaming content breaks call sites."""
    name = "AST: classify_memo signature is (self, content: str) -> str"
    try:
        tree = _parse_runner()
        method = _method_def(_class_def(tree, "CodexRunner"), "classify_memo")
        if method is None:
            return CheckResult(name, False, "method missing (caught by previous test)")
        args = method.args
        # Positional args: self, content
        pos = args.args
        if len(pos) != 2:
            return CheckResult(name, False, f"expected 2 positional args, got {len(pos)}: {[a.arg for a in pos]}")
        if pos[0].arg != "self":
            return CheckResult(name, False, f"first arg is {pos[0].arg!r}, expected 'self'")
        if pos[1].arg != "content":
            return CheckResult(name, False, f"second arg is {pos[1].arg!r}, expected 'content'")
        if pos[1].annotation is None or not (
            isinstance(pos[1].annotation, ast.Name) and pos[1].annotation.id == "str"
        ):
            return CheckResult(name, False, f"content annotation is not 'str': {ast.dump(pos[1].annotation)}")
        if method.returns is None or not (
            isinstance(method.returns, ast.Name) and method.returns.id == "str"
        ):
            return CheckResult(name, False, f"return annotation is not 'str': {ast.dump(method.returns)}")
        return CheckResult(name, True, "(self, content: str) -> str")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_never_raises() -> CheckResult:
    """The whole point of classify_memo: no `raise` in the body."""
    name = "AST: classify_memo body has no Raise nodes (never-raise contract)"
    try:
        tree = _parse_runner()
        method = _method_def(_class_def(tree, "CodexRunner"), "classify_memo")
        if method is None:
            return CheckResult(name, False, "method missing (caught by previous test)")
        raises = [n for n in ast.walk(method) if isinstance(n, ast.Raise)]
        if raises:
            lines = sorted({r.lineno for r in raises})
            return CheckResult(name, False, f"found {len(raises)} Raise node(s) at lines {lines}")
        nodes = sum(1 for _ in _walk_skip_nested_funcs(method))
        return CheckResult(name, True, f"0 Raise nodes in {nodes} AST nodes (skipping nested defs)")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_returns_are_categories() -> CheckResult:
    """Every return must yield a MEMO_CATEGORIES member (string literal in the set, or a Name that holds one).

    Catches the regression where someone `return ""` or `return None` --
    both would break the curator's `category in {"preference", ...}` switch
    at runner.py:1006.
    """
    name = "AST: every Return in classify_memo is a category literal or Name"
    try:
        tree = _parse_runner()
        method = _method_def(_class_def(tree, "CodexRunner"), "classify_memo")
        if method is None:
            return CheckResult(name, False, "method missing (caught by previous test)")
        categories = set(CodexRunner.MEMO_CATEGORIES)
        offenders: list[tuple[int, str]] = []
        for ret in (n for n in _walk_skip_nested_funcs(method) if isinstance(n, ast.Return)):
            v = ret.value
            if v is None:
                offenders.append((ret.lineno, "bare `return`"))
                continue
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                if v.value not in categories:
                    offenders.append((ret.lineno, f"return {v.value!r} not in MEMO_CATEGORIES"))
                continue
            if isinstance(v, ast.Name):
                # `return category` inside the for-loop is the only Name return.
                # We can't resolve the binding statically, but the source line
                # `if category in text` shows it must be a categories member.
                continue
            # Anything else (Call, Attribute, Subscript of an unknown value) is
            # suspicious enough to flag.
            offenders.append((ret.lineno, f"return {type(v).__name__}, not Constant/Name"))
        if offenders:
            preview = "; ".join(f"line {ln}: {why}" for ln, why in offenders[:5])
            return CheckResult(name, False, f"{len(offenders)} offender(s): {preview}")
        return CheckResult(name, True, f"all returns are str-literal-in-categories or Name")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_except_clauses_have_bodies() -> CheckResult:
    """Every `except` block must have a body that doesn't re-raise. Bare `except: raise` would violate the contract."""
    name = "AST: every except handler in classify_memo ends in a return, not a raise"
    try:
        tree = _parse_runner()
        method = _method_def(_class_def(tree, "CodexRunner"), "classify_memo")
        if method is None:
            return CheckResult(name, False, "method missing (caught by previous test)")
        offenders: list[tuple[int, str]] = []
        for try_node in (n for n in _walk_skip_nested_funcs(method) if isinstance(n, ast.Try)):
            for handler in try_node.handlers:
                has_raise = any(isinstance(s, ast.Raise) and s.exc is not None for s in handler.body)
                has_return = any(isinstance(s, ast.Return) for s in handler.body)
                if has_raise:
                    offenders.append((handler.lineno, "handler contains `raise ...`"))
                elif not has_return:
                    offenders.append((handler.lineno, "handler has no Return (control may fall through)"))
        if offenders:
            preview = "; ".join(f"line {ln}: {why}" for ln, why in offenders[:5])
            return CheckResult(name, False, f"{len(offenders)} offender(s): {preview}")
        n_handlers = sum(1 for n in _walk_skip_nested_funcs(method) if isinstance(n, ast.ExceptHandler))
        return CheckResult(name, True, f"all {n_handlers} handlers end in return")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- behavioral tests (no HTTP) ------------------------------------------

def _test_empty_content_unfiled() -> CheckResult:
    """No key path: empty content -> 'unfiled' (early return before HTTP)."""
    name = "behavior: empty content -> 'unfiled' (early return, no key path)"
    try:
        settings = Settings(
            telegram_bot_token="fake",
            telegram_allowed_user_id=0,
            codex_workspace_root=Path("/tmp"),
            codex_bin="codex",
            codex_task_root=Path("/tmp"),
            codex_model=None,
            codex_timeout_seconds=60,
            telegram_progress_seconds=20,
            codex_retry_429_delays_seconds=(),
            codex_memory_root=Path("/tmp"),
            user_timezone="UTC",
        )
        runner = CodexRunner(settings)
        with mock.patch.dict(os.environ, {"MINIMAX_API_KEY": ""}, clear=False):
            result = asyncio.run(runner.classify_memo(""))
        if result != "unfiled":
            return CheckResult(name, False, f"expected 'unfiled', got {result!r}")
        return CheckResult(name, True, "empty content + empty key -> 'unfiled'")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_whitespace_content_unfiled() -> CheckResult:
    """Whitespace-only content is `not content.strip()` -> 'unfiled' before HTTP."""
    name = "behavior: whitespace-only content -> 'unfiled' (early return)"
    try:
        settings = Settings(
            telegram_bot_token="fake",
            telegram_allowed_user_id=0,
            codex_workspace_root=Path("/tmp"),
            codex_bin="codex",
            codex_task_root=Path("/tmp"),
            codex_model=None,
            codex_timeout_seconds=60,
            telegram_progress_seconds=20,
            codex_retry_429_delays_seconds=(),
            codex_memory_root=Path("/tmp"),
            user_timezone="UTC",
        )
        runner = CodexRunner(settings)
        with mock.patch.dict(os.environ, {"MINIMAX_API_KEY": "test-key"}, clear=False):
            result = asyncio.run(runner.classify_memo("   \n\t  "))
        if result != "unfiled":
            return CheckResult(name, False, f"expected 'unfiled', got {result!r}")
        return CheckResult(name, True, "whitespace content -> 'unfiled' without HTTP")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_no_api_key_unfiled() -> CheckResult:
    """Key present in env but empty: must not HTTP, must not raise, must return 'unfiled'."""
    name = "behavior: no API key + real content -> 'unfiled' (no HTTP, no raise)"
    try:
        settings = Settings(
            telegram_bot_token="fake",
            telegram_allowed_user_id=0,
            codex_workspace_root=Path("/tmp"),
            codex_bin="codex",
            codex_task_root=Path("/tmp"),
            codex_model=None,
            codex_timeout_seconds=60,
            telegram_progress_seconds=20,
            codex_retry_429_delays_seconds=(),
            codex_memory_root=Path("/tmp"),
            user_timezone="UTC",
        )
        runner = CodexRunner(settings)
        # Mock urlopen to detect any HTTP attempt -- the test fails if it gets called.
        with mock.patch.dict(os.environ, {"MINIMAX_API_KEY": " "}, clear=False), \
             mock.patch("urllib.request.urlopen") as urlopen_mock:
            result = asyncio.run(runner.classify_memo("user prefers dark mode"))
        if urlopen_mock.called:
            return CheckResult(name, False, "urlopen was called despite empty/whitespace API key")
        if result != "unfiled":
            return CheckResult(name, False, f"expected 'unfiled', got {result!r}")
        return CheckResult(name, True, "no HTTP, no raise, 'unfiled' returned")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- entrypoint ----------------------------------------------------------

def main() -> int:
    results = [
        _test_method_exists_and_async(),
        _test_signature(),
        _test_never_raises(),
        _test_returns_are_categories(),
        _test_except_clauses_have_bodies(),
        _test_empty_content_unfiled(),
        _test_whitespace_content_unfiled(),
        _test_no_api_key_unfiled(),
    ]
    ok = print_results(results)
    print("classify_memo smoke ok" if ok else "classify_memo smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
