#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from redaction import truncate
from scripts.job_metadata import job_sort_time, load_job_metadata, metadata_text


RATE_LIMIT_RE = re.compile(r"\b429\b|too many requests|rate limit|high demand", re.IGNORECASE)


@dataclass(frozen=True)
class RateLimitHit:
    job_id: str
    path: Path
    updated_at: datetime
    line_preview: str


def find_rate_limit_hits(logs_root: Path, limit: int) -> list[RateLimitHit]:
    hits: list[RateLimitHit] = []
    if not logs_root.exists():
        return hits
    for metadata_path in logs_root.glob("*/job.json"):
        data = load_job_metadata(metadata_path.parent)
        if not data or not data.get("rate_limited"):
            continue
        hits.append(
            RateLimitHit(
                job_id=metadata_path.parent.name,
                path=metadata_path,
                updated_at=job_sort_time(metadata_path.parent),
                line_preview=metadata_text(data, "last_event", "rate_limited=true"),
            )
        )

    attempts = list(logs_root.glob("*/attempt-*.jsonl")) + list(logs_root.glob("*/codex.jsonl"))
    for attempt in attempts:
        if any(hit.job_id == attempt.parent.name for hit in hits):
            continue
        text = attempt.read_text(encoding="utf-8", errors="replace")
        match = RATE_LIMIT_RE.search(text)
        if not match:
            continue
        line = next((raw.strip() for raw in text.splitlines() if RATE_LIMIT_RE.search(raw)), "")
        hits.append(
            RateLimitHit(
                job_id=attempt.parent.name,
                path=attempt,
                updated_at=datetime.fromtimestamp(attempt.stat().st_mtime, timezone.utc),
                line_preview=line[:220],
            )
        )
    return sorted(hits, key=lambda hit: hit.updated_at, reverse=True)[:limit]


def rate_limit_report(env_file: str, limit: int = 5) -> str:
    settings = load_settings(env_file)
    hits = find_rate_limit_hits(settings.codex_task_root / "logs", limit)
    if not hits:
        return "No rate-limit events found in stored job logs."
    lines = ["Recent rate-limit events:"]
    for hit in hits:
        stamp = hit.updated_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append(f"{stamp} · {hit.job_id} · {hit.path.name}")
        if hit.line_preview:
            lines.append(f"  {truncate(hit.line_preview, 500)}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Report recent 429/rate-limit events from stored Codex job logs.")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    print(rate_limit_report(args.env, max(1, min(args.limit, 50))))


if __name__ == "__main__":
    main()
