#!/usr/bin/env python3
"""ops_run_smoke.py — handlers.ops._run timeout kills and waits.

Run: .venv/bin/python scripts/ops_run_smoke.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handlers import ops
from scripts.harness_common import CheckResult, print_results


class _FakeProc:
    def __init__(self) -> None:
        self.killed = False
        self.waited = False

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        return -9

    async def communicate(self):
        await asyncio.sleep(10)
        return b"", b""


async def _test_timeout_kills_and_waits() -> CheckResult:
    name = "behavior: _run on timeout kills process and awaits wait()"
    try:
        proc = _FakeProc()

        async def fake_exec(*args, **kwargs):
            return proc

        with mock.patch("asyncio.create_subprocess_exec", fake_exec):
            with mock.patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                result = await ops._run(["sleep", "999"], timeout=0.01)
        ok = proc.killed and proc.waited and result == ""
        return CheckResult(name, ok, f"killed={proc.killed} waited={proc.waited} result={result!r}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def main() -> int:
    results = [asyncio.run(_test_timeout_kills_and_waits())]
    print_results(results)
    ok = all(r.ok for r in results)
    print("ops run smoke ok" if ok else "ops run smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
