#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from runner import CodexRunner, JobMode, JobState
from scripts.harness_common import (
    CheckResult,
    attempt_completed,
    check_minimax_models,
    check_systemd_active,
    print_results,
)
from scripts.telegram_api import send_message


async def run_smoke(env_file: str, service_name: str, notify: bool) -> int:
    settings = load_settings(env_file)
    expected = f"SMOKE_OK_{uuid4().hex[:8]}"
    results: list[CheckResult] = [
        check_systemd_active(service_name),
        check_minimax_models(settings),
    ]

    if notify:
        try:
            send_message(settings, f"Smoke harness started: {expected}")
            results.append(CheckResult("telegram", True, "sendMessage ok"))
        except Exception as exc:
            results.append(CheckResult("telegram", False, f"sendMessage failed: {exc}"))
    else:
        results.append(CheckResult("telegram", True, "sendMessage skipped; notify=false"))

    progress_messages: list[str] = []
    runner = CodexRunner(settings)

    async def progress(message: str) -> None:
        progress_messages.append(message)
        if notify:
            await asyncio.to_thread(send_message, settings, message)

    job = await runner.start(JobMode.RUN, f"Reply exactly {expected}", progress)
    while runner.current_job and runner.current_job.id == job.id:
        await asyncio.sleep(1)

    final_job = runner.last_job or job
    final_text = final_job.summary.strip()
    results.append(CheckResult("codex job", final_job.state == JobState.COMPLETED, f"state={final_job.state.value} id={final_job.id}"))
    results.append(CheckResult("final text", final_text == expected, f"expected={expected} got={final_text!r}"))
    if final_job.log_path and final_job.log_path.exists():
        results.append(CheckResult("turn.completed", attempt_completed(final_job.log_path), str(final_job.log_path)))
    else:
        results.append(CheckResult("turn.completed", False, "missing attempt log"))

    ok = print_results(results)
    if ok:
        print(f"smoke ok: {expected}")
    else:
        print("smoke failed")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an end-to-end Telegram Codex runner smoke test.")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--service", default="conveyor-telegram-bot", help="systemd service name")
    parser.add_argument("--notify", action="store_true", help="Send runner progress to Telegram during the smoke job")
    args = parser.parse_args()

    raise SystemExit(asyncio.run(run_smoke(args.env, args.service, args.notify)))


if __name__ == "__main__":
    main()
