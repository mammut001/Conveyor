import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from conveyor.automation.overnight_guard import check_guard,LeaseLost,LeaseGuard,bind_guard
from conveyor.automation.overnight_store import OvernightStore
from conveyor.automation.overnight_worker import execute_claim


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_thread_cannot_start_a_later_side_effect(self):
        with tempfile.TemporaryDirectory() as root:
            store=OvernightStore(Path(root)/'run.db');run=store.create('cancel side effects')
            store.enqueue(run,'verify','candidate',{});task=store.claim(run,'verify')
            entered,release,finished=threading.Event(),threading.Event(),threading.Event()
            effects=[]
            def background():
                entered.set();release.wait(2)
                try:
                    check_guard()
                    effects.append('publish')
                except LeaseLost:
                    pass
                finally:
                    finished.set()
            async def handler(_task):
                await asyncio.to_thread(background)
                return {}
            job=asyncio.create_task(execute_claim(store,task,handler,lease_seconds=1,heartbeat_seconds=.01))
            self.assertTrue(await asyncio.to_thread(entered.wait,1))
            store.cancel(run)
            self.assertFalse(await asyncio.wait_for(job,1))
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait,1))
            self.assertEqual(effects,[])

    def test_guard_checks_durable_cancellation_without_waiting_for_heartbeat(self):
        with tempfile.TemporaryDirectory() as root:
            store=OvernightStore(Path(root)/'run.db');run=store.create('cancel')
            store.enqueue(run,'repair','candidate',{});task=store.claim(run,'repair')
            with bind_guard(LeaseGuard(store,task)):
                check_guard()
                store.cancel(run)
                with self.assertRaises(LeaseLost):
                    check_guard()
