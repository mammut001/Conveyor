#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from redaction import safe_json, truncate
from scripts.harness_common import latest_attempt_file, latest_final_file
from scripts.job_metadata import job_sort_time


INTERESTING_TYPES = {
    "turn.started",
    "turn.completed",
    "turn.failed",
    "response.completed",
    "response.failed",
    "error",
    "item.completed",
    "item.started",
    "agent_message",
}


def _event_preview(event: dict) -> str:
    event_type = str(event.get("type") or event.get("event") or "event")
    for key in ("message", "summary", "text", "delta"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return f"{event_type}: {value.strip()}"
    item = event.get("item")
    if isinstance(item, dict):
        item_type = item.get("type") or item.get("kind")
        text = item.get("text") or item.get("summary") or item.get("message")
        if text:
            return f"{event_type}/{item_type or 'item'}: {text}"
        return f"{event_type}/{item_type or 'item'}: {safe_json(item, 500)}"
    data = event.get("data")
    if isinstance(data, dict):
        return f"{event_type}: {safe_json(data, 500)}"
    return f"{event_type}: {safe_json(event, 500)}"


def _resolve_job_dir(logs_root: Path, selector: str | None) -> Path | None:
    if selector in (None, "", "latest"):
        candidates = [path for path in logs_root.iterdir() if path.is_dir()]
        return max(candidates, key=job_sort_time) if candidates else None

    safe_selector = "".join(ch for ch in selector if ch.isalnum() or ch in "-_")
    if safe_selector != selector:
        raise ValueError("job id selector contains unsupported characters")

    exact = logs_root / selector
    if exact.is_dir():
        return exact
    matches = sorted([path for path in logs_root.iterdir() if path.is_dir() and path.name.startswith(selector)])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"job id prefix is ambiguous: {selector}")
    return None


def _latest_attempt_or_legacy(job_dir: Path) -> Path | None:
    return latest_attempt_file(job_dir) or (job_dir / "codex.jsonl" if (job_dir / "codex.jsonl").exists() else None)


def summarize_log(env_file: str, selector: str | None = None, limit: int = 12) -> str:
    settings = load_settings(env_file)
    logs_root = settings.codex_task_root / "logs"
    if not logs_root.exists():
        return "No job logs found."

    job_dir = _resolve_job_dir(logs_root, selector)
    if not job_dir:
        return f"No job log matched {selector or 'latest'}."

    attempt = _latest_attempt_or_legacy(job_dir)
    final = latest_final_file(job_dir)
    if not attempt:
        return f"{job_dir.name}: no attempt log found."

    events: list[str] = []
    total_events = 0
    interesting_seen = 0
    for raw in attempt.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        total_events += 1
        event_type = str(event.get("type") or event.get("event") or "event")
        preview = _event_preview(event)
        if event_type in INTERESTING_TYPES or "429" in preview or "rate limit" in preview.lower() or "too many requests" in preview.lower():
            interesting_seen += 1
            events.append(truncate(preview.replace("\n", " "), 500))

    if len(events) > limit:
        events = events[-limit:]

    updated = job_sort_time(job_dir).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"Job: {job_dir.name}",
        f"Attempt: {attempt.name}",
        f"Updated: {updated}",
        f"Events: total={total_events} interesting={interesting_seen}",
    ]
    if final and final.exists():
        final_text = final.read_text(encoding="utf-8", errors="replace").strip().replace("\n", " ")
        lines.append(f"Final: {truncate(final_text or '(empty)', 700)}")
    else:
        lines.append("Final: missing")
    lines.append("Recent interesting events:")
    lines.extend(f"- {event}" for event in events[-limit:]) if events else lines.append("- none")
    return truncate("\n".join(lines), 3900)


def main() -> None:
    parser = argparse.ArgumentParser(description="Safely summarize a Codex JSONL job log without dumping raw events.")
    parser.add_argument("job", nargs="?", default="latest", help="Job id, unique prefix, or latest")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()
    print(summarize_log(args.env, args.job, max(1, min(args.limit, 50))))


if __name__ == "__main__":
    main()
