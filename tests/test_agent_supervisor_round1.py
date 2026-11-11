"""Focused Round 1 repair regressions for agent_supervisor:
1. Content-bound target_identity (tracked bytes, staged index, modes/deletions,
   NUL-safe Unicode/quoted paths, incremental large-file hashing, symlink safety)
2. Strict content-bound record_acceptance / effective_acceptance / completion gate
3. Strict structured review verdict schema validation (parse_review_verdict)
4. Actual CommandCode NDJSON result/error envelope parsing (_parse_ndjson_result)
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CONVEYOR_AGENT_SUPERVISOR_ENABLED", "true")

from conveyor.core.config import Settings
from conveyor.agents import agent_supervisor as ags
from conveyor.agents.agent_supervisor import (
    CommandCodeExecutor,
    ReviewEnvelopeError,
    RoleConfig,
    SupervisorStateError,
    _parse_ndjson_result,
    build_reviewer_prompt,
    create_task,
    effective_acceptance,
    parse_review_verdict,
    record_acceptance,
    target_identity,
    transition,
)


def make_settings(tmp: Path) -> Settings:
    return Settings(
        telegram_bot_token="",
        telegram_allowed_user_id=0,
        codex_workspace_root=tmp,
        codex_bin="codex",
        codex_task_root=tmp,
        codex_model=None,
        codex_timeout_seconds=60,
        telegram_progress_seconds=3,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=tmp / "memory",
        user_timezone="UTC",
        agent_supervisor_enabled=True,
    )


def git_run(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_disposable_repo(tmp: Path) -> tuple[Path, Path, str]:
    repo = tmp / "repo"
    repo.mkdir()
    git_run(repo, "init", "-q")
    git_run(repo, "config", "user.email", "test@example.invalid")
    git_run(repo, "config", "user.name", "Supervisor Test")
    (repo / "module.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "script.sh").write_text("#!/bin/sh\necho baseline\n", encoding="utf-8")
    git_run(repo, "add", ".")
    git_run(repo, "commit", "-qm", "baseline")
    head = git_run(repo, "rev-parse", "HEAD").stdout.decode("ascii").strip()
    wt = tmp / "wt"
    git_run(repo, "worktree", "add", "--detach", str(wt), head)
    return repo, wt, head


class Round1TargetIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sup-r1-id-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo, self.wt, self.head = make_disposable_repo(self.tmp)

    def test_tracked_content_changes_at_identical_status_and_head(self) -> None:
        mod = self.wt / "module.py"
        mod.write_text("value = 2\n", encoding="utf-8")
        status_a = git_run(self.wt, "status", "--porcelain", "-uall").stdout
        id_a = target_identity(str(self.wt))

        mod.write_text("value = 3\n", encoding="utf-8")
        status_b = git_run(self.wt, "status", "--porcelain", "-uall").stdout
        id_b = target_identity(str(self.wt))

        self.assertEqual(status_a, status_b)
        self.assertEqual(id_a["sha"], self.head)
        self.assertEqual(id_b["sha"], self.head)
        self.assertNotEqual(id_a["fingerprint"], id_b["fingerprint"])

        # Restoring exact bytes restores exact fingerprint (pure content binding)
        mod.write_text("value = 2\n", encoding="utf-8")
        self.assertEqual(target_identity(str(self.wt))["fingerprint"], id_a["fingerprint"])

    def test_staged_index_modes_and_deletions_change_fingerprint(self) -> None:
        mod = self.wt / "module.py"
        mod.write_text("staged_v1\n", encoding="utf-8")
        git_run(self.wt, "add", "module.py")
        mod.write_text("same_working_tree\n", encoding="utf-8")
        status_1 = git_run(self.wt, "status", "--porcelain", "-uall").stdout
        id_1 = target_identity(str(self.wt))

        mod.write_text("staged_v2\n", encoding="utf-8")
        git_run(self.wt, "add", "module.py")
        mod.write_text("same_working_tree\n", encoding="utf-8")
        status_2 = git_run(self.wt, "status", "--porcelain", "-uall").stdout
        id_2 = target_identity(str(self.wt))

        self.assertEqual(status_1, status_2)
        self.assertNotEqual(id_1["fingerprint"], id_2["fingerprint"])

        # File mode change at same bytes
        git_run(self.wt, "reset", "--hard", "HEAD")
        base_clean = target_identity(str(self.wt))
        script = self.wt / "script.sh"
        os.chmod(script, 0o755)
        after_chmod = target_identity(str(self.wt))
        self.assertNotEqual(base_clean["fingerprint"], after_chmod["fingerprint"])

        # Unstaged deletion vs staged deletion
        os.chmod(script, 0o644)
        script.unlink()
        unstaged_del = target_identity(str(self.wt))
        self.assertNotEqual(unstaged_del["fingerprint"], base_clean["fingerprint"])
        git_run(self.wt, "rm", "-q", "script.sh")
        staged_del = target_identity(str(self.wt))
        self.assertNotEqual(staged_del["fingerprint"], unstaged_del["fingerprint"])

    def test_unicode_quoted_filenames_and_large_tail_diff_untracked(self) -> None:
        weird_name = '含 空格 "双引号" \'单引号\' $var.txt'
        ufile = self.wt / weird_name
        ufile.write_text("first unicode payload\n", encoding="utf-8")
        id_u1 = target_identity(str(self.wt))
        ufile.write_text("second unicode payload\n", encoding="utf-8")
        id_u2 = target_identity(str(self.wt))
        self.assertNotEqual(id_u1["fingerprint"], id_u2["fingerprint"])
        ufile.unlink()

        # >8 MiB file whose difference is ONLY past the 8 MiB boundary
        prefix = b"P" * (8 * 1024 * 1024 + 4096)
        big = self.wt / "large_asset.bin"
        big.write_bytes(prefix + b"TAIL_ONE")
        id_big_1 = target_identity(str(self.wt))
        big.write_bytes(prefix + b"TAIL_TWO")
        id_big_2 = target_identity(str(self.wt))
        self.assertNotEqual(id_big_1["fingerprint"], id_big_2["fingerprint"])

    def test_external_symlinks_and_invalid_repos_fail_closed(self) -> None:
        outside_file = self.tmp / "outside_secret.txt"
        outside_file.write_text("TOP_SECRET_OUTSIDE_BYTES\n", encoding="utf-8")
        outside_dir = self.tmp / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "nested.txt").write_text("outside nested", encoding="utf-8")

        # Internal relative symlink hashes link metadata safely without reading external bytes
        link = self.wt / "internal_link"
        os.symlink("module.py", link)
        id_link_1 = target_identity(str(self.wt))
        link.unlink()
        os.symlink("script.sh", link)
        id_link_2 = target_identity(str(self.wt))
        self.assertNotEqual(id_link_1["fingerprint"], id_link_2["fingerprint"])
        link.unlink()

        # Absolute external symlink must fail closed
        os.symlink(str(outside_file), link)
        with self.assertRaises(SupervisorStateError):
            target_identity(str(self.wt))
        link.unlink()

        # Relative escaping symlink must fail closed
        os.symlink("../outside_secret.txt", link)
        with self.assertRaises(SupervisorStateError):
            target_identity(str(self.wt))
        link.unlink()

        # Symlinked directory traversal must fail closed
        dir_link = self.wt / "escaped_dir"
        os.symlink(str(outside_dir), dir_link)
        with self.assertRaises(SupervisorStateError):
            target_identity(str(self.wt))
        dir_link.unlink()

        # Non-git plain directory and unborn git repo must fail closed
        plain = self.tmp / "plain_dir"
        plain.mkdir()
        with self.assertRaises(SupervisorStateError):
            target_identity(str(plain))

        unborn = self.tmp / "unborn_repo"
        unborn.mkdir()
        git_run(unborn, "init", "-q")
        with self.assertRaises(SupervisorStateError):
            target_identity(str(unborn))

        if os.geteuid() != 0:
            unreadable = self.wt / "unreadable.txt"
            unreadable.write_text("secret", encoding="utf-8")
            os.chmod(unreadable, 0)
            self.addCleanup(lambda: os.chmod(unreadable, stat.S_IRUSR | stat.S_IWUSR) if unreadable.exists() else None)
            with self.assertRaises(SupervisorStateError):
                target_identity(str(self.wt))


class Round1AcceptanceGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sup-r1-acc-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.settings = make_settings(self.tmp)
        self.repo, self.wt, self.head = make_disposable_repo(self.tmp)

    def test_rejects_missing_invalid_mismatched_and_legacy_empty_fingerprint(self) -> None:
        task = create_task(
            self.settings,
            goal="strict acceptance check",
            repo_path=str(self.repo),
            worktree_path=str(self.wt),
            base_sha=self.head,
        )
        tid = task["id"]
        transition(self.settings, tid, "implementing", stage="implementation")
        identity = target_identity(str(self.wt))

        # Missing / invalid / mismatched fingerprint or SHA rejected at record_acceptance
        for bad_fp in ("", None, "short-hex", "Z" * 64, "0" * 64):
            with self.assertRaises(SupervisorStateError):
                record_acceptance(
                    self.settings, tid, sha=identity["sha"], passed=True,
                    issues=[], evidence="ok", fingerprint=bad_fp,  # type: ignore[arg-type]
                )
        for bad_sha in ("", "not-a-sha", "0" * 40):
            with self.assertRaises(SupervisorStateError):
                record_acceptance(
                    self.settings, tid, sha=bad_sha, passed=True,
                    issues=[], evidence="ok", fingerprint=identity["fingerprint"],
                )
        # Passing acceptance with unresolved issues rejected
        with self.assertRaises(SupervisorStateError):
            record_acceptance(
                self.settings, tid, sha=identity["sha"], passed=True,
                issues=["still broken"], evidence="ok", fingerprint=identity["fingerprint"],
            )

        # Simulate a legacy SQLite row with empty fingerprint: must NOT validate or complete
        conn = ags._connect(self.settings)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO supervisor_acceptance
                       (id, task_id, sha, passed, issues_json, evidence, evidence_path, fingerprint, created_at)
                       VALUES (?, ?, ?, 1, '[]', 'legacy pass', '', '', ?)""",
                    ("acc-legacy-empty", tid, self.head, ags._utc_now()),
                )
        finally:
            conn.close()
        self.assertIsNone(effective_acceptance(self.settings, tid))
        with self.assertRaises(SupervisorStateError):
            transition(self.settings, tid, "completed")

        # Valid content-bound acceptance validates initially, then invalidates on tracked byte edit at same HEAD
        record_acceptance(
            self.settings, tid, sha=identity["sha"], passed=True,
            issues=[], evidence="verified", fingerprint=identity["fingerprint"],
        )
        self.assertIsNotNone(effective_acceptance(self.settings, tid))

        (self.wt / "module.py").write_text("value = 999\n", encoding="utf-8")
        self.assertEqual(target_identity(str(self.wt))["sha"], self.head)
        self.assertIsNone(effective_acceptance(self.settings, tid))
        with self.assertRaises(SupervisorStateError):
            transition(self.settings, tid, "completed")

        # Restoring exact accepted content allows completion, but mismatched transition args are rejected
        (self.wt / "module.py").write_text("value = 1\n", encoding="utf-8")
        with self.assertRaises(SupervisorStateError):
            transition(self.settings, tid, "completed", fingerprint="f" * 64)
        completed = transition(self.settings, tid, "completed")
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["content_fingerprint"], identity["fingerprint"])


class Round1ReviewVerdictTests(unittest.TestCase):
    def _valid_pass_payload(self, sha: str = "a" * 40, fp: str = "b" * 64) -> dict:
        return {
            "passed": True,
            "target_sha": sha,
            "target_fingerprint": fp,
            "issues": [],
            "checks": [
                {
                    "name": "unit_tests",
                    "command": "python -m unittest",
                    "passed": True,
                    "output": "",
                }
            ],
            "summary": "All checks passed cleanly against target snapshot.",
        }

    def test_valid_pass_with_empty_check_output_and_clean_fence_succeeds(self) -> None:
        payload = self._valid_pass_payload()
        parsed = parse_review_verdict(
            json.dumps(payload),
            expected_sha="a" * 40,
            expected_fingerprint="b" * 64,
        )
        self.assertTrue(parsed["passed"])
        self.assertEqual(parsed["checks"][0]["output"], "")

        fenced = f"```json\n{json.dumps(payload, indent=2)}\n```"
        parsed_fenced = parse_review_verdict(
            fenced,
            expected_sha="a" * 40,
            expected_fingerprint="b" * 64,
        )
        self.assertEqual(parsed_fenced, parsed)

    def test_rejects_schema_violations_coercions_and_ambiguous_verdicts(self) -> None:
        sha = "a" * 40
        fp = "b" * 64

        bad_mutations = [
            lambda p: p.update(summary=None),
            lambda p: p.update(summary="   "),
            lambda p: p.update(passed=1),
            lambda p: p.update(passed="true"),
            lambda p: p.update(issues=["still failing"]),
            lambda p: p.update(checks=[]),
            lambda p: p.update(target_sha="c" * 40),
            lambda p: p.update(target_fingerprint=fp[:32]),
            lambda p: p.update(extra_field=True),
            lambda p: p["checks"][0].pop("command"),
            lambda p: p["checks"][0].pop("output"),
            lambda p: p["checks"][0].update(output=None),
            lambda p: p["checks"][0].update(name=""),
            lambda p: p["checks"][0].update(command="   "),
            lambda p: p["checks"][0].update(passed=1),
            lambda p: p["checks"][0].update(passed=False),
            lambda p: p["checks"][0].update(extra="nope"),
        ]
        for idx, mutate in enumerate(bad_mutations):
            candidate = json.loads(json.dumps(self._valid_pass_payload(sha, fp)))
            mutate(candidate)
            with self.subTest(case=idx), self.assertRaises(ReviewEnvelopeError):
                parse_review_verdict(
                    json.dumps(candidate),
                    expected_sha=sha,
                    expected_fingerprint=fp,
                )

        # Reject fake PASS prose, trailing prose on raw JSON, multiple JSON objects, duplicate keys
        raw_valid = json.dumps(self._valid_pass_payload(sha, fp))
        for bad_text in (
            "PASS",
            f"PASS\n{raw_valid}",
            f"{raw_valid}\nLooks good!",
            f'{{"passed": false}}\n```json\n{raw_valid}\n```',
            f"```json\n{raw_valid}\n```\n```json\n{raw_valid}\n```",
            f"```json\n{raw_valid}",
            '{"passed": false, "passed": true, "target_sha": "' + sha + '", '
            '"target_fingerprint": "' + fp + '", "issues": [], '
            '"checks": [{"name":"t","command":"true","passed":true,"output":""}], "summary": "dup"}',
        ):
            with self.assertRaises(ReviewEnvelopeError):
                parse_review_verdict(bad_text, expected_sha=sha, expected_fingerprint=fp)

        # Empty expected_fingerprint must also fail closed
        with self.assertRaises(ReviewEnvelopeError):
            parse_review_verdict(raw_valid, expected_sha=sha, expected_fingerprint="")

    def test_reviewer_prompt_embeds_strict_schema_contract(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="sup-r1-prompt-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        _, wt, head = make_disposable_repo(tmp)
        identity = target_identity(str(wt))
        prompt = build_reviewer_prompt(
            goal="verify strict schema",
            worktree_path=str(wt),
            base_sha=head,
            current_sha=identity["sha"],
            fingerprint=identity["fingerprint"],
            analysis_plan="plan",
            implementer_summary="impl",
            acceptance_criteria="tests pass",
        )
        self.assertIn(f'"target_sha": "{identity["sha"]}"', prompt)
        self.assertIn(f'"target_fingerprint": "{identity["fingerprint"]}"', prompt)
        self.assertIn('"issues": []', prompt)


class Round1CommandCodeParserTests(unittest.TestCase):
    def test_parses_success_envelope_and_ignores_non_result_decoys(self) -> None:
        stream = "\n".join([
            json.dumps({"type": "event", "event": {"type": "session_start", "sessionId": "s-1"}}),
            json.dumps({
                "type": "event",
                "result": "TOP_LEVEL_DECOY",
                "finalText": "TOP_LEVEL_FINAL_DECOY",
                "event": {"type": "turn_end", "result": {"finalText": "NESTED_DECOY"}},
            }),
            json.dumps({
                "type": "result",
                "subtype": "success",
                "sessionId": "s-1",
                "stopReason": "end_turn",
                "usage": {"inputTokens": 10, "outputTokens": 20},
                "durationMs": 123,
                "finalText": "Verified final output payload.",
            }),
        ])
        text, meta = _parse_ndjson_result(stream)
        self.assertEqual(text, "Verified final output payload.")
        self.assertIs(meta["is_error"], False)
        self.assertEqual(meta["subtype"], "success")
        self.assertEqual(meta["error"], "")

    def test_preserves_error_reason_and_rejects_missing_or_ambiguous_envelopes(self) -> None:
        err_stream = "\n".join([
            json.dumps({"type": "event", "event": {"type": "turn_end", "result": {"finalText": "partial text"}}}),
            json.dumps({
                "type": "result",
                "subtype": "error",
                "sessionId": "s-err",
                "finalText": "should not be returned as success",
                "error": "Error: 403 MODEL_NOT_IN_PLAN: Model unavailable on current tier",
            }),
        ])
        text, meta = _parse_ndjson_result(err_stream)
        self.assertEqual(text, "")
        self.assertIs(meta["is_error"], True)
        self.assertEqual(meta["subtype"], "error")
        self.assertIn("403 MODEL_NOT_IN_PLAN", meta["error"])

        # Missing final result envelope (stream ended on non-result event)
        truncated = json.dumps({"type": "event", "result": "decoy", "event": {"result": {"finalText": "decoy"}}})
        t_trunc, m_trunc = _parse_ndjson_result(truncated)
        self.assertEqual(t_trunc, "")
        self.assertIs(m_trunc["is_error"], True)

        # Trailing non-result event with decoy result key does NOT overwrite actual result envelope
        trailing_event = "\n".join([
            json.dumps({"type": "result", "subtype": "success", "finalText": "real answer"}),
            json.dumps({"type": "event", "result": "decoy trailing", "finalText": "decoy"}),
        ])
        t_tr, m_tr = _parse_ndjson_result(trailing_event)
        self.assertEqual(t_tr, "real answer")
        self.assertIs(m_tr["is_error"], False)

        # Duplicate result envelopes -> rejected
        dup = "\n".join([
            json.dumps({"type": "result", "subtype": "error", "error": "boom"}),
            json.dumps({"type": "result", "subtype": "success", "finalText": "override"}),
        ])
        t_dup, m_dup = _parse_ndjson_result(dup)
        self.assertEqual(t_dup, "")
        self.assertIs(m_dup["is_error"], True)

    def test_actual_parent_command_code_evidence_logs_if_present(self) -> None:
        step5 = Path("/tmp/conveyor-supervisor-evidence/analysis-step5.jsonl")
        err_log = Path("/tmp/conveyor-supervisor-evidence/analysis.jsonl")
        if step5.is_file():
            txt, meta = _parse_ndjson_result(step5.read_text(encoding="utf-8", errors="replace"))
            self.assertEqual(len(txt), 5948)
            self.assertIs(meta["is_error"], False)
            self.assertEqual(meta["subtype"], "success")
        if err_log.is_file():
            txt_e, meta_e = _parse_ndjson_result(err_log.read_text(encoding="utf-8", errors="replace"))
            self.assertEqual(txt_e, "")
            self.assertIs(meta_e["is_error"], True)
            self.assertIn("MODEL_NOT_IN_PLAN", meta_e["error"])

    def test_command_code_executor_surfaces_envelope_error_reason_on_cli_run(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="sup-r1-cc-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        settings = make_settings(tmp)
        _, wt, _ = make_disposable_repo(tmp)

        fake_cli = tmp / "mock-command-code"
        fake_cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "_ = sys.stdin.read()\n"
            "print(json.dumps({'type': 'event', 'event': {'type': 'start'}}))\n"
            "print(json.dumps({'type': 'result', 'subtype': 'error', 'finalText': '', "
            "'error': 'Error: quota exhausted until 06:03 UTC'}))\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        os.chmod(fake_cli, 0o755)

        old_bin = os.environ.get("CONVEYOR_COMMAND_CODE_BIN")
        os.environ["CONVEYOR_COMMAND_CODE_BIN"] = str(fake_cli)
        try:
            executor = CommandCodeExecutor(settings)
            res = asyncio.run(
                executor.run(
                    role=RoleConfig("analyst", executor="command-code", tools=["read"], location="worktree"),
                    prompt="analyze",
                    worktree_path=str(wt),
                    timeout_seconds=15,
                )
            )
            self.assertFalse(res.success)
            self.assertIn("quota exhausted until 06:03 UTC", res.error)
        finally:
            if old_bin is None:
                os.environ.pop("CONVEYOR_COMMAND_CODE_BIN", None)
            else:
                os.environ["CONVEYOR_COMMAND_CODE_BIN"] = old_bin


if __name__ == "__main__":
    unittest.main()
