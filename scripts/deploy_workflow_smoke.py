#!/usr/bin/env python3
"""deploy_workflow_smoke.py — static checks for the auto-deploy setup.

Verifies:
  - .github/workflows/deploy.yml exists
  - scripts/deploy_vps.sh exists
  - workflow references required secrets by name, not hardcoded values
  - deploy script contains `make smoke`
  - deploy script restarts conveyor-telegram-bot
  - deploy script restarts conveyor-feishu-bot
  - deploy script does not print .env
  - deploy script uses flock or another lock

Run: .venv/bin/python scripts/deploy_workflow_smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.harness_common import CheckResult, print_results  # noqa: E402

WORKFLOW_PY = REPO / ".github" / "workflows" / "deploy.yml"
DEPLOY_SH = REPO / "scripts" / "deploy_vps.sh"

REQUIRED_SECRETS = ("VPS_HOST", "VPS_USER", "VPS_SSH_KEY")


def _test_workflow_exists() -> CheckResult:
    name = "workflow: .github/workflows/deploy.yml exists"
    return CheckResult(name, WORKFLOW_PY.exists(), f"path={WORKFLOW_PY}")


def _test_deploy_script_exists() -> CheckResult:
    name = "deploy: scripts/deploy_vps.sh exists"
    return CheckResult(name, DEPLOY_SH.exists(), f"path={DEPLOY_SH}")


def _test_workflow_references_secrets() -> CheckResult:
    name = "workflow: references required secrets (VPS_HOST, VPS_USER, VPS_SSH_KEY)"
    try:
        content = WORKFLOW_PY.read_text(encoding="utf-8")
        missing = [s for s in REQUIRED_SECRETS if s not in content]
        return CheckResult(
            name,
            len(missing) == 0,
            f"missing={missing}" if missing else "all 3 secrets referenced",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_workflow_no_hardcoded_host() -> CheckResult:
    name = "workflow: no hardcoded VPS host or IP"
    try:
        content = WORKFLOW_PY.read_text(encoding="utf-8")
        # A hardcoded host would be something like "ssh user@1.2.3.4"
        # but NOT "${{ secrets.VPS_HOST }}". Check for patterns like
        # digits-only IP or a literal hostname outside of ${{ }}.
        import re
        # Look for IP addresses outside of ${{ }} expressions
        stripped = re.sub(r'\$\{\{[^}]+\}\}', '', content)
        ip_match = re.search(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', stripped)
        return CheckResult(
            name,
            ip_match is None,
            f"found hardcoded IP: {ip_match.group()}" if ip_match else "no hardcoded host",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_contains_smoke() -> CheckResult:
    name = "deploy: script contains 'make smoke'"
    try:
        content = DEPLOY_SH.read_text(encoding="utf-8")
        return CheckResult(name, "make smoke" in content, "found 'make smoke'")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_restarts_telegram() -> CheckResult:
    name = "deploy: script restarts conveyor-telegram-bot"
    try:
        content = DEPLOY_SH.read_text(encoding="utf-8")
        return CheckResult(
            name,
            "conveyor-telegram-bot" in content and "restart" in content,
            "found restart of conveyor-telegram-bot",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_restarts_feishu() -> CheckResult:
    name = "deploy: script restarts conveyor-feishu-bot"
    try:
        content = DEPLOY_SH.read_text(encoding="utf-8")
        return CheckResult(
            name,
            "conveyor-feishu-bot" in content and "restart" in content,
            "found restart of conveyor-feishu-bot",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_no_env_print() -> CheckResult:
    name = "deploy: script does NOT print .env (no cat .env, no less .env)"
    try:
        content = DEPLOY_SH.read_text(encoding="utf-8")
        bad_patterns = ["cat .env", "less .env", "more .env", "head .env", "tail .env"]
        found = [p for p in bad_patterns if p in content]
        return CheckResult(
            name,
            len(found) == 0,
            f"found dangerous patterns: {found}" if found else "safe",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_uses_lock() -> CheckResult:
    name = "deploy: script uses flock or similar lock"
    try:
        content = DEPLOY_SH.read_text(encoding="utf-8")
        has_lock = "flock" in content or "lockfile" in content or ".deploy.lock" in content
        return CheckResult(name, has_lock, "found lock mechanism" if has_lock else "no lock found")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_fails_on_smoke_failure() -> CheckResult:
    name = "deploy: script exits nonzero if smoke fails (no restart)"
    try:
        content = DEPLOY_SH.read_text(encoding="utf-8")
        # The pattern should be: if ! make smoke; then die "..." or exit 1
        has_gate = (
            ("if ! make smoke" in content or "if make smoke" in content)
            and ("die" in content or "exit 1" in content or "exit 2" in content)
        )
        return CheckResult(
            name,
            has_gate,
            "found smoke failure gate" if has_gate else "no smoke failure gate found",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_workflow_on_main_push() -> CheckResult:
    name = "workflow: triggers on push to main"
    try:
        content = WORKFLOW_PY.read_text(encoding="utf-8")
        return CheckResult(
            name,
            "push" in content and "main" in content,
            "found push to main trigger",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


CHECKS = [
    _test_workflow_exists,
    _test_deploy_script_exists,
    _test_workflow_references_secrets,
    _test_workflow_no_hardcoded_host,
    _test_deploy_contains_smoke,
    _test_deploy_restarts_telegram,
    _test_deploy_restarts_feishu,
    _test_deploy_no_env_print,
    _test_deploy_uses_lock,
    _test_deploy_fails_on_smoke_failure,
    _test_workflow_on_main_push,
]


def main() -> int:
    results = [t() for t in CHECKS]
    print_results(results)
    ok = all(r.ok for r in results)
    print("deploy workflow smoke ok" if ok else "deploy workflow smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
