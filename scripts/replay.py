#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import CodexRunner, Job, JobMode


FIXTURE_EVENTS = [
    {"type": "thread.started", "thread_id": "fixture-thread"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "FIXTURE_OK"}},
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
]


class DummySettings:
    pass


def run_replay(verbose: bool) -> int:
    runner = CodexRunner(DummySettings())  # type: ignore[arg-type]
    leaked: list[str] = []
    for event in FIXTURE_EVENTS:
        summary = runner._event_summary(json.dumps(event))
        if runner._should_send_event_progress(summary):
            leaked.append(summary)
        if verbose:
            print(f"{event['type']}: {summary}")

    job = Job(id="fixture", mode=JobMode.RUN, prompt="fixture", sandbox="read-only", summary="FIXTURE_OK")
    completed = runner._completed_message(job)

    forbidden = ["thread.started", "turn.started", "Queued job", "Started job", "Worktree:"]
    failures: list[str] = []
    if leaked:
        failures.append(f"noisy progress leaked: {leaked}")
    for text in forbidden:
        if text in completed:
            failures.append(f"completion message leaked {text!r}")
    if "FIXTURE_OK" not in completed:
        failures.append("completion message omitted final answer")

    if failures:
        for failure in failures:
            print(f"[fail] {failure}")
        return 1
    print("[ok] replay filtered noisy Codex JSONL events")
    print("[ok] completion message contains only human-facing result")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay Codex JSONL fixtures and verify Telegram output filtering.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run_replay(args.verbose))


if __name__ == "__main__":
    main()
