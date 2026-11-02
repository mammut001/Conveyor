import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,Mock,patch
from overnight_store import OvernightStore
from overnight_pipeline import OvernightPipeline
from overnight_worker import execute_claim


class CompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_clean_passes_finish_bounded_run(self):
        with tempfile.TemporaryDirectory() as root:
            store=OvernightStore(Path(root)/'run.db');run=store.create('preview smoke',max_tasks=3)
            store.enqueue(run,'discover','initial',{'revision':'a'*40})
            pipeline=OvernightPipeline(github=Mock(),discover=AsyncMock(return_value=[]),repair=None,verify=None,deploy=None)
            for _ in range(2):
                claim=store.claim(run,'discover')
                self.assertTrue(await execute_claim(store,claim,pipeline.discover))
            self.assertEqual(store.reconcile(run)['state'],'completed')

    async def test_budget_exhaustion_blocks_without_crashing_worker(self):
        with tempfile.TemporaryDirectory() as root:
            store=OvernightStore(Path(root)/'run.db');run=store.create('preview smoke',max_tasks=1)
            store.enqueue(run,'discover','initial',{'revision':'a'*40})
            pipeline=OvernightPipeline(github=Mock(),discover=AsyncMock(return_value=[]),repair=None,verify=None,deploy=None)
            await execute_claim(store,store.claim(run,'discover'),pipeline.discover)
            self.assertEqual(store.reconcile(run)['state'],'blocked')

    def test_deadline_cancels_pending_tasks(self):
        with tempfile.TemporaryDirectory() as root:
            store=OvernightStore(Path(root)/'run.db');run=store.create('preview smoke')
            store.enqueue(run,'discover','initial',{})
            with patch('overnight_store.time.time',return_value=10**12):
                status=store.reconcile(run)
            self.assertEqual(status['state'],'expired')
            self.assertEqual(status['tasks'][0]['state'],'cancelled')
