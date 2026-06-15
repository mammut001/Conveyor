#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from scripts.doctor import check_disk, check_latest_job, check_runtime_dirs, check_workspace
from scripts.harness_common import CheckResult, check_minimax_models, check_systemd_active, latest_final_file, latest_job_dir
from scripts.job_audit import run_job_audit
from scripts.job_metadata import job_sort_time, load_job_metadata
from scripts.metrics_report import _latest_attempt_or_legacy, _state_and_usage
from scripts.offline_harnesses import run_offline_harnesses
from scripts.rate_limit_report import find_rate_limit_hits
from scripts.security_audit import run_security_audit
from scripts.triage import triage_lines


def _check_dict(result: CheckResult) -> dict[str, Any]:
    return {"name": result.name, "ok": result.ok, "detail": result.detail}


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def write_snapshot(settings, snapshot: dict[str, Any], name: str | None = None) -> Path:
    mode = str(snapshot.get("mode") or "snapshot")
    safe_name = name or f"latest-{mode}.json"
    if "/" in safe_name or safe_name in {"", ".", ".."}:
        raise ValueError("snapshot name must be a file name, not a path")
    health_dir = settings.codex_task_root / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    target = health_dir / safe_name
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def _job_metrics(logs_root: Path, limit: int) -> dict[str, Any]:
    if not logs_root.exists():
        return {
            "count": 0,
            "states": {},
            "success_rate": 0,
            "rate_limit_hits": 0,
            "average_duration_seconds": 0,
            "duration_samples": 0,
            "usage": {},
            "recent": [],
        }

    job_dirs = sorted([path for path in logs_root.iterdir() if path.is_dir()], key=job_sort_time, reverse=True)[:limit]
    states: dict[str, int] = {"completed": 0, "failed": 0, "running": 0, "unknown": 0}
    usage_totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    durations: list[int] = []
    rate_limited = 0
    recent: list[dict[str, Any]] = []

    for job_dir in job_dirs:
        metadata = load_job_metadata(job_dir)
        attempt = _latest_attempt_or_legacy(job_dir)
        final = latest_final_file(job_dir)
        state, usage, hit_rate_limit = _state_and_usage(attempt, bool(final and final.exists()))
        if metadata:
            if isinstance(metadata.get("state"), str):
                state = metadata["state"]
            hit_rate_limit = bool(metadata.get("rate_limited", hit_rate_limit))
            metadata_usage = metadata.get("usage")
            if isinstance(metadata_usage, dict) and metadata_usage:
                usage = {key: int(value) for key, value in metadata_usage.items() if isinstance(value, int)}
            duration = metadata.get("duration_seconds")
            if isinstance(duration, int) and duration >= 0:
                durations.append(duration)
        states[state] = states.get(state, 0) + 1
        rate_limited += 1 if hit_rate_limit else 0
        for key in usage_totals:
            usage_totals[key] += usage.get(key, 0)
        recent.append(
            {
                "id": job_dir.name,
                "state": state,
                "updated_at": _iso(job_sort_time(job_dir)),
                "duration_seconds": metadata.get("duration_seconds") if metadata else None,
                "rate_limited": hit_rate_limit,
                "summary": metadata.get("summary", "") if metadata else "",
            }
        )

    completed = states.get("completed", 0)
    return {
        "count": len(job_dirs),
        "states": states,
        "success_rate": round((completed / len(job_dirs)) * 100) if job_dirs else 0,
        "rate_limit_hits": rate_limited,
        "average_duration_seconds": round(sum(durations) / len(durations)) if durations else 0,
        "duration_samples": len(durations),
        "usage": usage_totals,
        "recent": recent[:8],
    }


def _latest_job(settings) -> dict[str, Any] | None:
    job_dir = latest_job_dir(settings)
    if not job_dir:
        return None
    metadata = load_job_metadata(job_dir) or {}
    return {
        "id": job_dir.name,
        "state": metadata.get("state", "unknown"),
        "updated_at": _iso(job_sort_time(job_dir)),
        "attempt": metadata.get("attempt"),
        "max_attempts": metadata.get("max_attempts"),
        "return_code": metadata.get("return_code"),
        "rate_limited": bool(metadata.get("rate_limited", False)),
        "duration_seconds": metadata.get("duration_seconds"),
        "summary": metadata.get("summary", ""),
        "log_path": metadata.get("log_path"),
        "worktree_path": metadata.get("worktree_path"),
    }


def health_snapshot(
    env_file: str,
    service_name: str,
    since: str,
    metrics_limit: int = 20,
    include_security: bool = True,
    include_offline: bool = True,
    offline_results: list[CheckResult] | None = None,
    security_results: list[CheckResult] | None = None,
) -> dict[str, Any]:
    settings = load_settings(env_file)
    doctor_results = [
        check_systemd_active(service_name),
        check_workspace(settings),
        check_minimax_models(settings),
        check_disk(settings.codex_task_root),
    ]
    doctor_results.extend(check_runtime_dirs(settings))
    doctor_results.extend(check_latest_job(settings))

    audit_results = run_job_audit(env_file, stale_minutes=90)
    if offline_results is None:
        offline_results = run_offline_harnesses(env_file, include_command=True) if include_offline else []
    if security_results is None:
        security_results = run_security_audit(env_file, service_name, since) if include_security else []
    all_checks = [*doctor_results, *audit_results, *offline_results, *security_results]
    hits = find_rate_limit_hits(settings.codex_task_root / "logs", 5)

    return {
        "ok": all(result.ok for result in all_checks),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "service": service_name,
        "workspace_root": str(settings.codex_workspace_root),
        "task_root": str(settings.codex_task_root),
        "mode": "full" if include_offline or include_security else "fast",
        "checks": {
            "doctor": [_check_dict(result) for result in doctor_results],
            "job_audit": [_check_dict(result) for result in audit_results],
            "offline_harnesses": [_check_dict(result) for result in offline_results],
            "security": [_check_dict(result) for result in security_results],
        },
        "metrics": _job_metrics(settings.codex_task_root / "logs", metrics_limit),
        "latest_job": _latest_job(settings),
        "rate_limits": [
            {
                "job_id": hit.job_id,
                "path": str(hit.path),
                "updated_at": hit.updated_at.isoformat(),
                "preview": hit.line_preview,
            }
            for hit in hits
        ],
        "triage": triage_lines(all_checks),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Emit a machine-readable health snapshot for the Telegram Codex runner.")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--service", default="conveyor-telegram-bot")
    parser.add_argument("--since", default="1 hour ago", help="Journal window for security scan")
    parser.add_argument("--metrics-limit", type=int, default=20)
    parser.add_argument("--no-security", action="store_true", help="Skip journal/security checks")
    parser.add_argument("--fast", action="store_true", help="Skip offline harnesses and journal/security checks")
    parser.add_argument("--no-offline", action="store_true", help="Skip offline harness subprocesses")
    parser.add_argument("--write", action="store_true", help="Write the snapshot to CODEX_TASK_ROOT/health/latest-<mode>.json")
    parser.add_argument("--write-name", default="", help="Override snapshot output file name under CODEX_TASK_ROOT/health")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON")
    args = parser.parse_args()

    try:
        snapshot = health_snapshot(
            args.env,
            args.service,
            args.since,
            metrics_limit=max(1, min(args.metrics_limit, 200)),
            include_security=not args.no_security and not args.fast,
            include_offline=not args.no_offline and not args.fast,
        )
    except RuntimeError as exc:
        snapshot = {
            "ok": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "service": args.service,
            "error": str(exc),
            "checks": {
                "doctor": [{"name": "configuration", "ok": False, "detail": str(exc)}],
                "job_audit": [],
                "offline_harnesses": [],
                "security": [],
            },
            "metrics": {},
            "latest_job": None,
            "rate_limits": [],
            "triage": ["- configuration: Check `.env` and required CODEX/Telegram settings."],
        }
    if args.write and "task_root" in snapshot:
        settings = load_settings(args.env)
        write_snapshot(settings, snapshot, args.write_name or None)
    print(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":") if args.compact else None, indent=None if args.compact else 2))
    raise SystemExit(0 if snapshot["ok"] else 1)


if __name__ == "__main__":
    main()
