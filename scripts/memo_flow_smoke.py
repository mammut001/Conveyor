#!/usr/bin/env python3
"""Regression guard for CodexRunner.append_memo + reclassify_unfiled.

append_memo (runner.py:822-868) is the single entry point for MEMORY.md
writes. It is called from the inline `memorize` command and from the
12pm gate. Contract:
  - AsyncFunctionDef on CodexRunner
  - signature: (self, category: str, content: str, *,
                auto_timestamp: bool = False) -> str
  - raises ValueError for unknown category (before any I/O)
  - raises ValueError for empty/whitespace content (before any I/O)
  - on first call to a fresh worktree: writes MEMORY.md with the
    "# MEMORY.md -- YYYY-MM-DD" header, the "## <category>" section,
    and the bullet "- <content>"
  - on dedup: returns "已存在: <cat> * <preview> (跳过重复)" and does NOT
    append a second bullet

reclassify_unfiled (runner.py:896-985) is the 12pm gate's primary tool.
It walks every line in the `## unfiled` section of a MEMORY.md text,
re-calls classify_memo on each, and moves lines into a real category.
Contract:
  - AsyncFunctionDef on CodexRunner
  - signature: (self, content: str) -> tuple[str, int]
  - body has no Raise nodes (never-raise contract; classify_memo itself
    swallows and returns "unfiled")
  - re-calls classify_memo on each "- ..." line in the ## unfiled section
    and moves lines into the returned category
  - preserves unknown headings (e.g. user-written "## notes")
  - returns (new_content, moved_count) where count is # of lines moved
  - reassembles in MEMO_CATEGORIES order with "unfiled" last

These tests are AST-only for signature/never-raise, plus behavioral
checks using a temp worktree (for append_memo) and a mock for
classify_memo (for reclassify_unfiled). No HTTP, no real LLM, no real
git workspace.

Run with:  .venv/bin/python scripts/memo_flow_smoke.py
Exit code: 0 if all pass, 1 otherwise.
"""
from __future__ import annotations

import ast
import asyncio
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings
from runner import CodexRunner
from scripts.harness_common import CheckResult, print_results


RUNNER_PY = Path(__file__).resolve().parents[1] / "runner.py"


# ---- AST helpers ---------------------------------------------------------

def _parse_runner() -> ast.Module:
    return ast.parse(RUNNER_PY.read_text(encoding="utf-8"))


def _class_def(tree: ast.Module, name: str) -> ast.ClassDef | None:
    return next(
        (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == name),
        None,
    )


def _method_def(class_node: ast.ClassDef | None, name: str) -> ast.AsyncFunctionDef | None:
    if class_node is None:
        return None
    return next(
        (n for n in class_node.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name),
        None,
    )


def _signature_str(method: ast.AsyncFunctionDef) -> str:
    """Render a method's full signature as `ast.unparse(args) -> ast.unparse(returns)`."""
    returns = ast.unparse(method.returns) if method.returns is not None else "None"
    return f"{ast.unparse(method.args)} -> {returns}"


def _walk_skip_nested_funcs(node):
    """ast.walk that does not descend into nested FunctionDef/AsyncFunctionDef.

    We only want to inspect the body's own Raise/Return nodes, not the
    inside of helper closures or nested functions.
    """
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield from _walk_skip_nested_funcs(child)


# ---- fixtures ------------------------------------------------------------

def _make_runner(tmp: Path) -> CodexRunner:
    """Settings + CodexRunner with no real git workspace.

    CodexRunner.__init__ does not touch the filesystem. For tests that
    do not reach _ensure_today_worktree (the two error cases), this is
    enough. For tests that do, the caller must mock _ensure_today_worktree
    to skip the validate() + git worktree-add path.
    """
    return CodexRunner(
        Settings(
            telegram_bot_token="fake",
            telegram_allowed_user_id=0,
            codex_workspace_root=tmp / "ws",
            codex_bin="codex",
            codex_task_root=tmp / "task",
            codex_model=None,
            codex_timeout_seconds=60,
            telegram_progress_seconds=20,
            codex_retry_429_delays_seconds=(),
            codex_memory_root=tmp / "memory",
            user_timezone="UTC",
        )
    )


def _async_return(value):
    """Return an async callable that resolves to `value`, accepting any args."""
    async def _fn(*args, **kwargs):
        return value
    return _fn


# ---- AST tests -----------------------------------------------------------

def _test_append_memo_signature() -> CheckResult:
    name = "AST: append_memo on CodexRunner with signature (self, category: str, content: str, *, auto_timestamp: bool = False) -> str"
    try:
        method = _method_def(_class_def(_parse_runner(), "CodexRunner"), "append_memo")
        if method is None:
            return CheckResult(name, False, "method missing")
        sig = _signature_str(method)
        # ast.unparse normalizes: drops outer parens, no space around `=`.
        expected = "self, category: str, content: str, *, auto_timestamp: bool=False -> str"
        if sig != expected:
            return CheckResult(name, False, f"got {sig!r}, expected {expected!r}")
        return CheckResult(name, True, sig)
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_reclassify_unfiled_signature() -> CheckResult:
    name = "AST: reclassify_unfiled on CodexRunner with signature (self, content: str) -> tuple[str, int]"
    try:
        method = _method_def(_class_def(_parse_runner(), "CodexRunner"), "reclassify_unfiled")
        if method is None:
            return CheckResult(name, False, "method missing")
        sig = _signature_str(method)
        # ast.unparse drops the outer parens.
        expected = "self, content: str -> tuple[str, int]"
        if sig != expected:
            return CheckResult(name, False, f"got {sig!r}, expected {expected!r}")
        return CheckResult(name, True, sig)
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_reclassify_unfiled_never_raises() -> CheckResult:
    name = "AST: reclassify_unfiled body has no Raise nodes (never-raise contract)"
    try:
        method = _method_def(_class_def(_parse_runner(), "CodexRunner"), "reclassify_unfiled")
        if method is None:
            return CheckResult(name, False, "method missing (caught by previous test)")
        raises = [
            n for n in _walk_skip_nested_funcs(method)
            if isinstance(n, ast.Raise) and n.exc is not None
        ]
        if raises:
            preview = "; ".join(f"line {r.lineno}" for r in raises[:5])
            return CheckResult(name, False, f"{len(raises)} raise(s): {preview}")
        return CheckResult(name, True, "no Raise nodes in body (skipping nested defs)")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- append_memo behavioral tests ----------------------------------------

def _test_append_memo_unknown_category() -> CheckResult:
    name = "behavior: append_memo raises ValueError for unknown category (no I/O)"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            runner = _make_runner(Path(tmp))
            try:
                asyncio.run(runner.append_memo("not_a_category", "hello"))
            except ValueError as e:
                if "not_a_category" not in str(e):
                    return CheckResult(name, False, f"ValueError message missing category: {e}")
                return CheckResult(name, True, "ValueError raised, message includes category name")
            return CheckResult(name, False, "no ValueError raised")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_append_memo_empty_content() -> CheckResult:
    name = "behavior: append_memo raises ValueError for empty/whitespace content (no I/O)"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            runner = _make_runner(Path(tmp))
            for bad in ("", "   ", "\n\t  \n"):
                try:
                    asyncio.run(runner.append_memo("fact", bad))
                except ValueError as e:
                    if "empty" not in str(e).lower():
                        return CheckResult(name, False, f"ValueError message for {bad!r} unexpected: {e}")
                    continue
                return CheckResult(name, False, f"no ValueError for content {bad!r}")
            return CheckResult(name, True, "ValueError raised for '', '   ', '\\n\\t  \\n'")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_append_memo_writes_file() -> CheckResult:
    name = "behavior: append_memo writes fresh MEMORY.md with header + section + bullet"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            worktree = tmp_p / "wt"
            worktree.mkdir(parents=True, exist_ok=True)
            runner = _make_runner(tmp_p)
            with mock.patch.object(
                runner, "_ensure_today_worktree", _async_return(worktree.resolve())
            ):
                result = asyncio.run(
                    runner.append_memo("fact", "AAPL current price is 310 USD")
                )
            text = (worktree / "MEMORY.md").read_text(encoding="utf-8")
            if "# MEMORY.md " not in text:
                return CheckResult(name, False, f"missing header in: {text!r}")
            if "## fact" not in text:
                return CheckResult(name, False, f"missing '## fact' section in: {text!r}")
            if "- AAPL current price is 310 USD" not in text:
                return CheckResult(name, False, f"missing bullet in: {text!r}")
            if "记下了:" not in result:
                return CheckResult(name, False, f"return message unexpected: {result!r}")
            return CheckResult(name, True, "header + ## fact + bullet all present, return says 记下了")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_append_memo_dedup() -> CheckResult:
    name = "behavior: append_memo dedupes identical content (second call returns 已存在, no duplicate bullet)"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            worktree = tmp_p / "wt"
            worktree.mkdir(parents=True, exist_ok=True)
            runner = _make_runner(tmp_p)
            with mock.patch.object(
                runner, "_ensure_today_worktree", _async_return(worktree.resolve())
            ):
                first = asyncio.run(runner.append_memo("fact", "TSLA 250 USD"))
                second = asyncio.run(runner.append_memo("fact", "TSLA 250 USD"))
            text = (worktree / "MEMORY.md").read_text(encoding="utf-8")
            n_bullets = text.count("- TSLA 250 USD")
            if n_bullets != 1:
                return CheckResult(name, False, f"expected 1 bullet, found {n_bullets}: {text!r}")
            if not first.startswith("记下了:"):
                return CheckResult(name, False, f"first return unexpected: {first!r}")
            if not second.startswith("已存在:"):
                return CheckResult(name, False, f"second return unexpected: {second!r}")
            if "(跳过重复)" not in second:
                return CheckResult(name, False, f"second return missing (跳过重复): {second!r}")
            return CheckResult(name, True, "1 bullet on disk, second call returned 已存在 + (跳过重复)")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- reclassify_unfiled behavioral tests ---------------------------------

def _test_reclassify_unfiled_moves_and_preserves() -> CheckResult:
    name = "behavior: reclassify_unfiled moves fact-line, preserves unknown ## notes, returns count, ## fact before ## unfiled"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            runner = _make_runner(Path(tmp))

            async def fake_classify(text: str) -> str:
                # Stand-in for the real classifier. Returns a real
                # category for text containing TSLA, "unfiled" for the
                # rest, so the test exercises the "moves" branch
                # without hitting the LLM.
                if "TSLA" in text:
                    return "fact"
                if "always" in text or "prefer" in text:
                    return "preference"
                return "unfiled"

            content = (
                "# MEMORY.md -- 2026-06-04\n"
                "\n"
                "## unfiled\n"
                "- TSLA 250 USD\n"
                "- vague thing no signal\n"
                "\n"
                "## notes\n"
                "- personal note\n"
            )
            with mock.patch.object(runner, "classify_memo", fake_classify):
                new_content, count = asyncio.run(runner.reclassify_unfiled(content))

            if count != 1:
                return CheckResult(name, False, f"expected count=1, got {count}")
            if "## fact" not in new_content:
                return CheckResult(name, False, f"missing ## fact in output: {new_content!r}")
            if "- TSLA 250 USD" not in new_content:
                return CheckResult(name, False, f"TSLA line missing from output: {new_content!r}")
            if "- vague thing no signal" not in new_content:
                return CheckResult(name, False, f"vague line disappeared: {new_content!r}")
            if "## notes" not in new_content:
                return CheckResult(name, False, f"## notes section lost: {new_content!r}")
            if "- personal note" not in new_content:
                return CheckResult(name, False, f"personal note line lost: {new_content!r}")
            fact_idx = new_content.index("## fact")
            unfiled_idx = new_content.index("## unfiled")
            if fact_idx >= unfiled_idx:
                return CheckResult(
                    name, False,
                    f"## fact should come before ## unfiled, got fact@{fact_idx} unfiled@{unfiled_idx}",
                )
            return CheckResult(
                name, True,
                "1 line moved to ## fact, ## notes preserved, ## fact before ## unfiled",
            )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- main ----------------------------------------------------------------

def main() -> int:
    tests = [
        _test_append_memo_signature,
        _test_reclassify_unfiled_signature,
        _test_reclassify_unfiled_never_raises,
        _test_append_memo_unknown_category,
        _test_append_memo_empty_content,
        _test_append_memo_writes_file,
        _test_append_memo_dedup,
        _test_reclassify_unfiled_moves_and_preserves,
    ]
    results = [t() for t in tests]
    print_results(results)
    ok = all(r.ok for r in results)
    print("memo_flow smoke ok" if ok else "memo_flow smoke failed")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
