#!/usr/bin/env python3
"""tools_intent_smoke.py — route_intent classifies deterministic/hybrid/llm paths.

Run: .venv/bin/python scripts/tools_intent_smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import handlers.tools.executors  # noqa: F401 — register tools
from handlers.intent import route_intent
from scripts.harness_common import CheckResult, print_results


CASES: list[tuple[str, str, tuple[str, ...] | None]] = [
    ("看看我的负载", "deterministic", ("load",)),
    ("看看磁盘", "deterministic", ("disk",)),
    ("看看日志", "deterministic", ("logs",)),
    ("服务还在跑吗", "deterministic", ("service_status",)),
    ("git status", "deterministic", ("git_status",)),
    ("为什么服务器这么慢", "hybrid", ("load", "ps", "disk", "service_status")),
    ("帮我分析一下 vps 为什么这么卡", "hybrid", None),
    ("诊断服务器", "hybrid", None),
    ("帮我诊断一下 bot", "hybrid", None),
    ("写个 quicksort", "llm", None),
    ("fix the failing test", "llm", None),
    ("look at htop source code", "llm", None),
    ("帮我改 htop 相关代码", "llm", None),
    ("write docs about htop", "llm", None),
    ("tool load", "deterministic", ("load",)),
    ("重启 telegram bot", "deterministic", ("service_restart",)),
]

_DIAGNOSE_ITEM_EXPECTATIONS: dict[str, frozenset[str]] = {
    "诊断服务器": frozenset({"load", "ps", "disk", "service_status", "logs"}),
    "帮我诊断一下 bot": frozenset({"service_status", "logs", "git_status", "disk"}),
}

# Natural-language restart intent: route_intent should resolve the
# target unit (or fall through to llm if ambiguous — never default).
# Each row is (text, expected_arg_or_none). None means: must NOT be a
# service_restart route with empty arg.
_RESTART_CASES: list[tuple[str, str | None]] = [
    ("重启 telegram bot", "conveyor-telegram-bot"),
    ("重启 feishu bot", "conveyor-feishu-bot"),
    ("重启 maintain", "conveyor-maintain.timer"),
    ("restart conveyor-feishu-bot", "conveyor-feishu-bot"),
    ("restart feishu", "conveyor-feishu-bot"),
    ("重启飞书 bot", "conveyor-feishu-bot"),
    ("重启电报 bot", "conveyor-telegram-bot"),
    ("重启维护", "conveyor-maintain.timer"),
    # Ambiguous: must NOT default to telegram-bot. Either routed to
    # llm, or to service_restart with a non-empty arg (this second
    # branch never happens by design, but we keep the guard).
    ("重启 bot", None),
    ("重启服务", None),
    ("帮我重启 bot", None),
]


def _test_routes() -> list[CheckResult]:
    out: list[CheckResult] = []
    for text, expected_kind, expected_tools in CASES:
        route = route_intent(text)
        ok = route.kind == expected_kind
        detail = f"kind={route.kind!r} tools={route.tools!r}"
        if ok and expected_tools is not None and route.tools != expected_tools:
            ok = False
            detail += f" expected tools={expected_tools!r}"
        if ok and text in _DIAGNOSE_ITEM_EXPECTATIONS:
            names = frozenset(n for n, _ in route.tool_items)
            exp = _DIAGNOSE_ITEM_EXPECTATIONS[text]
            if names != exp:
                ok = False
                detail += f" tool_items={names!r} expected={exp!r}"
        out.append(CheckResult(f"route({text!r}) -> {expected_kind}", ok, detail))
    return out


def _test_restart_intent() -> list[CheckResult]:
    """Round-10 safety: never default restart to telegram-bot."""
    out: list[CheckResult] = []
    for text, expected_arg in _RESTART_CASES:
        route = route_intent(text)
        is_restart_route = (
            route.kind == "deterministic"
            and route.tools == ("service_restart",)
        )
        if expected_arg is None:
            ok = not (is_restart_route and route.arg == "")
            detail = f"kind={route.kind!r} tools={route.tools!r} arg={route.arg!r}"
            out.append(CheckResult(f"restart ambiguous({text!r}) does NOT default", ok, detail))
        else:
            ok = is_restart_route and route.arg == expected_arg
            detail = f"kind={route.kind!r} tools={route.tools!r} arg={route.arg!r} expected={expected_arg!r}"
            out.append(CheckResult(f"restart({text!r}) -> {expected_arg!r}", ok, detail))
    return out


def main() -> int:
    results = _test_routes() + _test_restart_intent()
    print_results(results)
    ok = all(r.ok for r in results)
    print("tools intent smoke ok" if ok else "tools intent smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
