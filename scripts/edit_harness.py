#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
import shutil
import sys
import tempfile
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from runner import CodexRunner, JobMode, JobState
from scripts.harness_common import CheckResult, attempt_completed, print_results, run_command
from scripts.telegram_api import send_message


@dataclass(frozen=True)
class EditHarnessOutcome:
    code: int
    summary: str


def _git(args: list[str], cwd: Path) -> None:
    result = run_command(["git", *args], cwd=cwd, timeout=30)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")


async def run_edit_harness(env_file: str, notify: bool = False) -> EditHarnessOutcome:
    settings = load_settings(env_file)
    expected_token = f"EDIT_HARNESS_OK_{uuid4().hex[:8]}"
    progress_messages: list[str] = []
    results: list[CheckResult] = []

    temp_parent = Path(tempfile.mkdtemp(prefix="codex-edit-harness-"))
    try:
        repo = temp_parent / "repo"
        task_root = temp_parent / "tasks"
        repo.mkdir()
        _git(["init", "-q"], cwd=repo)
        (repo / "status.txt").write_text("status=before\n", encoding="utf-8")
        _git(["add", "status.txt"], cwd=repo)
        _git(["-c", "user.name=Harness", "-c", "user.email=harness@example.invalid", "commit", "-q", "-m", "seed"], cwd=repo)

        harness_settings = replace(
            settings,
            codex_workspace_root=repo.resolve(),
            codex_task_root=task_root.resolve(),
        )
        runner = CodexRunner(harness_settings)

        async def progress(message: str) -> None:
            progress_messages.append(message)
            if notify:
                await asyncio.to_thread(send_message, settings, message)

        prompt = "\n".join(
            [
                "Edit the repository file status.txt.",
                "Make its complete contents exactly:",
                "status=after",
                "",
                f"When done, reply exactly {expected_token}",
            ]
        )
        job = await runner.start(JobMode.FIX, prompt, progress)
        while runner.current_job and runner.current_job.id == job.id:
            await asyncio.sleep(1)

        final_job = runner.last_job or job
        final_text = final_job.summary.strip()
        results.append(CheckResult("codex edit job", final_job.state == JobState.COMPLETED, f"state={final_job.state.value} id={final_job.id}"))
        results.append(CheckResult("final token", expected_token in final_text, f"expected token present={expected_token in final_text}"))

        worktree = final_job.worktree_path
        if worktree and worktree.exists():
            status_path = worktree / "status.txt"
            content = status_path.read_text(encoding="utf-8", errors="replace") if status_path.exists() else ""
            diff = run_command(["git", "diff", "--", "status.txt"], cwd=worktree, timeout=30)
            results.append(CheckResult("file content", content == "status=after\n", repr(content)))
            results.append(CheckResult("git diff", diff.returncode == 0 and "+status=after" in diff.stdout, "expected status.txt diff present"))
        else:
            results.append(CheckResult("worktree", False, "missing job worktree"))

        if final_job.log_path and final_job.log_path.exists():
            results.append(CheckResult("turn.completed", attempt_completed(final_job.log_path), str(final_job.log_path)))
        else:
            results.append(CheckResult("turn.completed", False, "missing attempt log"))

        ok = all(result.ok for result in results)
        lines = [result.line() for result in results]
        if ok:
            lines.append(f"edit harness ok: {expected_token}")
        else:
            lines.append("edit harness failed")
        return EditHarnessOutcome(0 if ok else 1, "\n".join(lines))
    finally:
        shutil.rmtree(temp_parent, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a real edit harness against a temporary git repository.")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--notify", action="store_true", help="Send runner progress to Telegram during the edit harness")
    args = parser.parse_args()

    outcome = asyncio.run(run_edit_harness(args.env, args.notify))
    print(outcome.summary)
    raise SystemExit(outcome.code)


if __name__ == "__main__":
    main()
