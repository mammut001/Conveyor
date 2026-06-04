#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from runner import CodexRunner, JobMode, JobState
from scripts.telegram_api import send_message


async def run_job(mode: JobMode, prompt: str, env_file: str, notify: bool) -> int:
    settings = load_settings(env_file)
    runner = CodexRunner(settings)

    async def progress(message: str) -> None:
        print(message)
        if notify:
            await asyncio.to_thread(send_message, settings, message)

    job = await runner.start(mode, prompt, progress)

    while runner.current_job and runner.current_job.id == job.id:
        await asyncio.sleep(1)

    final_job = runner.last_job or job
    return 0 if final_job.state == JobState.COMPLETED else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Submit a Codex runner job without Telegram inbound updates.")
    parser.add_argument("prompt", nargs="+", help="Prompt to pass to Codex")
    parser.add_argument("--mode", choices=[mode.value for mode in JobMode], default=JobMode.RUN.value)
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--no-notify", action="store_true", help="Do not send Telegram progress messages")
    args = parser.parse_args()

    raise SystemExit(asyncio.run(run_job(JobMode(args.mode), " ".join(args.prompt), args.env, not args.no_notify)))


if __name__ == "__main__":
    main()
