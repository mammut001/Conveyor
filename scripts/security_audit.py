#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from scripts.harness_common import CheckResult, print_results, run_command


TOKEN_URL_RE = re.compile(r"api\.telegram\.org/bot\d+:[A-Za-z0-9_-]+")
BOT_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def check_env_permissions(env_path: Path) -> CheckResult:
    if not env_path.exists():
        return CheckResult("env permissions", False, f"{env_path} missing")
    mode = _mode(env_path)
    ok = mode & 0o077 == 0
    return CheckResult("env permissions", ok, f"{env_path} mode={mode:o}")


def check_repo_secret_patterns(root: Path) -> CheckResult:
    scanned = 0
    matches = 0
    ignored = {".git", ".venv", "__pycache__"}
    suffixes = {".py", ".md", ".service", ".timer", ".example", ".sh", ".txt"}
    for path in root.rglob("*"):
        if any(part in ignored for part in path.parts):
            continue
        if not path.is_file() or path.suffix not in suffixes:
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in (TOKEN_URL_RE, BOT_TOKEN_RE):
            matches += len(pattern.findall(text))
    return CheckResult("repo secret scan", matches == 0, f"scanned={scanned} suspicious_matches={matches}")


def check_recent_journal(service_name: str, since: str) -> CheckResult:
    result = run_command(["journalctl", "-u", service_name, "--since", since, "--no-pager"], timeout=20)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:300]
        return CheckResult("journal token scan", False, detail or "journalctl failed")
    matches = len(TOKEN_URL_RE.findall(result.stdout)) + len(BOT_TOKEN_RE.findall(result.stdout))
    return CheckResult("journal token scan", matches == 0, f"{service_name} since {since}: token_matches={matches}")


def _systemctl_show(service_name: str, key: str) -> str:
    result = run_command(["systemctl", "show", service_name, f"--property={key}", "--value"], timeout=10)
    return (result.stdout or result.stderr).strip()


def check_service_hardening(service_name: str) -> list[CheckResult]:
    expected = {
        "NoNewPrivileges": "yes",
        "PrivateTmp": "yes",
    }
    results: list[CheckResult] = []
    for key, value in expected.items():
        actual = _systemctl_show(service_name, key)
        results.append(CheckResult(f"{service_name} {key}", actual == value, f"{key}={actual or 'unknown'}"))
    protect_system = _systemctl_show(service_name, "ProtectSystem")
    results.append(CheckResult(f"{service_name} ProtectSystem", protect_system in {"full", "strict"}, f"ProtectSystem={protect_system or 'unknown'}"))
    return results


def run_security_audit(env_file: str, service_name: str, since: str) -> list[CheckResult]:
    settings = load_settings(env_file)
    root = Path(__file__).resolve().parents[1]
    env_path = Path(env_file).resolve()
    results = [
        check_env_permissions(env_path),
        check_repo_secret_patterns(root),
        check_recent_journal(service_name, since),
    ]
    results.extend(check_service_hardening(service_name))
    maintain_service = "codex-telegram-maintain.service"
    if run_command(["systemctl", "cat", maintain_service], timeout=10).returncode == 0:
        results.extend(check_service_hardening(maintain_service))
    task_mode = _mode(settings.codex_task_root) if settings.codex_task_root.exists() else 0
    results.append(CheckResult("task root permissions", bool(task_mode), f"{settings.codex_task_root} mode={task_mode:o}" if task_mode else f"{settings.codex_task_root} missing"))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit Telegram Codex runner security posture without printing secrets.")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--service", default="codex-telegram-bot")
    parser.add_argument("--since", default="1 hour ago", help="Journal window for token-pattern scan")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results = run_security_audit(args.env, args.service, args.since)
    if args.json:
        print(
            json.dumps(
                {
                    "ok": all(result.ok for result in results),
                    "checks": [
                        {"name": result.name, "ok": result.ok, "detail": result.detail}
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
