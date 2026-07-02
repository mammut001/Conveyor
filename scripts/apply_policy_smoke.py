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

async def test_apply_policy_hardening() -> list[CheckResult]:
    from runner import CodexRunner
    from runner.types import Job, JobMode
    from runner.apply_policy import CollectResult, ApplyValidationResult
    from unittest import mock
    import shutil

    results = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        
        # Create directories
        workspace = tmp / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        
        task_root = tmp / "conveyor"
        task_root.mkdir(parents=True, exist_ok=True)
        (task_root / "locks").mkdir(parents=True, exist_ok=True)
        
        memory_root = tmp / "memory"
        memory_root.mkdir(parents=True, exist_ok=True)

        settings = SimpleNamespace(
            codex_workspace_root=workspace,
            codex_task_root=task_root,
            codex_memory_root=memory_root,
            conveyor_apply_allow_high_risk=False,
            conveyor_apply_max_untracked_bytes=1048576,
        )

        runner = CodexRunner(settings)

        # -------------------------------------------------------------
        # Test 1: _copy_validated_untracked_files copies only explicitly provided validated files
        # -------------------------------------------------------------
        wt_path = tmp / "wt_copy"
        wt_path.mkdir(parents=True, exist_ok=True)
        (wt_path / "docs").mkdir(parents=True, exist_ok=True)
        
        # Create some files in the worktree
        (wt_path / "docs" / "file1.txt").write_text("content1")
        (wt_path / "docs" / "file2.txt").write_text("content2")
        (wt_path / "MEMORY.md").write_text("memory content")
        
        # Copy only docs/file1.txt
        copied = await runner._copy_validated_untracked_files(wt_path, ["docs/file1.txt", "MEMORY.md"])
        
        ok_copied = copied == 1
        ok_file1_copied = (workspace / "docs" / "file1.txt").read_text() == "content1"
        ok_file2_not_copied = not (workspace / "docs" / "file2.txt").exists()
        ok_memory_not_copied = not (workspace / "MEMORY.md").exists()
        
        results.append(CheckResult(
            "_copy_validated_untracked_files_only_copies_provided",
            ok_copied and ok_file1_copied and ok_file2_not_copied and ok_memory_not_copied,
            f"copied={copied} file1={ok_file1_copied} file2={not ok_file2_not_copied} memory={not ok_memory_not_copied}"
        ))

        # -------------------------------------------------------------
        # Test 2: old _copy_untracked_files() cannot bypass apply policy
        # -------------------------------------------------------------
        # We put a blocked file in the worktree (.env)
        (wt_path / ".env").write_text("SECRET=1")
        
        # Calling _copy_untracked_files should collect and validate it,
        # and since .env is blocked, it must raise a RuntimeError
        try:
            await runner._copy_untracked_files(wt_path)
            passed_bypass = False
        except RuntimeError as err:
            passed_bypass = "blocked paths" in str(err) or "Refusing" in str(err)
            
        results.append(CheckResult(
            "old_copy_untracked_files_cannot_bypass_policy",
            passed_bypass,
            f"raised expected error: {passed_bypass}"
        ))

        # Clean up .env and docs/file1.txt from destination to avoid interference
        if (workspace / "docs" / "file1.txt").exists():
            (workspace / "docs" / "file1.txt").unlink()
        if (workspace / ".env").exists():
            (workspace / ".env").unlink()

        # -------------------------------------------------------------
        # Test 3: apply path collection failure refuses apply, not silently proceeds
        # -------------------------------------------------------------
        job_id = "job-123"
        runner._last_job_id = mock.MagicMock(return_value=job_id)
        runner._last_worktree_path = mock.MagicMock(return_value=wt_path)
        
        # Mock _git call to return dirty status or clean status
        runner._git = mock.AsyncMock(return_value="")
        
        # Test 3a: Tracked collection failure
        with mock.patch("runner.apply_policy.collect_tracked_changed_files") as mock_collect_tracked:
            mock_collect_tracked.return_value = CollectResult.failure("git diff failed")
            
            res = await runner.apply_last_job()
            ok_refused_tracked = f"Refused to apply job {job_id}: could not collect changed files safely." in res
            
        # Test 3b: Untracked collection failure
        with mock.patch("runner.apply_policy.collect_tracked_changed_files") as mock_collect_tracked, \
             mock.patch("runner.apply_policy.collect_untracked_files") as mock_collect_untracked:
            mock_collect_tracked.return_value = CollectResult.success([])
            mock_collect_untracked.return_value = CollectResult.failure("git ls-files failed")
            
            res = await runner.apply_last_job()
            ok_refused_untracked = f"Refused to apply job {job_id}: could not collect changed files safely." in res
            
        results.append(CheckResult(
            "apply_collection_failure_refuses_apply",
            ok_refused_tracked and ok_refused_untracked,
            f"tracked_refused={ok_refused_tracked} untracked_refused={ok_refused_untracked}"
        ))

        # -------------------------------------------------------------
        # Test 4: if a new untracked file appears after validation, apply refuses
        # -------------------------------------------------------------
        # Reset the mock of _git to return status summary
        runner._git = mock.AsyncMock(return_value="")
        
        with mock.patch("runner.apply_policy.collect_tracked_changed_files") as mock_collect_tracked, \
             mock.patch("runner.apply_policy.collect_untracked_files") as mock_collect_untracked, \
             mock.patch("runner.apply_policy.validate_apply_paths") as mock_val, \
             mock.patch.object(runner, "_copy_validated_untracked_files", mock.AsyncMock(return_value=1)):
             
             # Tracked changes: none
             mock_collect_tracked.return_value = CollectResult.success([])
             
             # First collection (before validation) returns one file
             # Second collection (TOCTOU check before copy) returns two files (drift!)
             mock_collect_untracked.side_effect = [
                 CollectResult.success(["file2.txt"]),
                 CollectResult.success(["file2.txt", "file3.txt"])
             ]
             
             # Validation allowed
             mock_val.return_value = ApplyValidationResult(True, [])
             
             res = await runner.apply_last_job()
             
             ok_refused_toctou = (
                 f"Refused to apply job {job_id}: untracked files changed during apply. "
                 "Please rerun /diff and /apply."
             ) in res
             
        results.append(CheckResult(
            "apply_refuses_if_untracked_files_change_during_apply",
            ok_refused_toctou,
            f"res={res!r}"
        ))

    return results

def main() -> int:
    import asyncio
    results = test_apply_policy()
    results.extend(asyncio.run(test_apply_policy_hardening()))
    print_results(results)
    ok = all(r.ok for r in results)
    print("apply policy smoke ok" if ok else "apply policy smoke failed")
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
