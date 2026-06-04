#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings, load_settings
from runner import CodexRunner, Job, JobMode, JobState
from scripts.harness_common import CheckResult, print_results
from scripts.job_metadata import load_job_metadata


@dataclass(frozen=True)
class AttemptFault:
    return_code: int
    final_text: str = ""
    stderr: str = ""
    events: tuple[dict[str, Any], ...] = ()


class FaultRunner(CodexRunner):
    def __init__(self, settings: Settings, faults: list[AttemptFault]) -> None:
        super().__init__(settings)
        self.faults = faults

    async def validate(self) -> None:
        self.settings.codex_task_root.mkdir(parents=True, exist_ok=True)
        (self.settings.codex_task_root / "logs").mkdir(parents=True, exist_ok=True)
        (self.settings.codex_task_root / "worktrees").mkdir(parents=True, exist_ok=True)

    async def _create_worktree(self, job: Job) -> Path:
        worktree = self.settings.codex_task_root / "worktrees" / job.id
        worktree.mkdir(parents=True, exist_ok=True)
        return worktree.resolve()

    async def _run_codex_attempt(self, job: Job, on_progress) -> None:  # type: ignore[no-untyped-def]
        fault = self.faults[min(job.attempt - 1, len(self.faults) - 1)]
        assert job.log_path is not None
        assert job.final_message_path is not None

        with job.log_path.open("ab") as log_file:
            for event in fault.events:
                raw = json.dumps(event, ensure_ascii=False) + "\n"
                log_file.write(raw.encode("utf-8"))
                event_text = self._event_summary(raw)
                if event_text:
                    job.last_event = event_text
                self._capture_usage(job, raw)
                if event_text and self._should_send_event_progress(event_text):
                    await on_progress(event_text)

        if fault.final_text:
            job.final_message_path.write_text(fault.final_text, encoding="utf-8")
        job.return_code = fault.return_code
        if fault.stderr:
            job.error = fault.stderr


def _turn_completed(tokens: int = 11) -> dict[str, Any]:
    return {
        "type": "turn.completed",
        "usage": {
            "input_tokens": tokens,
            "cached_input_tokens": max(0, tokens - 1),
            "output_tokens": 3,
            "reasoning_output_tokens": 0,
        },
    }


def _turn_failed(message: str) -> dict[str, Any]:
    return {"type": "turn.failed", "error": {"message": message}}


async def _run_fault_case(
    settings: Settings,
    name: str,
    faults: list[AttemptFault],
    expected_state: JobState,
    expected_attempts: int,
    expected_rate_limited: bool,
    expected_summary: str = "",
    cancel_on_retry_notice: bool = False,
    retry_delays: tuple[int, ...] | None = None,
) -> CheckResult:
    if retry_delays is not None:
        settings = replace(settings, codex_retry_429_delays_seconds=retry_delays)
    runner = FaultRunner(settings, faults)
    progress: list[str] = []

    async def on_progress(message: str) -> None:
        progress.append(message)
        if cancel_on_retry_notice and "限流" in message:
            await runner.cancel()

    job = await runner.start(JobMode.RUN, f"fault case {name}", on_progress)
    while runner.current_job and runner.current_job.id == job.id:
        await asyncio.sleep(0.05)

    final_job = runner.last_job or job
    metadata = load_job_metadata(settings.codex_task_root / "logs" / final_job.id)
    if not metadata:
        return CheckResult(name, False, "missing job.json")

    checks = {
        "state": final_job.state == expected_state and metadata.get("state") == expected_state.value,
        "attempt": final_job.attempt == expected_attempts and metadata.get("attempt") == expected_attempts,
        "rate_limited": final_job.rate_limited == expected_rate_limited and metadata.get("rate_limited") == expected_rate_limited,
        "finished": isinstance(metadata.get("finished_at"), str) and bool(metadata.get("finished_at")),
        "current_cleared": runner.current_job is None,
    }
    if expected_summary:
        checks["summary"] = final_job.summary.strip() == expected_summary and metadata.get("summary") == expected_summary
    ok = all(checks.values())
    detail = ", ".join(f"{key}={value}" for key, value in checks.items())
    if progress:
        detail = f"{detail}; progress={progress[-2:]}"
    return CheckResult(name, ok, detail)


async def run_fault_harness(env_file: str) -> int:
    temp_parent = Path(tempfile.mkdtemp(prefix="codex-fault-harness-"))
    try:
        fallback_settings = Settings(
            telegram_bot_token="0:offline",
            telegram_allowed_user_id=0,
            codex_workspace_root=(temp_parent / "repo").resolve(),
            codex_bin="codex",
            codex_task_root=(temp_parent / "tasks").resolve(),
            codex_model=None,
            codex_timeout_seconds=5,
            telegram_progress_seconds=1,
            codex_retry_429_delays_seconds=(0, 1),
            codex_memory_root=(temp_parent / "memory").resolve(),
            user_timezone="America/Toronto",
        )
        try:
            base_settings = load_settings(env_file)
        except RuntimeError:
            base_settings = fallback_settings
        settings = replace(
            base_settings,
            codex_workspace_root=(temp_parent / "repo").resolve(),
            codex_task_root=(temp_parent / "tasks").resolve(),
            codex_retry_429_delays_seconds=(0, 1),
            codex_timeout_seconds=5,
        )
        settings.codex_workspace_root.mkdir(parents=True, exist_ok=True)

        results = [
            await _run_fault_case(
                settings,
                "success",
                [AttemptFault(0, "FAULT_OK", events=(_turn_completed(),))],
                JobState.COMPLETED,
                1,
                False,
                expected_summary="FAULT_OK",
            ),
            await _run_fault_case(
                settings,
                "non429 failure",
                [AttemptFault(1, stderr="plain failure", events=(_turn_failed("plain failure"),))],
                JobState.FAILED,
                1,
                False,
            ),
            await _run_fault_case(
                settings,
                "429 retry success",
                [
                    AttemptFault(1, stderr="429 Too Many Requests", events=(_turn_failed("429 Too Many Requests"),)),
                    AttemptFault(0, "FAULT_RETRY_OK", events=(_turn_completed(17),)),
                ],
                JobState.COMPLETED,
                2,
                False,
                expected_summary="FAULT_RETRY_OK",
            ),
            await _run_fault_case(
                settings,
                "429 exhausted",
                [
                    AttemptFault(1, stderr="429 Too Many Requests", events=(_turn_failed("429 Too Many Requests"),)),
                    AttemptFault(1, stderr="high demand", events=(_turn_failed("high demand"),)),
                    AttemptFault(1, stderr="rate limit", events=(_turn_failed("rate limit"),)),
                ],
                JobState.FAILED,
                3,
                True,
            ),
            await _run_fault_case(
                settings,
                "cancel during retry wait",
                [AttemptFault(1, stderr="429 Too Many Requests", events=(_turn_failed("429 Too Many Requests"),))],
                JobState.CANCELLED,
                1,
                True,
                cancel_on_retry_notice=True,
                retry_delays=(1,),
            ),
        ]
    finally:
        shutil.rmtree(temp_parent, ignore_errors=True)

    ok = print_results(results)
    if ok:
        print("fault harness ok")
    else:
        print("fault harness failed")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline fault-injection harness for CodexRunner state transitions.")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run_fault_harness(args.env)))


if __name__ == "__main__":
    main()
