#!/usr/bin/env python3
"""Static safety checks for the production transactional deploy path."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_vps.sh"


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise SystemExit(f"deploy transaction smoke failed: missing {label}: {needle}")


def main() -> int:
    text = SCRIPT.read_text(encoding="utf-8")
    require(text, "git worktree add --detach", "detached candidate validation")
    require(text, "--untracked-files=no", "tracked-dirty production gate")
    require(text, "queued=${QUEUED_COUNT}, running=${RUNNING_COUNT}", "idle queue gate")
    require(text, "src.backup(dst)", "SQLite online backup")
    require(text, "PRAGMA integrity_check", "backup integrity check")
    require(text, 'git reset --hard "${TARGET_COMMIT_FULL}"', "exact target cutover")
    require(text, 'git reset --hard "${OLD_COMMIT_FULL}"', "whole revision rollback")
    require(text, "git merge-base --is-ancestor", "validated SHA ancestry check")
    require(text, "Candidate validation FAILED; live checkout was not changed", "pre-cutover failure behavior")
    require(text, "--exclude=.env", "secret preservation")
    require(text, 'cat > "${CANDIDATE}/.env.test"', "self-contained candidate smoke fixture")
    require(text, "TELEGRAM_BOT_TOKEN=deploy-placeholder-token", "non-secret candidate bot token")
    require(text, "CODEX_MEMORY_ROOT=${CANDIDATE}/.smoke-memory", "isolated candidate memory root")

    candidate = text.index("Validating detached candidate")
    fixture = text.index('cat > "${CANDIDATE}/.env.test"')
    smoke = text.index("if ! make smoke")
    cutover = text.index("Cutting over live checkout")
    if not (candidate < fixture < smoke < cutover):
        raise SystemExit("deploy transaction smoke failed: candidate fixture/smoke/cutover ordering is unsafe")

    if "for f in Makefile config.py runner.py bot.py feishu_bot.py" in text:
        raise SystemExit("deploy transaction smoke failed: legacy partial-file rollback still present")

    print("deploy transaction smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
