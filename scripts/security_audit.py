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
    ok = (mode & 0o077) == 0
    detail = f"{env_path} mode={mode:o}"
    if not ok:
        detail += f" (suggest: chmod 0600 {env_path})"
    return CheckResult("env permissions", ok, detail)


def check_file_private(path: Path) -> CheckResult:
    if not path.exists():
        return CheckResult(f"private file: {path.name}", True, f"{path} not present")
    mode = _mode(path)
    ok = (mode & 0o077) == 0
    detail = f"{path} mode={mode:o}"
    if not ok:
        detail += f" (suggest: chmod 0600 {path})"
    return CheckResult(f"private file: {path.name}", ok, detail)


def check_dir_private(path: Path) -> CheckResult:
    if not path.exists():
        return CheckResult(f"private dir: {path.name}", True, f"{path} not present")
    mode = _mode(path)
    ok = (mode & 0o077) == 0
    detail = f"{path} mode={mode:o}"
    if not ok:
        detail += f" (suggest: chmod 0700 {path})"
    return CheckResult(f"private dir: {path.name}", ok, detail)


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

    rw_paths = _systemctl_show(service_name, "ReadWritePaths")
    user = _systemctl_show(service_name, "User") or "ubuntu"
    broad_home = f"/home/{user}"
    
    has_broad = False
    for p in rw_paths.split():
        clean_p = p.lstrip("-+:")
        if clean_p == broad_home:
            has_broad = True
            break
            
    results.append(CheckResult(
        f"{service_name} ReadWritePaths",
        not has_broad,
        f"ReadWritePaths={rw_paths or 'none'} (contains broad {broad_home})" if has_broad else f"ReadWritePaths={rw_paths or 'none'}"
    ))
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
    maintain_service = "conveyor-maintain.service"
    if run_command(["systemctl", "cat", maintain_service], timeout=10).returncode == 0:
        results.extend(check_service_hardening(maintain_service))
    task_mode = _mode(settings.codex_task_root) if settings.codex_task_root.exists() else 0
    results.append(CheckResult("task root permissions", bool(task_mode), f"{settings.codex_task_root} mode={task_mode:o}" if task_mode else f"{settings.codex_task_root} missing"))

    # Check repo .desktop-agent.env if present
    repo_desktop_env = root / ".desktop-agent.env"
    if repo_desktop_env.exists():
        results.append(check_file_private(repo_desktop_env))

    # Check ~/.local/share/conveyor/desktop-agent.env if present
    local_desktop_env = Path("~/.local/share/conveyor/desktop-agent.env").expanduser()
    if local_desktop_env.exists():
        results.append(check_file_private(local_desktop_env))

    # Check GOOGLE_TOKEN_PATH if configured
    if settings.google_token_path:
        results.append(check_file_private(Path(settings.google_token_path).expanduser().resolve()))

    # Check codex_memory_root mode 0700 or stricter
    memory_root = settings.codex_memory_root
    results.append(check_dir_private(memory_root))

    # Check codex_memory_root/secrets mode 0700 and token files 0600
    secrets_dir = memory_root / "secrets"
    if secrets_dir.exists():
        results.append(check_dir_private(secrets_dir))
        for token_file in secrets_dir.glob("*"):
            if token_file.is_file():
                results.append(check_file_private(token_file))

    # Check codex_memory_root/audit/tools.log mode 0600 if present
    tools_log = memory_root / "audit" / "tools.log"
    if tools_log.exists():
        results.append(check_file_private(tools_log))

    # Check codex_memory_root/desktop/uploads and screenshots mode 0700 if present
    uploads_dir = memory_root / "desktop" / "uploads"
    if uploads_dir.exists():
        results.append(check_dir_private(uploads_dir))
    screenshots_dir = memory_root / "desktop" / "screenshots"
    if screenshots_dir.exists():
        results.append(check_dir_private(screenshots_dir))

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit Telegram Codex runner security posture without printing secrets.")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--service", default="conveyor-telegram-bot")
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
