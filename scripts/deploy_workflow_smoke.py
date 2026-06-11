#!/usr/bin/env python3
"""deploy_workflow_smoke.py — static checks for the auto-deploy setup.

Verifies:
  - .github/workflows/deploy.yml exists
  - scripts/deploy_vps.sh exists
  - scripts/deploy.sh exists
  - workflow references required secrets by name, not hardcoded values
  - deploy scripts contain `make smoke`
  - deploy scripts restart both services only after smoke
  - deploy scripts write `.deploy-status.json`
  - deploy scripts use flock for locking
  - deploy scripts exclude .env and .venv from rsync
  - deploy scripts do not print .env
  - workflow passes GitHub metadata (GITHUB_SHA, GITHUB_REF_NAME)
  - deploy_status command is registered in COMMAND_TABLE

Run: .venv/bin/python scripts/deploy_workflow_smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.harness_common import CheckResult, print_results  # noqa: E402

WORKFLOW_YML = REPO / ".github" / "workflows" / "deploy.yml"
DEPLOY_VPS_SH = REPO / "scripts" / "deploy_vps.sh"
DEPLOY_SH = REPO / "scripts" / "deploy.sh"
COMMANDS_PY = REPO / "handlers" / "commands.py"

REQUIRED_SECRETS = ("VPS_HOST", "VPS_USER", "VPS_SSH_KEY")


def _test_workflow_exists() -> CheckResult:
    name = "workflow: .github/workflows/deploy.yml exists"
    return CheckResult(name, WORKFLOW_YML.exists(), f"path={WORKFLOW_YML}")


def _test_deploy_vps_script_exists() -> CheckResult:
    name = "deploy: scripts/deploy_vps.sh exists"
    return CheckResult(name, DEPLOY_VPS_SH.exists(), f"path={DEPLOY_VPS_SH}")


def _test_deploy_script_exists() -> CheckResult:
    name = "deploy: scripts/deploy.sh exists"
    return CheckResult(name, DEPLOY_SH.exists(), f"path={DEPLOY_SH}")


def _test_workflow_references_secrets() -> CheckResult:
    name = "workflow: references required secrets (VPS_HOST, VPS_USER, VPS_SSH_KEY)"
    try:
        content = WORKFLOW_YML.read_text(encoding="utf-8")
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
        content = WORKFLOW_YML.read_text(encoding="utf-8")
        import re
        stripped = re.sub(r'\$\{\{[^}]+\}\}', '', content)
        ip_match = re.search(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', stripped)
        return CheckResult(
            name,
            ip_match is None,
            f"found hardcoded IP: {ip_match.group()}" if ip_match else "no hardcoded host",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_workflow_passes_github_metadata() -> CheckResult:
    name = "workflow: passes GITHUB_SHA and GITHUB_REF_NAME to deploy"
    try:
        content = WORKFLOW_YML.read_text(encoding="utf-8")
        has_sha = "GITHUB_SHA" in content
        has_ref = "GITHUB_REF_NAME" in content
        ok = has_sha and has_ref
        detail = []
        if not has_sha:
            detail.append("missing GITHUB_SHA")
        if not has_ref:
            detail.append("missing GITHUB_REF_NAME")
        return CheckResult(name, ok, "; ".join(detail) if detail else "both passed")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_contains_smoke(script_path: Path, label: str) -> CheckResult:
    name = f"deploy ({label}): contains 'make smoke'"
    try:
        content = script_path.read_text(encoding="utf-8")
        return CheckResult(name, "make smoke" in content, "found 'make smoke'")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_smoke_before_restart(script_path: Path, label: str) -> CheckResult:
    name = f"deploy ({label}): smoke runs before restart"
    try:
        content = script_path.read_text(encoding="utf-8")
        # Strip comment lines before searching for code patterns
        import re
        code_lines = [
            line for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        code = "\n".join(code_lines)
        smoke_pos = code.find("make smoke")
        # Look for actual restart commands, not comments
        restart_patterns = [
            "sudo systemctl restart",
            "systemctl restart",
        ]
        restart_pos = -1
        for pat in restart_patterns:
            pos = code.find(pat)
            if pos >= 0 and (restart_pos < 0 or pos < restart_pos):
                restart_pos = pos
        if smoke_pos < 0:
            return CheckResult(name, False, "'make smoke' not found")
        if restart_pos < 0:
            return CheckResult(name, False, "no 'systemctl restart' command found")
        ok = smoke_pos < restart_pos
        return CheckResult(
            name, ok,
            "smoke before restart" if ok else "restart appears before smoke",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_restarts_telegram(script_path: Path, label: str) -> CheckResult:
    name = f"deploy ({label}): restarts conveyor-telegram-bot"
    try:
        content = script_path.read_text(encoding="utf-8")
        return CheckResult(
            name,
            "conveyor-telegram-bot" in content and "restart" in content,
            "found restart of conveyor-telegram-bot",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_restarts_feishu(script_path: Path, label: str) -> CheckResult:
    name = f"deploy ({label}): restarts conveyor-feishu-bot"
    try:
        content = script_path.read_text(encoding="utf-8")
        return CheckResult(
            name,
            "conveyor-feishu-bot" in content and "restart" in content,
            "found restart of conveyor-feishu-bot",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_writes_status_file(script_path: Path, label: str) -> CheckResult:
    name = f"deploy ({label}): writes .deploy-status.json"
    try:
        content = script_path.read_text(encoding="utf-8")
        has_status = ".deploy-status.json" in content
        has_cat_status = 'cat > "${STATUS_FILE}"' in content or 'cat >"${STATUS_FILE}"' in content
        ok = has_status and has_cat_status
        return CheckResult(
            name, ok,
            "found status file write" if ok else f"has_status={has_status}, has_write={has_cat_status}",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_no_env_print(script_path: Path, label: str) -> CheckResult:
    name = f"deploy ({label}): does NOT print .env"
    try:
        content = script_path.read_text(encoding="utf-8")
        bad_patterns = ["cat .env", "less .env", "more .env", "head .env", "tail .env"]
        found = [p for p in bad_patterns if p in content]
        return CheckResult(
            name,
            len(found) == 0,
            f"found dangerous patterns: {found}" if found else "safe",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_uses_flock(script_path: Path, label: str) -> CheckResult:
    name = f"deploy ({label}): uses flock"
    try:
        content = script_path.read_text(encoding="utf-8")
        has_flock = "flock" in content
        return CheckResult(name, has_flock, "found flock" if has_flock else "no flock found")
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_excludes_env() -> CheckResult:
    name = "deploy (rsync): excludes .env and .venv"
    try:
        content = DEPLOY_SH.read_text(encoding="utf-8")
        has_env = "--exclude=.env" in content
        has_venv = "--exclude=.venv" in content
        ok = has_env and has_venv
        return CheckResult(
            name, ok,
            f"has_env_exclude={has_env}, has_venv_exclude={has_venv}",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_rollback_guard() -> CheckResult:
    name = "deploy (vps): has rollback guard on service failure"
    try:
        content = DEPLOY_VPS_SH.read_text(encoding="utf-8")
        has_rollback = "rollback" in content.lower() or "backup" in content.lower()
        has_active_check = "is-active" in content or "is_active" in content
        ok = has_rollback and has_active_check
        return CheckResult(
            name, ok,
            f"has_rollback={has_rollback}, has_active_check={has_active_check}",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_status_registered() -> CheckResult:
    name = "commands: /deploy_status registered in COMMAND_TABLE"
    try:
        content = COMMANDS_PY.read_text(encoding="utf-8")
        has_handler = "_deploy_status" in content
        has_spec = '"deploy_status"' in content or "'deploy_status'" in content
        ok = has_handler and has_spec
        return CheckResult(
            name, ok,
            f"has_handler={has_handler}, has_spec={has_spec}",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


def _test_deploy_status_writes_services_json() -> CheckResult:
    name = "deploy (vps): status file includes services JSON"
    try:
        content = DEPLOY_VPS_SH.read_text(encoding="utf-8")
        has_services = '"services"' in content
        has_telegram = '"telegram"' in content
        has_feishu = '"feishu"' in content
        ok = has_services and has_telegram and has_feishu
        return CheckResult(
            name, ok,
            f"has_services={has_services}, has_telegram={has_telegram}, has_feishu={has_feishu}",
        )
    except Exception as exc:
        return CheckResult(name, False, f"raised {type(exc).__name__}: {exc}")


CHECKS = [
    _test_workflow_exists,
    _test_deploy_vps_script_exists,
    _test_deploy_script_exists,
    _test_workflow_references_secrets,
    _test_workflow_no_hardcoded_host,
    _test_workflow_passes_github_metadata,
    lambda: _test_deploy_contains_smoke(DEPLOY_VPS_SH, "vps"),
    lambda: _test_deploy_contains_smoke(DEPLOY_SH, "rsync"),
    lambda: _test_deploy_smoke_before_restart(DEPLOY_VPS_SH, "vps"),
    lambda: _test_deploy_smoke_before_restart(DEPLOY_SH, "rsync"),
    lambda: _test_deploy_restarts_telegram(DEPLOY_VPS_SH, "vps"),
    lambda: _test_deploy_restarts_telegram(DEPLOY_SH, "rsync"),
    lambda: _test_deploy_restarts_feishu(DEPLOY_VPS_SH, "vps"),
    lambda: _test_deploy_restarts_feishu(DEPLOY_SH, "rsync"),
    lambda: _test_deploy_writes_status_file(DEPLOY_VPS_SH, "vps"),
    lambda: _test_deploy_writes_status_file(DEPLOY_SH, "rsync"),
    lambda: _test_deploy_no_env_print(DEPLOY_VPS_SH, "vps"),
    lambda: _test_deploy_no_env_print(DEPLOY_SH, "rsync"),
    lambda: _test_deploy_uses_flock(DEPLOY_VPS_SH, "vps"),
    lambda: _test_deploy_uses_flock(DEPLOY_SH, "rsync"),
    _test_deploy_excludes_env,
    _test_deploy_rollback_guard,
    _test_deploy_status_registered,
    _test_deploy_status_writes_services_json,
]


def main() -> int:
    results = [t() for t in CHECKS]
    print_results(results)
    ok = all(r.ok for r in results)
    print("deploy workflow smoke ok" if ok else "deploy workflow smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
