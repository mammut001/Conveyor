#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from redaction import truncate
from scripts.harness_common import CheckResult, print_results


def _run_script(name: str, args: list[str], timeout: int) -> CheckResult:
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        process = subprocess.run(
            [sys.executable, str(root / "scripts" / name), *args],
            cwd=root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "").strip() if isinstance(exc.stdout, str) else ""
        detail = f"timed out after {timeout}s" + (f": {output}" if output else "")
        return CheckResult(name.removesuffix(".py"), False, truncate(detail, 900))
    except OSError as exc:
        return CheckResult(name.removesuffix(".py"), False, truncate(str(exc), 900))
    output = (process.stdout or "").strip()
    detail = output.splitlines()[-1] if output else f"exit={process.returncode}"
    if process.returncode != 0 and output:
        detail = output.replace("\n", " | ")
    return CheckResult(name.removesuffix(".py"), process.returncode == 0, truncate(detail, 900))


def run_offline_harnesses(env_file: str, include_command: bool = True, timeout: int = 30) -> list[CheckResult]:
    checks = [
        _run_script("replay.py", [], timeout),
        _run_script("fault_harness.py", ["--env", env_file], timeout),
    ]
    if include_command:
        checks.insert(1, _run_script("command_harness.py", [], timeout))
    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description="Run cheap offline harnesses and summarize their status.")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--skip-command", action="store_true", help="Skip the Telegram command harness")
    args = parser.parse_args()
    results = run_offline_harnesses(args.env, include_command=not args.skip_command, timeout=max(1, args.timeout))
    ok = print_results(results)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
