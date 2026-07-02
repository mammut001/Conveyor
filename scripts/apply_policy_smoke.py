#!/usr/bin/env python3
"""apply_policy_smoke.py — env-free unit tests for apply safety policy.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.harness_common import CheckResult, print_results
from runner.apply_policy import ApplyPolicy, validate_apply_paths

def _fake_settings(tmp: Path, allow_high_risk=False, max_untracked_bytes=1048576):
    return SimpleNamespace(
        codex_workspace_root=tmp / "workspace",
        codex_task_root=tmp / "conveyor",
        codex_memory_root=tmp / "memory",
        conveyor_apply_allow_high_risk=allow_high_risk,
        conveyor_apply_max_untracked_bytes=max_untracked_bytes,
    )

def test_apply_policy() -> list[CheckResult]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        settings = _fake_settings(tmp)
        policy = ApplyPolicy(settings)
        
        results = []
        
        # 1. Normal source file allowed
        res = policy.validate_path("runner/worktree.py", kind="tracked")
        results.append(CheckResult("normal_file_allowed", res is None, f"res={res}"))
        
        # 2. .env denied
        res = policy.validate_path(".env", kind="tracked")
        results.append(CheckResult("env_denied", res is not None, f"res={res}"))
        
        # 3. .github/workflows/deploy.yml denied by default
        res = policy.validate_path(".github/workflows/deploy.yml", kind="tracked")
        results.append(CheckResult("deploy_yml_denied_by_default", res is not None, f"res={res}"))
        
        # 4. private key path denied
        res = policy.validate_path("id_rsa", kind="tracked")
        results.append(CheckResult("private_key_denied", res is not None, f"res={res}"))
        res = policy.validate_path("key.pem", kind="tracked")
        results.append(CheckResult("pem_denied", res is not None, f"res={res}"))
        
        # 5. token/secret/password filename denied
        res = policy.validate_path("my_token.txt", kind="tracked")
        results.append(CheckResult("token_denied", res is not None, f"res={res}"))
        res = policy.validate_path("secret_password.json", kind="tracked")
        results.append(CheckResult("password_denied", res is not None, f"res={res}"))
        
        # 6. absolute path denied
        res = policy.validate_path("/etc/passwd", kind="tracked")
        results.append(CheckResult("absolute_path_denied", res is not None, f"res={res}"))
        
        # 7. ../escape denied
        res = policy.validate_path("../escape", kind="tracked")
        results.append(CheckResult("path_traversal_denied", res is not None, f"res={res}"))
        
        # 8. untracked symlink denied
        wt = tmp / "wt"
        wt.mkdir(parents=True, exist_ok=True)
        # Create a symlink
        link_path = wt / "symlink_file"
        target_path = wt / "target_file"
        target_path.write_text("hello")
        try:
            link_path.symlink_to(target_path)
            res = policy.validate_path("symlink_file", kind="untracked", worktree_path=wt)
            results.append(CheckResult("symlink_denied", res is not None, f"res={res}"))
        except OSError:
            # Windows fallback or similar if symlinks not supported
            results.append(CheckResult("symlink_denied", True, "skipped due to lack of symlink support"))
            
        # 9. oversized untracked file denied
        settings_small = _fake_settings(tmp, max_untracked_bytes=10)
        policy_small = ApplyPolicy(settings_small)
        large_file = wt / "large_file"
        large_file.write_text("hello world, this is a large file")
        res = policy_small.validate_path("large_file", kind="untracked", worktree_path=wt)
        results.append(CheckResult("oversized_file_denied", res is not None and "file too large" in res, f"res={res}"))
        
        # 10. high-risk allowed when CONVEYOR_APPLY_ALLOW_HIGH_RISK=true
        settings_high = _fake_settings(tmp, allow_high_risk=True)
        policy_high = ApplyPolicy(settings_high)
        res = policy_high.validate_path(".github/workflows/deploy.yml", kind="tracked")
        results.append(CheckResult("deploy_yml_allowed_when_high_risk_true", res is None, f"res={res}"))
        
        # But secrets must still be denied even when allow_high_risk is true
        res = policy_high.validate_path(".env", kind="tracked")
        results.append(CheckResult("env_still_denied_when_high_risk_true", res is not None, f"res={res}"))
        
        # 11. validate_apply_paths batch check
        batch_res = validate_apply_paths(["runner/worktree.py", ".env"], kind="tracked", settings=settings)
        results.append(CheckResult("batch_validation_fails_if_any_denied", batch_res.allowed is False and len(batch_res.blocked_paths) == 1, f"allowed={batch_res.allowed}, blocked={batch_res.blocked_paths}"))
        
        return results

def main() -> int:
    results = test_apply_policy()
    print_results(results)
    ok = all(r.ok for r in results)
    print("apply policy smoke ok" if ok else "apply policy smoke failed")
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
