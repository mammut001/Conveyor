"""Independent behavioral contract tests for Agent Supervisor.

Deterministic contract tests validating:
1. Worktree content identity tracking (target_identity):
   - Tracked working-file byte changes invalidate fingerprint at identical HEAD and status.
   - Untracked Unicode/space/quoted filename content changes invalidate fingerprint.
   - Staged/index content changes with identical HEAD/status and unchanged working-file bytes affect fingerprint.
   - >8MiB untracked file changed in-place invalidates fingerprint or raises SupervisorStateError fail-closed.
2. Review verdict parsing (parse_review_verdict):
   - Valid passing structured envelope with required keys and checks.
   - Valid failing verdict with issues correctly parsed as failed (not coerced true).
   - Rejection of malformed JSON, plain text PASS, mismatched SHA/fingerprint, non-bool passed,
     unresolved issues with passed=True, null/empty summary, missing checks, empty check name/command,
     missing/non-string output key, non-bool check passed, and failed checks with passed=True.
3. NDJSON CommandCode envelope parsing (_parse_ndjson_result):
   - Success envelope with finalText and usage metadata.
   - Failure envelope with error metadata/reason.
   - Non-result events with result-like keys do not overwrite final output.
   - Stream without final envelope must not be accepted as success.
4. Acceptance invalidation (effective_acceptance):
   - Dirty content changes at the same HEAD invalidate acceptance.
   - Legacy empty-fingerprint passing rows cannot validate.

These tests assert contract specifications independently of internal implementation details
and do NOT run live LLM/model calls or CLI workers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest

# Ensure supervisor module in repo root is importable
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from conveyor.agents.agent_supervisor import (
    ReviewEnvelopeError,
    SupervisorStateError,
    _connect,
    _parse_ndjson_result,
    create_task,
    effective_acceptance,
    parse_review_verdict,
    record_acceptance,
    target_identity,
)


def _init_git_repo(path: Path) -> Path:
    """Initialize a disposable git repository with canonical config and an initial commit."""
    repo = path.resolve()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Contract Test"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "contract@example.com"], check=True, capture_output=True)
    readme = repo / "README.md"
    readme.write_text("# Initial Repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial commit"], check=True, capture_output=True)
    return repo


class TestTargetIdentityContract(unittest.TestCase):
    """Behavioral contracts for target_identity worktree content tracking."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = _init_git_repo(Path(self._temp_dir.name) / "test_repo")

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_tracked_working_file_bytes_change_invalidates_fingerprint_at_same_head_and_status(self) -> None:
        """Different tracked working-file bytes with IDENTICAL HEAD+git status must change fingerprint.
        Preserve current HEAD for dirty changes (don't require different commit SHA)."""
        tracked_file = self.repo_dir / "service.py"
        tracked_file.write_text("print('version 1')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo_dir), "add", "service.py"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo_dir), "commit", "-m", "add service.py"], check=True, capture_output=True)

        # Dirty modification A (status: ' M service.py')
        tracked_file.write_text("print('version 2 - dirty A')\n", encoding="utf-8")
        id_a = target_identity(str(self.repo_dir))
        self.assertIn("sha", id_a)
        self.assertIn("fingerprint", id_a)

        # Dirty modification B (same HEAD, same porcelain status ' M service.py', different file bytes)
        tracked_file.write_text("print('version 2 - dirty B (altered)')\n", encoding="utf-8")
        id_b = target_identity(str(self.repo_dir))

        self.assertEqual(id_a["sha"], id_b["sha"], "HEAD SHA must be preserved across dirty working changes")
        self.assertNotEqual(
            id_a["fingerprint"],
            id_b["fingerprint"],
            "Changing tracked working-file bytes at identical HEAD and status MUST change content fingerprint",
        )

    def test_untracked_unicode_space_quoted_filenames_invalidate_fingerprint(self) -> None:
        """Untracked Unicode/space/quoted filenames: content changes at same status invalidate fingerprint."""
        untracked_path = self.repo_dir / "doc ñoño 测试 spaces.txt"

        # Version A
        untracked_path.write_text("initial untracked data\n", encoding="utf-8")
        id_a = target_identity(str(self.repo_dir))

        # Version B: same untracked filename and porcelain status, different content bytes
        untracked_path.write_text("modified untracked data with new bytes\n", encoding="utf-8")
        id_b = target_identity(str(self.repo_dir))

        self.assertEqual(id_a["sha"], id_b["sha"])
        self.assertNotEqual(
            id_a["fingerprint"],
            id_b["fingerprint"],
            "Content change in untracked file with Unicode/spaces must invalidate content fingerprint",
        )

    def test_staged_index_content_changes_affect_fingerprint_with_unchanged_final_working_bytes(self) -> None:
        """Staged/index content changes with same HEAD/status and unchanged final working-file bytes must affect identity."""
        tracked_file = self.repo_dir / "index_tracked.txt"
        tracked_file.write_text("base commit content\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo_dir), "add", "index_tracked.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo_dir), "commit", "-m", "commit index_tracked"], check=True, capture_output=True)

        final_working_bytes = "constant working-tree payload across both states\n"

        # State 1: stage X into index, then write final working bytes
        tracked_file.write_text("staged blob content X\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo_dir), "add", "index_tracked.txt"], check=True, capture_output=True)
        tracked_file.write_text(final_working_bytes, encoding="utf-8")
        id_state1 = target_identity(str(self.repo_dir))

        # State 2: stage Y into index, then restore identical working bytes
        tracked_file.write_text("staged blob content Y (different from X)\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo_dir), "add", "index_tracked.txt"], check=True, capture_output=True)
        tracked_file.write_text(final_working_bytes, encoding="utf-8")
        id_state2 = target_identity(str(self.repo_dir))

        self.assertEqual(id_state1["sha"], id_state2["sha"], "HEAD SHA must be identical")
        self.assertNotEqual(
            id_state1["fingerprint"],
            id_state2["fingerprint"],
            "Staged index changes must change content fingerprint even if final working-tree bytes are identical",
        )

    def test_large_untracked_file_changed_in_place_invalidates_fingerprint_or_raises_fail_closed(self) -> None:
        """A >8MiB untracked file changed in-place must change fingerprint OR raise SupervisorStateError fail-closed.
        A constant skipped/path-only identity is invalid."""
        large_file = self.repo_dir / "huge_untracked.bin"
        large_size = (8 * 1024 * 1024) + 4096  # Exceeds 8 MiB limit

        large_file.write_bytes(b"A" * large_size)
        try:
            id1 = target_identity(str(self.repo_dir))
        except SupervisorStateError:
            # Raising fail-closed on >8MiB file is valid contract behavior
            return

        # Mutate the >8MiB untracked file in place
        large_file.write_bytes(b"B" * large_size)
        try:
            id2 = target_identity(str(self.repo_dir))
        except SupervisorStateError:
            # Raising fail-closed after modification is also valid contract behavior
            return

        self.assertNotEqual(
            id1["fingerprint"],
            id2["fingerprint"],
            "A >8MiB untracked file changed in-place must change fingerprint OR raise SupervisorStateError fail-closed. Constant skipped/path-only identity is invalid.",
        )


class TestParseReviewVerdictContract(unittest.TestCase):
    """Behavioral contracts for review verdict envelope parsing."""

    EXPECTED_SHA = "a1b2c3d4e5f678901234567890abcdef12345678"
    EXPECTED_FINGERPRINT = "f0e1d2c3b4a596877896543210fedcba0123456789abcdef0123456789abcdef"

    def _valid_passing_payload(self) -> dict:
        return {
            "passed": True,
            "target_sha": self.EXPECTED_SHA,
            "target_fingerprint": self.EXPECTED_FINGERPRINT,
            "issues": [],
            "checks": [
                {
                    "name": "unit-contract-tests",
                    "command": "pytest tests/unit",
                    "passed": True,
                    "output": "10 passed in 0.4s",
                }
            ],
            "summary": "Verified all acceptance criteria and test checks successfully.",
        }

    def test_valid_passing_verdict_succeeds(self) -> None:
        """A passing review must have matching SHA/fingerprint, passed strictly bool True,
        summary nonempty string, issues empty, at least one check with nonempty name/command,
        output string key (empty allowed), passed strictly bool."""
        payload = self._valid_passing_payload()
        verdict = parse_review_verdict(
            json.dumps(payload),
            expected_sha=self.EXPECTED_SHA,
            expected_fingerprint=self.EXPECTED_FINGERPRINT,
        )
        self.assertIs(verdict["passed"], True)
        self.assertEqual(verdict["target_sha"], self.EXPECTED_SHA)
        self.assertEqual(verdict["target_fingerprint"], self.EXPECTED_FINGERPRINT)
        self.assertEqual(verdict["issues"], [])
        self.assertTrue(len(verdict["checks"]) >= 1)
        self.assertTrue(bool(verdict["summary"].strip()))

    def test_valid_passing_verdict_tolerates_empty_string_output_in_check(self) -> None:
        """Check output string key allows empty string."""
        payload = self._valid_passing_payload()
        payload["checks"][0]["output"] = ""
        verdict = parse_review_verdict(
            json.dumps(payload),
            expected_sha=self.EXPECTED_SHA,
            expected_fingerprint=self.EXPECTED_FINGERPRINT,
        )
        self.assertIs(verdict["passed"], True)
        self.assertEqual(verdict["checks"][0]["output"], "")

    def test_valid_passing_verdict_in_fenced_markdown_block(self) -> None:
        """Tolerates valid structured JSON inside markdown ```json ... ``` code fence."""
        payload = self._valid_passing_payload()
        fenced_text = f"Here is my verdict:\n```json\n{json.dumps(payload, indent=2)}\n```\nThank you."
        verdict = parse_review_verdict(
            fenced_text,
            expected_sha=self.EXPECTED_SHA,
            expected_fingerprint=self.EXPECTED_FINGERPRINT,
        )
        self.assertIs(verdict["passed"], True)

    def test_valid_failing_verdict_with_issues_parsed_as_failed_not_coerced_true(self) -> None:
        """Valid FAIL with issues should parse as failed, not be coerced true."""
        payload = {
            "passed": False,
            "target_sha": self.EXPECTED_SHA,
            "target_fingerprint": self.EXPECTED_FINGERPRINT,
            "issues": ["Test suite regression in test_api", "Missing auth validation check"],
            "checks": [
                {
                    "name": "unit-tests",
                    "command": "pytest tests/",
                    "passed": False,
                    "output": "1 failed, 9 passed",
                }
            ],
            "summary": "Review failed due to test failures and incomplete validation.",
        }
        verdict = parse_review_verdict(
            json.dumps(payload),
            expected_sha=self.EXPECTED_SHA,
            expected_fingerprint=self.EXPECTED_FINGERPRINT,
        )
        self.assertIs(verdict["passed"], False, "Failing review must not be coerced to True")
        self.assertEqual(len(verdict["issues"]), 2)

    def test_malformed_json_and_plain_text_rejected(self) -> None:
        """Malformed/ambiguous JSON or plain PASS text cannot pass."""
        for bad_input in ["PASS", "APPROVED", "{passed: true}", '{"passed": true, }', ""]:
            with self.subTest(bad_input=bad_input):
                with self.assertRaises(ReviewEnvelopeError):
                    parse_review_verdict(
                        bad_input,
                        expected_sha=self.EXPECTED_SHA,
                        expected_fingerprint=self.EXPECTED_FINGERPRINT,
                    )

    def test_target_sha_or_fingerprint_mismatch_rejected(self) -> None:
        """Mismatched SHA or fingerprint fails review validation."""
        payload = self._valid_passing_payload()

        # SHA mismatch
        with self.assertRaises(ReviewEnvelopeError):
            parse_review_verdict(
                json.dumps(payload),
                expected_sha="mismatched00000000000000000000000000000000",
                expected_fingerprint=self.EXPECTED_FINGERPRINT,
            )

        # Fingerprint mismatch
        with self.assertRaises(ReviewEnvelopeError):
            parse_review_verdict(
                json.dumps(payload),
                expected_sha=self.EXPECTED_SHA,
                expected_fingerprint="mismatched00000000000000000000000000000000000000000000000000000000",
            )

    def test_passed_not_strictly_bool_rejected(self) -> None:
        """Passed must be strictly bool (not truthy strings, integers, or None)."""
        for non_bool in ["true", "True", 1, "yes", None]:
            payload = self._valid_passing_payload()
            payload["passed"] = non_bool
            with self.subTest(non_bool=non_bool):
                with self.assertRaises(ReviewEnvelopeError):
                    parse_review_verdict(
                        json.dumps(payload),
                        expected_sha=self.EXPECTED_SHA,
                        expected_fingerprint=self.EXPECTED_FINGERPRINT,
                    )

    def test_passing_verdict_with_unresolved_issues_rejected(self) -> None:
        """Unresolved issues with overall pass cannot pass."""
        payload = self._valid_passing_payload()
        payload["passed"] = True
        payload["issues"] = ["Unresolved issue: documentation missing"]
        with self.assertRaises(ReviewEnvelopeError):
            parse_review_verdict(
                json.dumps(payload),
                expected_sha=self.EXPECTED_SHA,
                expected_fingerprint=self.EXPECTED_FINGERPRINT,
            )

    def test_passing_verdict_with_null_or_empty_summary_rejected(self) -> None:
        """Null or empty summary with overall pass cannot pass."""
        for empty_summary in ["", "   ", None]:
            payload = self._valid_passing_payload()
            payload["summary"] = empty_summary
            with self.subTest(empty_summary=empty_summary):
                with self.assertRaises(ReviewEnvelopeError):
                    parse_review_verdict(
                        json.dumps(payload),
                        expected_sha=self.EXPECTED_SHA,
                        expected_fingerprint=self.EXPECTED_FINGERPRINT,
                    )

    def test_passing_verdict_with_missing_check_evidence_rejected(self) -> None:
        """Missing check evidence cannot pass (at least one check required)."""
        payload = self._valid_passing_payload()
        payload["checks"] = []
        with self.assertRaises(ReviewEnvelopeError):
            parse_review_verdict(
                json.dumps(payload),
                expected_sha=self.EXPECTED_SHA,
                expected_fingerprint=self.EXPECTED_FINGERPRINT,
            )

    def test_check_with_empty_name_or_command_rejected(self) -> None:
        """Checks must have nonempty name and nonempty command."""
        # Empty name
        payload1 = self._valid_passing_payload()
        payload1["checks"] = [{"name": "", "command": "pytest", "passed": True, "output": ""}]
        with self.assertRaises(ReviewEnvelopeError):
            parse_review_verdict(
                json.dumps(payload1),
                expected_sha=self.EXPECTED_SHA,
                expected_fingerprint=self.EXPECTED_FINGERPRINT,
            )

        # Empty command
        payload2 = self._valid_passing_payload()
        payload2["checks"] = [{"name": "pytest", "command": "", "passed": True, "output": ""}]
        with self.assertRaises(ReviewEnvelopeError):
            parse_review_verdict(
                json.dumps(payload2),
                expected_sha=self.EXPECTED_SHA,
                expected_fingerprint=self.EXPECTED_FINGERPRINT,
            )

    def test_check_missing_output_or_non_string_rejected(self) -> None:
        """Check must have output string key (empty allowed, but key must exist and be str)."""
        payload = self._valid_passing_payload()
        del payload["checks"][0]["output"]
        with self.assertRaises(ReviewEnvelopeError):
            parse_review_verdict(
                json.dumps(payload),
                expected_sha=self.EXPECTED_SHA,
                expected_fingerprint=self.EXPECTED_FINGERPRINT,
            )

        payload2 = self._valid_passing_payload()
        payload2["checks"][0]["output"] = None
        with self.assertRaises(ReviewEnvelopeError):
            parse_review_verdict(
                json.dumps(payload2),
                expected_sha=self.EXPECTED_SHA,
                expected_fingerprint=self.EXPECTED_FINGERPRINT,
            )

    def test_check_passed_not_strictly_bool_rejected(self) -> None:
        """Check passed must be strictly bool."""
        for non_bool in ["true", 1, None]:
            payload = self._valid_passing_payload()
            payload["checks"][0]["passed"] = non_bool
            with self.subTest(non_bool=non_bool):
                with self.assertRaises(ReviewEnvelopeError):
                    parse_review_verdict(
                        json.dumps(payload),
                        expected_sha=self.EXPECTED_SHA,
                        expected_fingerprint=self.EXPECTED_FINGERPRINT,
                    )

    def test_passing_verdict_with_failed_check_rejected(self) -> None:
        """Any failed check cannot pass."""
        payload = self._valid_passing_payload()
        payload["passed"] = True
        payload["checks"].append(
            {"name": "lint", "command": "flake8", "passed": False, "output": "syntax error"}
        )
        with self.assertRaises(ReviewEnvelopeError):
            parse_review_verdict(
                json.dumps(payload),
                expected_sha=self.EXPECTED_SHA,
                expected_fingerprint=self.EXPECTED_FINGERPRINT,
            )


class TestParseNdjsonResultContract(unittest.TestCase):
    """Behavioral contracts for CommandCode NDJSON envelope parsing."""

    def test_actual_command_code_envelope_success(self) -> None:
        """Actual CommandCode envelope success: {"type":"result","subtype":"success","finalText":"ok","usage":{}}."""
        stream = "\n".join([
            json.dumps({"type": "init", "version": "1.0"}),
            json.dumps({"type": "result", "subtype": "success", "finalText": "ok", "usage": {"tokens": 42}}),
        ])
        text, meta = _parse_ndjson_result(stream)
        self.assertEqual(text, "ok")
        self.assertFalse(bool(meta.get("is_error")), "Success envelope must not have is_error flag set")

    def test_actual_command_code_envelope_failure(self) -> None:
        """Failure: {"type":"result","subtype":"error","finalText":"","error":"boom","usage":{}}."""
        stream = "\n".join([
            json.dumps({"type": "init", "version": "1.0"}),
            json.dumps({"type": "result", "subtype": "error", "finalText": "", "error": "boom", "usage": {}}),
        ])
        text, meta = _parse_ndjson_result(stream)
        has_error = bool(meta.get("is_error")) or meta.get("error") == "boom" or "boom" in str(meta)
        self.assertTrue(has_error, "Failure envelope must expose visible error metadata/reason")

    def test_non_result_events_with_result_like_keys_do_not_overwrite_final_output(self) -> None:
        """Non-result events with result-like keys must not overwrite final output."""
        stream = "\n".join([
            json.dumps({"type": "tool_result", "result": "intermediate artifact output"}),
            json.dumps({"type": "result", "subtype": "success", "finalText": "actual verified answer", "usage": {}}),
            json.dumps({"type": "event", "result": "trailing log entry", "text": "bad"}),
        ])
        text, meta = _parse_ndjson_result(stream)
        self.assertEqual(text, "actual verified answer", "Non-result events must not overwrite finalText")

    def test_no_final_envelope_must_not_be_accepted_as_success(self) -> None:
        """No final envelope must not be accepted as success."""
        stream = "\n".join([
            json.dumps({"type": "step", "status": "running"}),
            json.dumps({"type": "log", "message": "waiting for external process"}),
        ])
        text, meta = _parse_ndjson_result(stream)
        # Without a final success result envelope, it cannot be considered a successful execution
        is_success = bool(text) and not meta.get("is_error")
        self.assertFalse(is_success, "Stream missing a final result envelope must not be accepted as success")


class TestAcceptanceInvalidationContract(unittest.TestCase):
    """Behavioral contracts for supervisor acceptance persistence and invalidation."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        canonical_tmp = Path(self._temp_dir.name).resolve()
        self.repo_dir = _init_git_repo(canonical_tmp / "repo")
        self.settings = types.SimpleNamespace(codex_memory_root=canonical_tmp / "codex")

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _get_head(self) -> str:
        r = subprocess.run(["git", "-C", str(self.repo_dir), "rev-parse", "HEAD"],
                           check=True, capture_output=True, text=True)
        return r.stdout.strip()

    def test_dirty_content_change_invalidates_acceptance_at_same_head(self) -> None:
        """Changing dirty content must invalidate acceptance at same HEAD."""
        head = self._get_head()
        task = create_task(
            self.settings,
            goal="Contract test task",
            repo_path=str(self.repo_dir),
            worktree_path=str(self.repo_dir),
            base_sha=head,
        )
        task_id = task["id"]

        # Record passing acceptance against clean initial worktree
        rec = record_acceptance(self.settings, task_id, sha=head, passed=True)
        self.assertTrue(rec["passed"])

        # Verification 1: effective acceptance is valid for clean tree
        eff = effective_acceptance(self.settings, task_id)
        self.assertIsNotNone(eff, "Clean state matching recorded acceptance should be effective")

        # Mutate worktree with dirty change at identical HEAD
        dirty_file = self.repo_dir / "dirty_modification.txt"
        dirty_file.write_text("dirty worktree content invalidating prior acceptance\n", encoding="utf-8")

        # Verification 2: effective acceptance MUST now be invalidated
        eff_dirty = effective_acceptance(self.settings, task_id)
        self.assertIsNone(
            eff_dirty,
            "Changing dirty content at same HEAD must invalidate acceptance",
        )

    def test_legacy_empty_fingerprint_passing_rows_cannot_validate(self) -> None:
        """Legacy empty-fingerprint passing rows cannot validate."""
        head = self._get_head()
        task = create_task(
            self.settings,
            goal="Legacy acceptance task",
            repo_path=str(self.repo_dir),
            worktree_path=str(self.repo_dir),
            base_sha=head,
        )
        task_id = task["id"]

        # Insert acceptance row with empty fingerprint simulating legacy records
        conn = _connect(self.settings)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO supervisor_acceptance
                       (id, task_id, sha, passed, issues_json, evidence, evidence_path, fingerprint, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    ("acc-legacy-01", task_id, head, 1, "[]", "legacy evidence", "", "", "2026-01-01T00:00:00Z"),
                )
        finally:
            conn.close()

        # Legacy empty fingerprint must fail validation
        eff = effective_acceptance(self.settings, task_id)
        self.assertIsNone(
            eff,
            "Legacy empty-fingerprint passing rows cannot validate because content integrity cannot be verified",
        )


if __name__ == "__main__":
    unittest.main()
