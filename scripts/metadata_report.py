#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from redaction import truncate
from scripts.job_metadata import load_job_metadata
from scripts.log_summary import _resolve_job_dir


def metadata_report(env_file: str, selector: str | None = "latest") -> str:
    settings = load_settings(env_file)
    logs_root = settings.codex_task_root / "logs"
    if not logs_root.exists():
        return "No job logs found."
    job_dir = _resolve_job_dir(logs_root, selector)
    if not job_dir:
        return f"No job log matched {selector or 'latest'}."
    metadata_path = job_dir / "job.json"
    if not metadata_path.exists():
        return f"{job_dir.name}: no job.json metadata yet."

    data = load_job_metadata(job_dir)
    if not data:
        return f"{job_dir.name}: job.json metadata is unreadable."
    lines = [
        f"Job: {data.get('id', job_dir.name)}",
        f"State: {data.get('state', 'unknown')} mode=/{data.get('mode', 'unknown')} sandbox={data.get('sandbox', 'unknown')}",
        f"Attempt: {data.get('attempt', 0)}/{data.get('max_attempts', 0)} return_code={data.get('return_code')}",
        f"Rate limited: {str(data.get('rate_limited')).lower()} cancel_requested={str(data.get('cancel_requested')).lower()}",
        f"Started: {data.get('started_at')}",
        f"Finished: {data.get('finished_at') or '(running)'}",
        f"Duration: {data.get('duration_seconds', '(unknown)')}s",
        f"Worktree: {data.get('worktree_path') or '(none)'}",
        f"Log: {data.get('log_path') or '(none)'}",
    ]
    if data.get("last_event"):
        lines.append(f"Last event: {data['last_event']}")
    usage = data.get("usage")
    if isinstance(usage, dict) and usage:
        lines.append(
            "Usage: "
            + " ".join(f"{key}={value}" for key, value in usage.items() if isinstance(value, int))
        )
    if data.get("error"):
        lines.append(f"Error: {data['error']}")
    if data.get("summary"):
        lines.append(f"Summary: {data['summary']}")
    return truncate("\n".join(lines), 3900)


def main() -> None:
    parser = argparse.ArgumentParser(description="Show structured job.json metadata for the latest or selected job.")
    parser.add_argument("job", nargs="?", default="latest", help="Job id, unique prefix, or latest")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    args = parser.parse_args()
    print(metadata_report(args.env, args.job))


if __name__ == "__main__":
    main()
