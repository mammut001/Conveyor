#!/usr/bin/env python3
"""ps_full_smoke.py — /ps full requires confirm before showing args.

Run: .venv/bin/python scripts/ps_full_smoke.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handlers.ops import format_ps_output, parse_ps_arg
from scripts.harness_common import CheckResult, print_results


def _test_parse_ps_arg() -> list[CheckResult]:
    cases = [
        ("", "comm"),
        ("full", "full_warn"),
        ("full confirm", "full_args"),
        ("FULL CONFIRM", "full_args"),
    ]
    out: list[CheckResult] = []
    for arg, expected in cases:
        got = parse_ps_arg(arg)
        out.append(CheckResult(
            f"parse_ps_arg({arg!r}) -> {expected!r}",
            got == expected,
            f"got {got!r}",
        ))
    return out


async def _test_default_comm_only() -> CheckResult:
    name = "behavior: /ps default output is comm mode, no args column"
    try:
        text = await format_ps_output("")
        ok = "comm 模式，不含 args" in text
        ok = ok and "full args 模式" not in text
        return CheckResult(name, ok, text[:120])
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_full_warns() -> CheckResult:
    name = "behavior: /ps full returns warning, not args output"
    try:
        text = await format_ps_output("full")
        ok = "full confirm" in text.lower() or "full confirm" in text
        ok = ok and "敏感" in text and "comm 模式" not in text
        return CheckResult(name, ok, text[:160])
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_full_confirm_title() -> CheckResult:
    name = "behavior: /ps full confirm uses full args title with redact warning"
    try:
        text = await format_ps_output("full confirm")
        ok = (
            "full args 模式" in text
            and "redact" in text.lower()
            and "敏感" in text
        )
        return CheckResult(name, ok, text[:160])
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def main() -> int:
    results = _test_parse_ps_arg()
    for fn in [_test_default_comm_only, _test_full_warns, _test_full_confirm_title]:
        results.append(asyncio.run(fn()))
    print_results(results)
    ok = all(r.ok for r in results)
    print("ps full smoke ok" if ok else "ps full smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
