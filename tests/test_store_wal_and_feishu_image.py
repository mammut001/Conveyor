"""Shared SQLite stores use WAL, hot transcript writes leave the event loop,
the Feishu image fallback needs no extra package, and local Claude files stay untracked."""
from __future__ import annotations

import asyncio
import inspect
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from conveyor.core.refinement_store import RefinementStore
from conveyor.core.transcript_store import TranscriptStore

REPO = Path(__file__).resolve().parent.parent


def _journal_mode(path: Path) -> str:
    conn = sqlite3.connect(str(path))
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()


class WalModeTest(unittest.TestCase):
    def test_transcript_store_uses_wal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state" / "job_queue.sqlite3"
            TranscriptStore(path)
            self.assertEqual(_journal_mode(path), "wal")

    def test_refinement_store_uses_wal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state" / "job_queue.sqlite3"
            RefinementStore(SimpleNamespace(codex_memory_root=Path(raw)), db_path=path)
            self.assertEqual(_journal_mode(path), "wal")


class DeleteSessionScrubsWalTest(unittest.TestCase):
    """Deleted message text must not stay readable in the .sqlite or -wal files."""

    SECRET = b"canary-zeta-4417-secret"

    def _seed(self, raw: str) -> Path:
        path = Path(raw) / "state" / "job_queue.sqlite3"
        store = TranscriptStore(path)
        for i in range(3):
            store.append_turn(
                "s1",
                f"question {i} {self.SECRET.decode()}",
                f"answer {i} {self.SECRET.decode()}",
                channel="web",
                operator_id="op1",
            )
        return path

    def _assert_secret_gone(self, path: Path) -> None:
        for candidate in (path, Path(f"{path}-wal")):
            if candidate.exists():
                # assertFalse keeps the raw bytes out of the failure message.
                self.assertFalse(self.SECRET in candidate.read_bytes(), f"secret still in {candidate.name}")

    def test_delete_session_leaves_no_secret_in_db_or_wal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = self._seed(raw)
            # A second connection (as the web console keeps open) stops SQLite
            # from checkpointing and removing the -wal file on the store's close.
            other = sqlite3.connect(str(path))
            try:
                other.execute("SELECT COUNT(*) FROM sessions").fetchall()
                self.assertTrue(TranscriptStore(path).delete_session("s1"))
                self._assert_secret_gone(path)
            finally:
                other.close()

    def test_delete_session_without_other_connections_leaves_no_secret(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = self._seed(raw)
            self.assertTrue(TranscriptStore(path).delete_session("s1"))
            self._assert_secret_gone(path)

    def test_delete_session_logs_warning_when_checkpoint_stays_busy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = self._seed(raw)
            # An open read transaction pins the WAL, so TRUNCATE cannot finish.
            reader = sqlite3.connect(str(path))
            try:
                reader.execute("BEGIN")
                reader.execute("SELECT COUNT(*) FROM session_messages").fetchall()
                with self.assertLogs("transcript_store", level="WARNING"):
                    self.assertTrue(TranscriptStore(path).delete_session("s1"))
                reader.execute("COMMIT")
            finally:
                reader.close()


class AppendTurnOffLoopTest(unittest.TestCase):
    def test_append_turn_async_writes_in_a_worker_thread(self) -> None:
        from handlers import session

        seen: list[int] = []

        def record(*_args, **_kwargs):
            seen.append(threading.get_ident())

        async def scenario() -> int:
            await session.append_turn_async(SimpleNamespace(), SimpleNamespace(), "q", "a", kind="chat")
            return threading.get_ident()

        with mock.patch.object(session, "append_turn", record):
            loop_thread = asyncio.run(scenario())
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], loop_thread)

    def test_async_handlers_use_the_threaded_append(self) -> None:
        from handlers import chat, jobs
        from handlers.tools import executors

        for func in (chat.ask_chat, jobs._execute_codex_job, executors.exec_pi_destructive_task):
            source = inspect.getsource(func)
            self.assertIn("await append_turn_async(", source, func.__name__)
            self.assertNotIn(" append_turn(", source, func.__name__)


class _FakeFeishuChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def upload_media(self, *_args, **_kwargs):
        raise AssertionError("old SDK path must not upload")

    async def send(self, chat_id, message):
        self.sent.append((chat_id, message))


class FeishuImageFallbackTest(unittest.TestCase):
    def test_fallback_reads_the_image_without_aiofiles(self) -> None:
        from channel.feishu import FeishuOutbound

        with tempfile.TemporaryDirectory() as raw:
            image = Path(raw) / "shot.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            channel = _FakeFeishuChannel()
            old_sdk = types.ModuleType("lark_oapi.channel")  # no MediaSource / OutboundImage
            with mock.patch.dict(sys.modules, {"aiofiles": None, "lark_oapi.channel": old_sdk}):
                asyncio.run(FeishuOutbound(channel).send_image("oc_chat", str(image), caption="hi"))
        self.assertEqual(channel.sent, [("oc_chat", {"image": b"\x89PNG\r\n\x1a\nfake", "caption": "hi"})])

    def test_aiofiles_is_not_used(self) -> None:
        source = (REPO / "channel" / "feishu.py").read_text(encoding="utf-8")
        self.assertNotIn("aiofiles", source)


@unittest.skipUnless(shutil.which("git") and (REPO / ".git").exists(), "git checkout required")
class GitignoreTest(unittest.TestCase):
    def test_local_claude_files_are_ignored_by_the_repo(self) -> None:
        for path in (".claude/settings.local.json", ".claude/worktrees/scratch/file.py"):
            # Only the repository's .gitignore counts, not a personal global one.
            res = subprocess.run(
                ["git", "-c", "core.excludesFile=/dev/null", "check-ignore", "--no-index", path],
                cwd=REPO, capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 0, path)
            self.assertEqual(res.stdout.strip(), path)


if __name__ == "__main__":
    unittest.main()
