#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from scripts.harness_common import latest_attempt_file, latest_final_file
from scripts.job_metadata import job_sort_time, load_job_metadata, metadata_text


RATE_LIMIT_RE = re.compile(r"\b429\b|too many requests|rate limit|high demand", re.IGNORECASE)


def _latest_attempt_or_legacy(job_dir: Path) -> Path | None:
    return latest_attempt_file(job_dir) or (job_dir / "codex.jsonl" if (job_dir / "codex.jsonl").exists() else None)


def _state_and_usage(attempt: Path | None, has_final: bool) -> tuple[str, dict[str, int], bool]:
    if not attempt or not attempt.exists():
        return ("completed" if has_final else "unknown"), {}, False
    state = "running"
    usage: dict[str, int] = {}
    rate_limited = False
    for raw in attempt.read_text(encoding="utf-8", errors="replace").splitlines():
        if RATE_LIMIT_RE.search(raw):
            rate_limited = True
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "turn.completed":
            state = "completed"
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                usage = {key: int(value) for key, value in raw_usage.items() if isinstance(value, int)}
        elif event_type == "turn.failed":
            state = "failed"
    if state == "running" and has_final:
        state = "completed"
    return state, usage, rate_limited


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def _fmt_age(moment: datetime) -> str:
    seconds = int((datetime.now(timezone.utc) - moment).total_seconds())
    if seconds < 120:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 120:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 72:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def metrics_report(env_file: str, limit: int = 20) -> str:
    settings = load_settings(env_file)
    logs_root = settings.codex_task_root / "logs"
    if not logs_root.exists():
        return "No job logs found."

    job_dirs = sorted([path for path in logs_root.iterdir() if path.is_dir()], key=job_sort_time, reverse=True)[:limit]
    if not job_dirs:
        return "No job logs found."

    states = {"completed": 0, "failed": 0, "running": 0, "unknown": 0}
    rate_limited = 0
    total_usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    durations: list[int] = []
    recent_lines: list[str] = []

    for job_dir in job_dirs:
        attempt = _latest_attempt_or_legacy(job_dir)
        final = latest_final_file(job_dir)
        metadata = load_job_metadata(job_dir)
        state, usage, hit_rate_limit = _state_and_usage(attempt, bool(final and final.exists()))
        if metadata:
            state = metadata_text(metadata, "state", state)
            hit_rate_limit = bool(metadata.get("rate_limited", hit_rate_limit))
            metadata_usage = metadata.get("usage")
            if isinstance(metadata_usage, dict) and metadata_usage:
                usage = {key: int(value) for key, value in metadata_usage.items() if isinstance(value, int)}
            duration = metadata.get("duration_seconds")
            if isinstance(duration, int) and duration >= 0:
                durations.append(duration)
        states[state] = states.get(state, 0) + 1
        rate_limited += 1 if hit_rate_limit else 0
        for key in total_usage:
            total_usage[key] += usage.get(key, 0)
        final_preview = metadata_text(metadata, "summary")[:60] if metadata else ""
        if not final_preview and final and final.exists():
            final_preview = final.read_text(encoding="utf-8", errors="replace").strip().replace("\n", " ")[:60]
        duration_suffix = ""
        if metadata and isinstance(metadata.get("duration_seconds"), int):
            duration_suffix = f" · {metadata['duration_seconds']}s"
        recent_lines.append(f"{job_dir.name} · {state} · {_fmt_age(job_sort_time(job_dir))}{duration_suffix}" + (f" · {final_preview}" if final_preview else ""))

    completed = states.get("completed", 0)
    success_rate = round((completed / len(job_dirs)) * 100)
    average_duration = round(sum(durations) / len(durations)) if durations else 0
    lines = [
        f"Metrics for latest {len(job_dirs)} jobs:",
        f"states: completed={states.get('completed', 0)} failed={states.get('failed', 0)} running={states.get('running', 0)} unknown={states.get('unknown', 0)} success={success_rate}%",
        f"rate-limit hits: {rate_limited}",
        f"duration: avg={average_duration}s samples={len(durations)}",
        "usage totals:",
        f"  input={_fmt_int(total_usage['input_tokens'])} cached={_fmt_int(total_usage['cached_input_tokens'])}",
        f"  output={_fmt_int(total_usage['output_tokens'])} reasoning={_fmt_int(total_usage['reasoning_output_tokens'])}",
        "recent:",
        *recent_lines[:8],
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize recent Codex runner job health and token usage.")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    print(metrics_report(args.env, max(1, min(args.limit, 200))))


if __name__ == "__main__":
    main()
