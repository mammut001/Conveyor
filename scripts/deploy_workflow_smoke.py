#!/usr/bin/env python3
"""Static invariants for CI-gated transactional production deployment."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
REMOTE = ROOT / "scripts" / "deploy_vps.sh"
MANUAL = ROOT / "scripts" / "deploy.sh"
COMMANDS = ROOT / "handlers" / "commands.py"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"deploy workflow smoke failed: {message}")


def main() -> int:
    for path in (WORKFLOW, REMOTE, MANUAL, COMMANDS):
        require(path.exists(), f"missing {path.relative_to(ROOT)}")

    workflow = WORKFLOW.read_text(encoding="utf-8")
    remote = REMOTE.read_text(encoding="utf-8")
    manual = MANUAL.read_text(encoding="utf-8")
    commands = COMMANDS.read_text(encoding="utf-8")

    for secret in ("VPS_HOST", "VPS_USER", "VPS_SSH_KEY"):
        require(secret in workflow, f"workflow missing {secret}")
    require("workflow_run:" in workflow and "workflows: [CI]" in workflow,
            "deployment is not gated by CI workflow completion")
    require("workflow_run.conclusion == 'success'" in workflow,
            "deployment does not require successful CI")
    require("workflow_run.head_branch == 'main'" in workflow,
            "deployment is not restricted to main")
    require("workflow_run.head_sha" in workflow,
            "workflow does not capture exact validated SHA")
    require("< scripts/deploy_vps.sh" in workflow and "bash -s" in workflow,
            "workflow must stream the validated deploy script over SSH")
    require("bash ${DEPLOY_PATH}/scripts/deploy_vps.sh" not in workflow,
            "workflow still executes the stale remote deploy script")
    stripped = re.sub(r"\$\{\{[^}]+\}\}", "", workflow)
    require(re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", stripped) is None,
            "workflow contains a hardcoded IPv4 host")

    for needle, label in (
        ("flock -n", "deploy lock"),
        ("--untracked-files=no", "tracked dirty gate"),
        ("git merge-base --is-ancestor", "target ancestry gate"),
        ("queued=${QUEUED_COUNT}, running=${RUNNING_COUNT}", "idle queue gate"),
        ("src.backup(dst)", "SQLite online backup"),
        ("PRAGMA integrity_check", "SQLite integrity check"),
        ("git worktree add --detach", "candidate worktree"),
        ('cat > "${CANDIDATE}/.env.test"', "candidate smoke fixture"),
        ("make smoke", "smoke gate"),
        ('git reset --hard "${TARGET_COMMIT_FULL}"', "whole revision cutover"),
        ('git reset --hard "${OLD_COMMIT_FULL}"', "whole revision rollback"),
        (".deploy-status.json", "deploy status record"),
    ):
        require(needle in remote, f"remote deploy missing {label}")
    require(remote.index("Validating detached candidate") < remote.index("Cutting over live checkout"),
            "live cutover occurs before candidate validation")
    require("cat .env" not in remote and "printenv" not in remote,
            "remote deploy may expose environment secrets")
    require("for f in Makefile config.py runner.py bot.py feishu_bot.py" not in remote,
            "legacy partial-file rollback remains")

    code = "\n".join(line for line in manual.splitlines() if not line.lstrip().startswith("#"))
    require("rsync" not in code, "manual deploy still has rsync mutation path")
    require("git fetch origin main" in manual and "git rev-parse origin/main" in manual,
            "manual deploy does not resolve canonical main")
    require('git show "${TARGET_COMMIT}:scripts/deploy_vps.sh"' in manual,
            "manual deploy does not stream target deployer")
    require("ssh \"${REMOTE}\"" in manual and "bash -s" in manual,
            "manual deploy does not delegate through SSH transactional path")

    require("_deploy_status" in commands and '"deploy_status"' in commands,
            "/deploy_status is not registered")

    print("deploy workflow smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
