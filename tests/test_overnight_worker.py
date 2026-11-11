import asyncio
import tempfile
import unittest
from pathlib import Path
from conveyor.automation.overnight_store import OvernightStore
from conveyor.automation.overnight_worker import execute_claim


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_stops_inflight_handler_and_discards_result(self):
        with tempfile.TemporaryDirectory() as root:
            store=OvernightStore(Path(root)/'run.db')
            run=store.create('test')
            store.enqueue(run,'repair','issue',{})
            claim=store.claim(run,'repair')
            started, stopped=asyncio.Event(),asyncio.Event()
            async def handler(task):
                started.set()
                try:
                    await asyncio.sleep(30)
                finally:
                    stopped.set()
            job=asyncio.create_task(execute_claim(store,claim,handler,lease_seconds=1,heartbeat_seconds=.01))
            await started.wait()
            store.cancel(run)
            self.assertFalse(await asyncio.wait_for(job,1))
            self.assertTrue(stopped.is_set())
            self.assertEqual(store.status(run)['tasks'][0]['state'],'cancelled')

    async def test_error_is_persisted_without_provider_secret(self):
        with tempfile.TemporaryDirectory() as root:
            store=OvernightStore(Path(root)/'run.db');run=store.create('test')
            store.enqueue(run,'verify','issue',{});claim=store.claim(run,'verify')
            async def handler(task):
                raise RuntimeError('secret-provider-credential')
            self.assertTrue(await execute_claim(store,claim,handler,lease_seconds=1,heartbeat_seconds=.01))
            task=store.status(run)['tasks'][0]
            self.assertEqual(task['state'],'blocked')
            self.assertNotIn('secret-provider-credential',task['result'])

    async def test_nonzero_process_exit_retries_without_exceeding_attempt_cap(self):
        from conveyor.automation.overnight_process import CommandFailure
        with tempfile.TemporaryDirectory() as root:
            store=OvernightStore(Path(root)/'run.db');run=store.create('retry process')
            store.enqueue(run,'repair','issue',{})
            async def handler(task):
                raise CommandFailure('failed',program='pi',category='nonzero_exit',return_code=1)
            for expected in ('queued','queued','blocked'):
                claim=store.claim(run,'repair')
                await execute_claim(store,claim,handler)
                self.assertEqual(store.status(run)['tasks'][0]['state'],expected)
            self.assertIsNone(store.claim(run,'repair'))
