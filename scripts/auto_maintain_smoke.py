#!/usr/bin/env python3
"""Static regression guard for scripts/auto_maintain.run_maintenance.

The hourly codex-telegram-maintain.timer crashed four times in a row
(09:11 / 10:16 / 11:18 / 12:21 / 13:25 UTC on 2026-06-04) because
run_maintenance did `actions.append(compress_if_needed(settings))`
without awaiting it. The unawaited coroutine then blew up
`"\n".join(actions)` with `TypeError: sequence item 1: expected str
instance, coroutine found`.

These tests don't exercise the script end-to-end; they just walk the
AST of run_maintenance and assert that every call to a known async
helper is wrapped in `await`. Cheap to run, no env required.

Run with:  .venv/bin/python scripts/auto_maintain_smoke.py
Exit code: 0 if all pass, 1 otherwise.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.auto_maintain import run_maintenance
from scripts.harness_common import CheckResult, print_results


AUTO_MAINTAIN_PY = Path(__file__).resolve().parents[1] / "scripts" / "auto_maintain.py"


# Async callees that run_maintenance should always await. Kept explicit
# (not auto-detected from inspect) so the test stays robust against
# renames / moves of the callees.
ASYNC_NAME_CALLEES = {"compress_if_needed"}
ASYNC_ATTR_CALLEES = {("runner", "clean_old_jobs"), ("runner", "clean_old_worktrees")}


def _function_def(tree, name):
    return next(
        (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name),
        None,
    )


def _calls_with_await_status(func_def):
    """Yield (Call node, 'awaited'|'plain') for every Call in func_def."""
    awaited_call_ids = {
        id(node.value)
        for node in ast.walk(func_def)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
    }
    for node in ast.walk(func_def):
        if isinstance(node, ast.Call):
            yield node, "awaited" if id(node) in awaited_call_ids else "plain"


def _callee_id(call):
    """Return (kind, name) for the callee, or None if not name-like.

    kind is 'name' for `foo(...)` and 'attr' for `obj.foo(...)`.
    """
    if isinstance(call.func, ast.Name):
        return ("name", call.func.id)
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
        return ("attr", f"{call.func.value.id}.{call.func.attr}")
    return None


async def main() -> int:
    results = []

    # 1. Direct regression: compress_if_needed must be awaited inside run_maintenance.
    try:
        tree = ast.parse(AUTO_MAINTAIN_PY.read_text())
        func_def = _function_def(tree, "run_maintenance")
        if func_def is None:
            raise AssertionError("run_maintenance not found")
        un_awaited = []
        for call, status in _calls_with_await_status(func_def):
            target = _callee_id(call)
            if target is None or target[0] != "name":
                continue
            name = target[1]
            if name in ASYNC_NAME_CALLEES and status == "plain":
                un_awaited.append(name)
        ok = not un_awaited
        detail = "all awaited" if ok else f"un-awaited: {sorted(un_awaited)}"
        results.append(CheckResult("compress_if_needed is awaited", ok, detail))
    except Exception as exc:
        results.append(
            CheckResult("compress_if_needed is awaited", False, f"raised {type(exc).__name__}: {exc}")
        )

    # 2. Same pattern for runner.clean_old_jobs / clean_old_worktrees.
    try:
        tree = ast.parse(AUTO_MAINTAIN_PY.read_text())
        func_def = _function_def(tree, "run_maintenance")
        if func_def is None:
            raise AssertionError("run_maintenance not found")
        un_awaited = []
        for call, status in _calls_with_await_status(func_def):
            target = _callee_id(call)
            if target is None or target[0] != "attr" or status != "plain":
                continue
            obj, attr = target[1].split(".", 1)
            if (obj, attr) in ASYNC_ATTR_CALLEES:
                un_awaited.append(f"{obj}.{attr}")
        ok = not un_awaited
        detail = "all awaited" if ok else f"un-awaited: {sorted(un_awaited)}"
        results.append(CheckResult("runner.clean_old_* awaited", ok, detail))
    except Exception as exc:
        results.append(
            CheckResult("runner.clean_old_* awaited", False, f"raised {type(exc).__name__}: {exc}")
        )

    # 3. Sanity: run_maintenance is still a coroutine function. If someone
    #    accidentally de-asyncs it, every `await` inside turns into a
    #    SyntaxError and the hourly timer is silently back to broken.
    try:
        is_coro = inspect.iscoroutinefunction(run_maintenance)
        results.append(CheckResult("run_maintenance is async", is_coro, str(is_coro)))
    except Exception as exc:
        results.append(
            CheckResult("run_maintenance is async", False, f"raised {type(exc).__name__}: {exc}")
        )

    ok = print_results(results)
    print("auto_maintain smoke ok" if ok else "auto_maintain smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
