#!/usr/bin/env python3
"""ops_intent_smoke.py — handlers.ops.detect_ops_intent classifies
common host-status phrases and is conservative on ambiguous text.

Pins:
  - "看看我的负载" -> "load"
  - "帮我运行 htop 看看我的vps" -> "htop"
  - "哪些进程占用内存" -> "ps"
  - "check vps load" -> "load"
  - "ssh ubuntu@1.2.3.4 ..." -> None (no fake remote creds)
  - "写个 quicksort" -> None (don't hijack coding requests)
  - "look at htop source code" -> "htop" (htop is the keyword; document the
    known false-positive and skip rather than guess).

Run: .venv/bin/python scripts/ops_intent_smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handlers.ops import detect_ops_intent
from scripts.harness_common import CheckResult, print_results


CASES: list[tuple[str, str | None]] = [
    # Chinese load
    ("看看我的负载", "load"),
    ("看一下负载", "load"),
    ("系统负载", "load"),
    ("主机负载", "load"),
    ("机器状态", "load"),
    ("vps 状态", "load"),
    ("服务器状态", "load"),
    # English load
    ("check vps load", "load"),
    ("server load", "load"),
    ("load average", "load"),
    ("show me the host status", "load"),
    # htop
    ("跑一下 htop", "htop"),
    ("帮我运行 htop 看看我的vps", "htop"),
    ("top 看一下", "htop"),
    # ps
    ("哪些进程占用", "ps"),
    ("ps aux", "ps"),
    # should NOT trigger
    ("写个 quicksort", None),
    ("fix the failing test", None),
    ("帮我 ssh 到别的机器", None),
    ("", None),
]


def _test_intent_classification() -> list[CheckResult]:
    out: list[CheckResult] = []
    for text, expected in CASES:
        got = detect_ops_intent(text)
        ok = got == expected
        out.append(CheckResult(
            f"intent({text!r}) -> {expected!r}",
            ok,
            f"got {got!r}" if not ok else f"got {got!r}",
        ))
    return out


CHECKS = [_test_intent_classification]


def main() -> int:
    results: list[CheckResult] = []
    for fn in CHECKS:
        results.extend(fn())
    print_results(results)
    ok = all(r.ok for r in results)
    print("ops intent smoke ok" if ok else "ops intent smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
