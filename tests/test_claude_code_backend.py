from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Settings
from runner.claude_code import ClaudeCodeBackend
from runner.providers import create_agent_backend
from runner.types import Job, JobMode, JobState


def _settings(root: Path) -> Settings:
    root = root.resolve()
    state_root = root.parent / f".{root.name}-conveyor-test"
    return Settings(
        telegram_bot_token="unused",
        telegram_allowed_user_id=1,
        codex_workspace_root=root,
        codex_bin="codex",
        codex_task_root=state_root / "tasks",
        codex_model=None,
        codex_timeout_seconds=5,
        telegram_progress_seconds=1,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=state_root / "memory",
        user_timezone="UTC",
    )


class ClaudeCodeBackendTests(unittest.TestCase):
    def test_text_delta_is_visible(self) -> None:
        progress, result = ClaudeCodeBackend._claude_event({
            "type": "stream_event",
            "event": {"delta": {"type": "text_delta", "text": "hello"}},
        })
        self.assertEqual(progress, "hello")
        self.assertEqual(result, "")

    def test_thinking_delta_is_not_exposed(self) -> None:
        progress, result = ClaudeCodeBackend._claude_event({
            "type": "stream_event",
            "event": {"delta": {"type": "thinking_delta", "thinking": "private"}},
        })
        self.assertEqual((progress, result), ("", ""))

    def test_tool_use_becomes_compact_indicator(self) -> None:
        progress, _ = ClaudeCodeBackend._claude_event({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "secret"}}]},
        })
        self.assertEqual(progress, "🔧 Bash...")
        self.assertNotIn("secret", progress)

    def test_result_becomes_final_text(self) -> None:
        progress, result = ClaudeCodeBackend._claude_event({"type": "result", "result": "done"})
        self.assertEqual(progress, "")
        self.assertEqual(result, "done")

    def test_error_and_tool_result_are_translated_without_content(self) -> None:
        tool = ClaudeCodeBackend._parse_claude_event({
            "type": "user",
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": "tool-1",
                "content": "secret output", "is_error": True,
            }]},
        })
        self.assertEqual(tool.kind, "tool.failed")
        self.assertEqual(tool.tool_call_id, "tool-1")
        self.assertNotIn("secret", str(tool.payload))
        error = ClaudeCodeBackend._parse_claude_event({
            "type": "result", "subtype": "error_max_turns", "is_error": True,
            "result": "stopped",
        })
        self.assertEqual(error.error, "stopped")
        self.assertEqual(error.kind, "agent.error")

    def test_commands_are_mode_scoped_and_never_enable_shell_or_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            backend = ClaudeCodeBackend(_settings(Path(temp)))
            run = backend._claude_command(Job("run-1", JobMode.RUN, "inspect", "unused"))
            fix = backend._claude_command(Job("fix-1", JobMode.FIX, "edit", "unused"))
        self.assertIn("--safe-mode", run)
        self.assertIn("--strict-mcp-config", run)
        self.assertIn("--no-session-persistence", run)
        self.assertEqual(run[run.index("--permission-mode") + 1], "plan")
        self.assertEqual(run[run.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(fix[fix.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(fix[fix.index("--tools") + 1], "Read,Glob,Grep,Edit,Write")
        self.assertNotIn("Bash", " ".join(fix))
        self.assertNotIn("bypassPermissions", " ".join(fix))
        self.assertNotIn("inspect", run)
        self.assertNotIn("edit", fix)

    def test_factory_is_codex_by_default_and_invalid_selection_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONVEYOR_AGENT_BACKEND", None)
            backend = create_agent_backend(_settings(Path(temp)))
            self.assertEqual(backend.name, "codex")
            with self.assertRaisesRegex(ValueError, "Unsupported agent backend"):
                create_agent_backend(_settings(Path(temp)), "unknown")

    def test_child_environment_does_not_inherit_unrelated_service_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ,
            {"UNRELATED_SERVICE_PASSWORD": "do-not-pass", "TELEGRAM_BOT_TOKEN": "do-not-pass"},
            clear=False,
        ):
            child = ClaudeCodeBackend(_settings(Path(temp)))._child_env()
        self.assertNotIn("UNRELATED_SERVICE_PASSWORD", child)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", child)

    def test_attempt_persists_only_sanitized_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            fake = root / "fake-claude"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "events = [\n"
                " {'type':'stream_event','event':{'delta':{'type':'thinking_delta','thinking':'private chain'}}},\n"
                " {'type':'assistant','message':{'content':[{'type':'tool_use','id':'t1','name':'Edit','input':{'command':'dangerous-command'}}]}},\n"
                " {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'t1','content':'secret output'}]}},\n"
                " {'type':'result','subtype':'success','is_error':False,'result':'done'},\n"
                "]\n"
                "for event in events: print(json.dumps(event), flush=True)\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            backend = ClaudeCodeBackend(_settings(root))
            backend.claude_bin = str(fake)
            job = Job("job-1", JobMode.FIX, "edit safely", "unused")
            job.worktree_path = root
            job.log_path = root / "attempt.jsonl"
            job.final_message_path = root / "final.txt"
            progress: list[str] = []

            async def run() -> None:
                async def on_progress(value: str) -> None:
                    progress.append(value)
                await backend._run_codex_attempt(job, on_progress)

            asyncio.run(run())
            log = job.log_path.read_text(encoding="utf-8")
            self.assertEqual(job.return_code, 0)
            self.assertEqual(job.final_message_path.read_text(encoding="utf-8"), "done")
            self.assertIn("tool.started", log)
            self.assertIn("tool.completed", log)
            self.assertNotIn("private chain", log)
            self.assertNotIn("dangerous-command", log)
            self.assertNotIn("secret output", log)
            self.assertIn("🔧 Edit...", progress)

    def test_full_fix_lifecycle_isolated_until_apply_and_discard(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Conveyor Test"], cwd=root, check=True)
            tracked = root / "docs" / "tracked.md"
            tracked.parent.mkdir()
            tracked.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "docs/tracked.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)
            fake = root / ".git" / "fake-claude"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "from pathlib import Path\n"
                "Path('docs/tracked.md').write_text('after\\n', encoding='utf-8')\n"
                "print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':'edited'}), flush=True)\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            backend = ClaudeCodeBackend(_settings(root))
            backend.claude_bin = str(fake)

            async def scenario() -> None:
                async def on_progress(_value: str) -> None:
                    pass
                job = await backend.start(JobMode.FIX, "change docs/tracked.md", on_progress)
                for _ in range(200):
                    if job.state is not JobState.RUNNING:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(job.state, JobState.COMPLETED, job.error)
                self.assertEqual(tracked.read_text(encoding="utf-8"), "before\n")
                self.assertEqual((job.worktree_path / "docs" / "tracked.md").read_text(encoding="utf-8"), "after\n")
                self.assertIn("docs/tracked.md", await backend.diff_job(job.id, job.worktree_path))
                applied = await backend.apply_job(job.id, job.worktree_path)
                self.assertIn("Applied", applied)
                self.assertEqual(tracked.read_text(encoding="utf-8"), "after\n")
                discarded = await backend.discard_job(job.id, job.worktree_path)
                self.assertIn("Discarded", discarded)
                self.assertFalse(job.worktree_path.exists())

            asyncio.run(scenario())

    def test_full_lifecycle_cancel_terminates_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Conveyor Test"], cwd=root, check=True)
            (root / "README.md").write_text("unchanged\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)
            fake = root / ".git" / "slow-claude"
            fake.write_text(
                "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            backend = ClaudeCodeBackend(_settings(root))
            backend.claude_bin = str(fake)

            async def scenario() -> None:
                async def on_progress(_value: str) -> None:
                    pass
                job = await backend.start(JobMode.RUN, "wait", on_progress)
                for _ in range(200):
                    if job.process is not None:
                        break
                    await asyncio.sleep(0.01)
                self.assertIsNotNone(job.process)
                response = await backend.cancel()
                self.assertIn("Cancellation requested", response)
                for _ in range(200):
                    if job.state is not JobState.RUNNING:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(job.state, JobState.CANCELLED, job.error)
                self.assertEqual((root / "README.md").read_text(encoding="utf-8"), "unchanged\n")
                await backend.discard_job(job.id, job.worktree_path)

            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
