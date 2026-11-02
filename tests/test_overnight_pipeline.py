import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from overnight_store import OvernightStore
from overnight_worker import execute_claim
from overnight_pipeline import OvernightPipeline


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_roles_durably_handoff_and_gate_deployment(self):
        with tempfile.TemporaryDirectory() as root:
            store=OvernightStore(Path(root)/'run.db');run=store.create('fixture chat regression')
            store.enqueue(run,'discover','pass:1',{'url':'http://preview.fixture'})
            github=Mock();github.issue.return_value={'number':7,'url':'https://fixture/issues/7'}
            receipt={'passed':True,'head':'a'*40,'pr':8,'repo':'o/r','checks':['regression']}
            deploy=AsyncMock(return_value={'healthy':True,'commit':'b'*40})
            pipeline=OvernightPipeline(github=github,discover=AsyncMock(return_value=[{'scenario':'chat','reproduction':'fixture'}]),
                repair=AsyncMock(return_value={'pr':8,'head':'a'*40}),verify=AsyncMock(return_value=receipt),deploy=deploy)
            for role in ('discover','repair','verify'):
                task=store.claim(run,role)
                self.assertIsNotNone(task)
                self.assertTrue(await execute_claim(store,task,pipeline.handlers()[role]))
            self.assertEqual([t['state'] for t in store.status(run)['tasks']],['passed']*3+['queued'])
            self.assertEqual(store.status(run)['tasks'][-1]['role'],'discover')
            github.merge_verified.assert_called_once_with(8,receipt)
            deploy.assert_awaited_once()

    def test_budget_failure_rolls_back_handoff(self):
        with tempfile.TemporaryDirectory() as root:
            store=OvernightStore(Path(root)/'run.db');run=store.create('fixture',max_tasks=1)
            ident=store.enqueue(run,'discover','first',{});task=store.claim(run,'discover')
            with self.assertRaises(ValueError):
                store.advance(ident,task['lease'],{},(('repair','issue',{}),))
            self.assertEqual(store.status(run)['tasks'][0]['state'],'running')
