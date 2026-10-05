#!/usr/bin/env python3
"""Static safety checks for the production transactional deploy path."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_vps.sh"
DB_HELPER = ROOT / "scripts" / "deploy_db.py"


def require(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise SystemExit(f"deploy transaction smoke failed: missing {label}: {needle}")


def main() -> int:
    text = SCRIPT.read_text(encoding="utf-8")
    require(text, "git worktree add --detach", "detached candidate validation")
    require(text, "--untracked-files=no", "tracked-dirty production gate")
    require(text, "queued=${QUEUED_COUNT}, running=${RUNNING_COUNT}", "idle queue gate")
    # The queue database belongs to the service user, so the gate and the
    # backup must go through the helper run as that user, never ~ of the deployer.
    helper = DB_HELPER.read_text(encoding="utf-8")
    require(text, "systemctl show -p User --value", "service user resolution")
    require(text, 'sudo -n -u "${SERVICE_USER}"', "queue database access as service user")
    require(text, "deploy_db idle", "idle queue gate via helper")
    require(text, "deploy_db backup", "SQLite backup via helper")
    require(helper, "src.backup(dst)", "SQLite online backup")
    require(helper, "PRAGMA integrity_check", "backup integrity check")
    if "dotenv_values('.env')" in text:
        raise SystemExit("deploy transaction smoke failed: deploy-user ~/.codex resolution still present")
    require(text, 'git reset --hard "${TARGET_COMMIT_FULL}"', "exact target cutover")
    require(text, 'git reset --hard "${OLD_COMMIT_FULL}"', "whole revision rollback")
    require(text, "git merge-base --is-ancestor", "validated SHA ancestry check")
    require(text, "Candidate validation FAILED; live checkout was not changed", "pre-cutover failure behavior")
    require(text, "--exclude=.env", "secret preservation")
    require(text, "write_smoke_fixture()", "isolated smoke fixture helper")
    require(text, "TELEGRAM_BOT_TOKEN=deploy-placeholder-token", "non-secret smoke token")
    require(text, 'write_smoke_fixture "${CANDIDATE}"', "candidate smoke fixture")
    require(text, 'write_smoke_fixture "${DEPLOY_PATH}"', "production smoke fixture")
    require(text, 'clean_smoke_fixture "${DEPLOY_PATH}"', "production fixture cleanup")

    candidate = text.index("Validating detached candidate")
    candidate_fixture = text.index('write_smoke_fixture "${CANDIDATE}"')
    first_smoke = text.index("if ! make smoke")
    cutover = text.index("Cutting over live checkout")
    production = text.index("Running production smoke tests")
    production_fixture = text.index('write_smoke_fixture "${DEPLOY_PATH}"', production)
    last_smoke = text.rindex("if ! make smoke")
    if not (candidate < candidate_fixture < first_smoke < cutover < production < production_fixture < last_smoke):
        raise SystemExit("deploy transaction smoke failed: candidate/production smoke ordering is unsafe")

    if "for f in Makefile config.py runner.py bot.py feishu_bot.py" in text:
        raise SystemExit("deploy transaction smoke failed: legacy partial-file rollback still present")

    print("deploy transaction smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
