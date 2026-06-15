#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from scripts.harness_common import (
    CheckResult,
    attempt_completed,
    check_minimax_models,
    check_systemd_active,
    latest_attempt_file,
    latest_final_file,
    latest_job_dir,
    print_results,
)
from scripts.telegram_api import send_message


def check_workspace(settings) -> CheckResult:
    root = settings.codex_workspace_root
    if not root.exists():
        return CheckResult("workspace", False, f"{root} missing")
    git_dir = root / ".git"
    return CheckResult("workspace", git_dir.exists(), f"{root} git={'yes' if git_dir.exists() else 'no'}")


def check_runtime_dirs(settings) -> list[CheckResult]:
    results: list[CheckResult] = []
    for name, path in [
        ("task root", settings.codex_task_root),
        ("logs", settings.codex_task_root / "logs"),
        ("worktrees", settings.codex_task_root / "worktrees"),
    ]:
        results.append(CheckResult(name, path.exists(), str(path)))

    logs_root = settings.codex_task_root / "logs"
    worktrees_root = settings.codex_task_root / "worktrees"
    log_count = len([path for path in logs_root.iterdir() if path.is_dir()]) if logs_root.exists() else 0
    worktree_count = len([path for path in worktrees_root.iterdir() if path.is_dir()]) if worktrees_root.exists() else 0
    log_detail = f"{log_count} job log dirs"
    worktree_detail = f"{worktree_count} worktrees"
    if log_count >= 100:
        log_detail += "; consider scripts/lifecycle.py clean --keep 50"
    if worktree_count >= 100:
        worktree_detail += "; consider scripts/lifecycle.py clean --keep 50"
    results.append(CheckResult("log count", log_count < 200, log_detail))
    results.append(CheckResult("worktree count", worktree_count < 200, worktree_detail))
    return results


def check_latest_job(settings) -> list[CheckResult]:
    job_dir = latest_job_dir(settings)
    if not job_dir:
        return [CheckResult("latest job", False, "no logs found")]

    results = [CheckResult("latest job", True, str(job_dir))]
    attempt = latest_attempt_file(job_dir)
    final = latest_final_file(job_dir)
    if attempt:
        results.append(CheckResult("latest attempt", True, str(attempt)))
        results.append(CheckResult("latest completed", attempt_completed(attempt), "turn.completed present" if attempt_completed(attempt) else "turn.completed missing"))
    else:
        results.append(CheckResult("latest attempt", False, "missing attempt-*.jsonl"))

    if final and final.exists():
        preview = final.read_text(encoding="utf-8", errors="replace").strip().replace("\n", " ")[:180]
        results.append(CheckResult("latest final", bool(preview), preview or "empty"))
    else:
        results.append(CheckResult("latest final", False, "missing final file"))
    return results


def check_disk(path: Path) -> CheckResult:
    usage = shutil.disk_usage(path)
    free_pct = usage.free / usage.total if usage.total else 0
    detail = f"free={usage.free // (1024**3)}GiB total={usage.total // (1024**3)}GiB"
    return CheckResult("disk", free_pct > 0.10, detail)


def main() -> None:
    parser = argparse.ArgumentParser(description="Show operational health for the Telegram Codex runner.")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--service", default="conveyor-telegram-bot", help="systemd service name")
    parser.add_argument("--send-test", action="store_true", help="Send a Telegram Bot API test message")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    settings = load_settings(args.env)
    results: list[CheckResult] = [
        check_systemd_active(args.service),
        check_workspace(settings),
        check_minimax_models(settings),
        check_disk(settings.codex_task_root),
    ]
    results.extend(check_runtime_dirs(settings))
    results.extend(check_latest_job(settings))

    if args.send_test:
        try:
            send_message(settings, "Doctor harness Telegram send test.")
            results.append(CheckResult("telegram", True, "sendMessage ok"))
        except Exception as exc:
            results.append(CheckResult("telegram", False, f"sendMessage failed: {exc}"))

    if args.json:
        print(
            json.dumps(
                {
                    "ok": all(result.ok for result in results),
                    "checks": [
                        {
                            "name": result.name,
                            "ok": result.ok,
                            "detail": result.detail,
                        }
                        for result in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(0 if all(result.ok for result in results) else 1)

    ok = print_results(results)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
