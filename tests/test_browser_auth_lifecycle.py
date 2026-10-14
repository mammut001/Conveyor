"""G4 owner identity, cross-manager locking and awaited one-shot close."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from browser_auth_owner import owner_alive, process_starttime, release
from browser_auth_sessions import AuthSession, AuthSessionManager


FLAGS = {
    "CONVEYOR_REAL_BROWSER": "1",
    "CONVEYOR_BROWSER_EXTERNAL_READONLY": "1",
    "CONVEYOR_BROWSER_EXTERNAL_INTERACT": "1",
    "CONVEYOR_BROWSER_EXTERNAL_SUBMIT": "1",
    "CONVEYOR_TAKEOVER_ENABLED": "1",
    "CONVEYOR_BROWSER_AUTH_SESSIONS": "1",
    "CONVEYOR_BROWSER_AUTH_DOMAINS": "http://127.0.0.1:8765",
    "CONVEYOR_BROWSER_AUTH_TEST_LOOPBACK": "1",
    "CONVEYOR_REQUIRE_REAL_BROWSER": "1",
}


class G4CloseOwnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, FLAGS)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.settings = SimpleNamespace(codex_memory_root=Path(self.temp.name))
        self.mgr = AuthSessionManager(self.settings)

    async def asyncTearDown(self):
        watches = [s._watch for s in self.mgr.sessions.values() if s._watch]
        await self.mgr.revoke_all()
        await asyncio.gather(*watches, return_exceptions=True)

    async def _fake(self):
        with patch.object(AuthSession, "start", new_callable=AsyncMock):
            return await self.mgr.open("alice", "task", "http://127.0.0.1:8765")

    async def test_revoke_awaits_existing_close_after_caller_cancel(self):
        s = await self._fake()
        entered, proceed = asyncio.Event(), asyncio.Event()
        async def blocked(_js):
            entered.set()
            await proceed.wait()

        page = SimpleNamespace(evaluate=AsyncMock(side_effect=blocked))
        context = SimpleNamespace(
            pages=[page], clear_cookies=AsyncMock(), close=AsyncMock())
        driver = SimpleNamespace(stop=AsyncMock())
        s.context = context
        self.mgr._drivers[s.id] = driver
        directory = s.directory

        original = asyncio.create_task(self.mgr.revoke(s.id, operator="alice"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        original.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await original
        self.assertIn(s.id, self.mgr._closing)
        another = asyncio.create_task(self.mgr.revoke(s.id, operator="alice"))
        all_closes = asyncio.create_task(self.mgr.revoke_all())
        await asyncio.sleep(0)
        self.assertFalse(another.done())
        self.assertFalse(all_closes.done())
        self.assertIs(s._closing, self.mgr._closing[s.id][1])
        proceed.set()
        self.assertTrue(await another)
        await all_closes
        await s.close()  # idempotent even after removing from manager
        self.assertFalse(directory.exists())
        self.assertEqual(page.evaluate.await_count, 1)
        context.clear_cookies.assert_awaited_once()
        context.close.assert_awaited_once()
        driver.stop.assert_awaited_once()

    async def test_evaluate_timeout_is_bounded_and_cleanup_continues(self):
        s = await self._fake()
        async def never(_js):
            await asyncio.Event().wait()
        page = SimpleNamespace(evaluate=AsyncMock(side_effect=never))
        context = SimpleNamespace(
            pages=[page], clear_cookies=AsyncMock(), close=AsyncMock())
        driver = SimpleNamespace(stop=AsyncMock())
        s.context = context
        self.mgr._drivers[s.id] = driver
        start = time.monotonic()
        self.assertTrue(await self.mgr.revoke(s.id, operator="alice"))
        self.assertLess(time.monotonic() - start, 4.5)
        self.assertFalse(s.directory.exists())
        context.clear_cookies.assert_awaited_once()
        context.close.assert_awaited_once()
        driver.stop.assert_awaited_once()

    async def test_lock_and_owner_protect_second_manager(self):
        s = await self._fake()
        self.assertTrue(s.directory.joinpath("owner.json").exists())
        ident = json.loads((s.directory / "owner.json").read_text())
        self.assertEqual(ident, {"pid": os.getpid(),
                                 "starttime": process_starttime(os.getpid())})
        self.assertTrue(owner_alive(s.directory))
        b = AuthSessionManager(self.settings)
        self.assertTrue(s.directory.exists())
        self.assertEqual(b.sessions, {})
        # Even if the lock owner exits normally, a matching PID/start tick
        # keeps the profile protected until its owning session closes.
        release(s._owner_lock)
        s._owner_lock = None
        c = AuthSessionManager(self.settings)
        self.assertTrue(s.directory.exists())
        self.assertEqual(c.sessions, {})
        self.assertTrue(await self.mgr.revoke(s.id, operator="alice"))
        self.assertFalse(s.directory.exists())

    def test_stale_pid_starttick_removed_only_when_unlocked(self):
        root = self.mgr.root
        orphan = root / "session-crashed"
        orphan.mkdir(mode=0o700)
        (orphan / "owner.json").write_text(
            json.dumps({"pid": os.getpid(), "starttime": 1}))
        (orphan / "profile").mkdir()
        AuthSessionManager(self.settings)
        self.assertFalse(orphan.exists())
