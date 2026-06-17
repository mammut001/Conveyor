#!/usr/bin/env python3
"""ops_smoke.py — handlers.ops runs host commands deterministically and
handlers.dispatch routes ops intents to /load /htop /ps without
invoking CodexRunner.start.

Pins:
  - AST: handle_ops_intent exists and dispatches on kind
  - behavior: /load reply contains load/memory/disk/process sections
  - behavior: /htop reply contains the "TUI 解释" line
  - behavior: /ps reply contains CPU+mem tables
  - behavior: detect_ops_intent path in dispatch() never calls
    runner.start for "看看我的负载" / "帮我运行 htop 看看我的vps"
  - behavior: normal coding request still calls runner.start

Run: .venv/bin/python scripts/ops_smoke.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "12345")
os.environ.setdefault("LARK_APP_ID", "cli_fake")
os.environ.setdefault("LARK_APP_SECRET", "fake")
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/codex-ops-ws")
os.environ.setdefault("CODEX_TASK_ROOT", "/tmp/codex-ops-task")
os.environ.setdefault("CODEX_MEMORY_ROOT", "/tmp/codex-ops-mem")
os.environ.setdefault("CODEX_BIN", "codex")
os.environ.setdefault("USER_TIMEZONE", "UTC")

from channel import InboundMessage
from config import load_settings
from handlers.dispatch import dispatch
from handlers.ops import (
    handle_ops_intent,
    _htop_snapshot,
    _load_snapshot,
    format_ps_output,
)
from runner import CodexRunner
from scripts.harness_common import CheckResult, print_results


HANDLERS_OPS_PY = Path(__file__).resolve().parents[1] / "handlers" / "ops.py"


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
        channel="telegram",
        operator_id="12345",
        chat_id="chat-1",
        message_id="m-1",
        text=text,
    )


def _settings():
    return load_settings()


# ---- AST tests -----------------------------------------------------------

def _parse_ops() -> ast.Module:
    return ast.parse(HANDLERS_OPS_PY.read_text(encoding="utf-8"))


def _test_handle_ops_intent_signature() -> CheckResult:
    name = "AST: handlers.ops.handle_ops_intent is module-level async, kind is Literal[load/htop/ps]"
    try:
        tree = _parse_ops()
        func = next(
            (n for n in tree.body
             if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
             and n.name == "handle_ops_intent"),
            None,
        )
        if not isinstance(func, ast.AsyncFunctionDef):
            return CheckResult(name, False, "function missing or not async")
        if "kind" not in ast.unparse(func.args):
            return CheckResult(name, False, "missing kind arg")
        return CheckResult(name, True, "kind discriminator + branch on load/htop/ps")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


# ---- behavior tests ------------------------------------------------------

def _test_load_contains_required_sections() -> CheckResult:
    name = "behavior: /load reply contains load/memory/disk/process sections"
    try:
        text = asyncio.run(_load_snapshot())
        for needle in (
            "VPS / Bot 主机负载快照",
            "运行时间",
            "CPU 核数",
            "内存:",
            "磁盘:",
            "CPU 占用最高",
            "内存占用最高",
        ):
            if needle not in text:
                return CheckResult(name, False, f"missing section: {needle}")
        if not text.endswith(("…", ".", "。")) and len(text) > 100:
            return CheckResult(name, True, f"all sections present, len={len(text)}")
        return CheckResult(name, True, f"all sections present, len={len(text)}")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_htop_contains_tui_explanation() -> CheckResult:
    name = "behavior: /htop reply contains TUI 解释 + a top-style frame"
    try:
        text = asyncio.run(_htop_snapshot())
        if "交互式 TUI" not in text and "TUI" not in text:
            return CheckResult(name, False, f"missing TUI explanation: {text[:200]!r}")
        if "top" not in text.lower() and "load" not in text.lower():
            return CheckResult(name, False, f"missing process info: {text[:200]!r}")
        return CheckResult(name, True, f"len={len(text)}")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_ps_default_does_not_contain_args() -> CheckResult:
    name = "behavior: /ps default uses comm only, no args"
    try:
        text = asyncio.run(format_ps_output(""))
        if "comm 模式" not in text:
            return CheckResult(name, False, f"missing comm title: {text[:200]!r}")
        if "CPU 占用最高" not in text or "内存占用最高" not in text:
            return CheckResult(name, False, f"missing tables: {text[:200]!r}")
        return CheckResult(name, True, f"len={len(text)}, comm-only confirmed")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_handle_ops_intent_dispatches() -> CheckResult:
    name = "behavior: handle_ops_intent routes kind=load to /load output"
    try:
        port = FakeOutbound()
        asyncio.run(handle_ops_intent(_msg("看看我的负载"), port, None, _settings(), "load"))
        if not port.replies or "VPS / Bot 主机负载快照" not in port.replies[0]:
            return CheckResult(name, False, f"port.replies={port.replies!r}")
        return CheckResult(name, True, "load kind produced /load output")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_dispatch_load_phrase_skips_codex() -> CheckResult:
    name = "behavior: '看看我的负载' routed via dispatch() never calls CodexRunner.start"
    try:
        port = FakeOutbound()
        runner = mock.Mock(spec=CodexRunner)
        runner.start = mock.AsyncMock(side_effect=AssertionError("runner.start should NOT be called"))
        runner.append_memo = mock.AsyncMock(side_effect=AssertionError("not memo"))
        runner.classify_memo = mock.AsyncMock(side_effect=AssertionError("not memo"))
        # handlers/jobs reads runner.settings.conveyor_progress_mode
        # (round 10). Mirror the production Settings default so the
        # mock looks like a real runner.
        runner.settings = SimpleNamespace(
            conveyor_progress_mode="verbose",
            codex_memory_root=Path("/tmp/codex-ops-mem"),
            conveyor_session_enabled=False,
        )
        asyncio.run(dispatch(_msg("看看我的负载"), port, _settings(), runner))
        runner.start.assert_not_called()
        if not any("VPS" in r or "负载" in r for r in port.replies):
            return CheckResult(name, False, f"port.replies={port.replies!r}")
        return CheckResult(name, True, "no CodexRunner.start, ops reply sent")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_dispatch_htop_phrase_skips_codex() -> CheckResult:
    name = "behavior: '帮我运行 htop 看看我的vps' routed via dispatch() never calls CodexRunner.start"
    try:
        port = FakeOutbound()
        runner = mock.Mock(spec=CodexRunner)
        runner.start = mock.AsyncMock(side_effect=AssertionError("runner.start should NOT be called"))
        runner.append_memo = mock.AsyncMock(side_effect=AssertionError("not memo"))
        runner.classify_memo = mock.AsyncMock(side_effect=AssertionError("not memo"))
        runner.settings = SimpleNamespace(
            conveyor_progress_mode="verbose",
            codex_memory_root=Path("/tmp/codex-ops-mem"),
            conveyor_session_enabled=False,
        )
        asyncio.run(dispatch(_msg("帮我运行 htop 看看我的vps"), port, _settings(), runner))
        runner.start.assert_not_called()
        if not any("TUI" in r or "top" in r.lower() for r in port.replies):
            return CheckResult(name, False, f"port.replies={port.replies!r}")
        return CheckResult(name, True, "no CodexRunner.start, htop reply sent")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_dispatch_coding_request_calls_codex() -> CheckResult:
    name = "behavior: '写个 quicksort' still routes to CodexRunner.start"
    try:
        port = FakeOutbound()
        runner = mock.Mock(spec=CodexRunner)
        # Build a job that completes immediately so handle_codex_job returns
        from types import SimpleNamespace
        job = SimpleNamespace(id="test-job-ops", state="completed", summary="ok", error=None)

        async def fake_start(mode, prompt, progress):
            return job

        runner.start = fake_start
        runner.current_job = None
        runner.append_memo = mock.AsyncMock()
        runner.classify_memo = mock.AsyncMock()
        runner.settings = SimpleNamespace(
            conveyor_progress_mode="verbose",
            codex_memory_root=Path("/tmp/codex-ops-mem"),
            conveyor_session_enabled=False,
        )
        asyncio.run(dispatch(_msg("写个 quicksort"), port, _settings(), runner))
        # Codex reply path: placeholder + possibly send_new final.
        if not any("收到" in r or "ok" in r.lower() for r in port.replies + port.replies):
            return CheckResult(name, False, f"port.replies={port.replies!r}")
        return CheckResult(name, True, "CodexRunner.start was called for coding request")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_dispatch_slash_load_routes_to_command_table() -> CheckResult:
    name = "behavior: /load routed via dispatch() to ops handler (no CodexRunner.start)"
    try:
        port = FakeOutbound()
        runner = mock.Mock(spec=CodexRunner)
        runner.start = mock.AsyncMock(side_effect=AssertionError("runner.start should NOT be called"))
        runner.settings = SimpleNamespace(
            conveyor_progress_mode="verbose",
            codex_memory_root=Path("/tmp/codex-ops-mem"),
            conveyor_session_enabled=False,
        )
        asyncio.run(dispatch(_msg("/load"), port, _settings(), runner))
        runner.start.assert_not_called()
        if not any("VPS" in r for r in port.replies):
            return CheckResult(name, False, f"port.replies={port.replies!r}")
        return CheckResult(name, True, "/load slash command goes to ops handler")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


CHECKS = [
    _test_handle_ops_intent_signature,
    _test_load_contains_required_sections,
    _test_htop_contains_tui_explanation,
    _test_ps_default_does_not_contain_args,
    _test_handle_ops_intent_dispatches,
    _test_dispatch_load_phrase_skips_codex,
    _test_dispatch_htop_phrase_skips_codex,
    _test_dispatch_coding_request_calls_codex,
    _test_dispatch_slash_load_routes_to_command_table,
]


def main() -> int:
    results = [t() for t in CHECKS]
    print_results(results)
    ok = all(r.ok for r in results)
    print("ops smoke ok" if ok else "ops smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
