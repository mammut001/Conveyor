#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from redaction import truncate
from scripts.doctor import check_disk, check_latest_job, check_runtime_dirs, check_workspace
from scripts.harness_common import check_minimax_models, check_systemd_active
from scripts.job_audit import run_job_audit
from scripts.log_summary import summarize_log
from scripts.metrics_report import metrics_report
from scripts.offline_harnesses import run_offline_harnesses
from scripts.rate_limit_report import rate_limit_report
from scripts.security_audit import run_security_audit
from scripts.triage import triage_text


def _section(title: str, body: str) -> str:
    return f"{title}\n{body.strip() if body.strip() else '(empty)'}"


def _check_lines(results) -> str:
    return "\n".join(result.line() for result in results)


def diagnostics_report(env_file: str, service_name: str, since: str, metrics_limit: int = 20) -> str:
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
    offline_results = run_offline_harnesses(env_file, include_command=True)
    security_results = run_security_audit(env_file, service_name, since)
    check_results = [*doctor_results, *audit_results, *offline_results, *security_results]

    parts = [
        _section("Doctor", _check_lines(doctor_results)),
        _section("Metrics", metrics_report(env_file, metrics_limit)),
        _section("Job audit", _check_lines(audit_results)),
        _section("Offline harnesses", _check_lines(offline_results)),
        _section("Rate limits", rate_limit_report(env_file, 5)),
        _section("Security", _check_lines(security_results)),
        _section("Triage", triage_text(check_results)),
        _section("Latest log", summarize_log(env_file, "latest", limit=5)),
    ]
    return truncate("\n\n".join(parts), 3900)


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a compact all-in-one diagnostics report for the Telegram Codex runner.")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--service", default="codex-telegram-bot")
    parser.add_argument("--since", default="1 hour ago", help="Journal window for security scan")
    parser.add_argument("--metrics-limit", type=int, default=20)
    args = parser.parse_args()
    print(diagnostics_report(args.env, args.service, args.since, max(1, min(args.metrics_limit, 100))))


if __name__ == "__main__":
    main()
