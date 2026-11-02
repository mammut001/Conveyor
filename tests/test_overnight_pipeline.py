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

    async def test_goal_plan_is_frozen_before_any_discovery_or_issue(self):
        events=[]
        async def prepare(task):
            events.append('prepare')
            return {'sha256':'pinned','scenarios':['goal contract']}
        async def discover(payload):
            events.append('discover')
            return []
        pipeline=OvernightPipeline(github=Mock(),prepare=prepare,discover=discover,
            repair=AsyncMock(),verify=AsyncMock(),deploy=AsyncMock())
        result=await pipeline.discover({'payload':{'revision':'a'*40}})
        self.assertEqual(events,['prepare','discover'])
        self.assertEqual(result.result['acceptance_plan']['sha256'],'pinned')
        pipeline.github.issue.assert_not_called()

    async def test_multiple_failed_contracts_are_repaired_as_one_complete_candidate(self):
        import asyncio
        from overnight_worker import serve
        with tempfile.TemporaryDirectory() as root:
            store=OvernightStore(Path(root)/'runs.sqlite3')
            run=store.create('Restore both independent contracts',hours=1,max_tasks=12)
            store.enqueue(run,'discover','initial',{'revision':'a'*40,'clean_passes':0})
            current={'A':False,'B':False};candidates={};issued=[]
            async def discover(payload):
                return [{'scenario':'contract '+name,'reproduction':'Restore contract '+name,'evidence':'failed twice'}
                        for name,passed in current.items() if not passed]
            def issue(finding):
                issued.append(finding)
                return {'number':len(issued)}
            github=Mock();github.issue.side_effect=issue
            async def repair(payload):
                number=len(candidates)+1
                # A repair can only implement the failures it was told about.
                snapshot={name:passed or ('contract '+name in payload['finding']['reproduction'])
                          for name,passed in current.items()}
                candidates[number]=snapshot
                return {'pr':number,'head':'b'*40}
            async def verify(payload):
                if not all(candidates[payload['candidate']['pr']].values()):
                    raise ValueError('full frozen acceptance still has another known failure')
                return {'passed':True,'pr':payload['candidate']['pr'],'head':'b'*40,'checks':['A','B']}
            async def deploy(payload,receipt):
                current.update(candidates[receipt['pr']])
                return {'healthy':True,'commit':'c'*40}
            pipeline=OvernightPipeline(github=github,discover=discover,repair=repair,verify=verify,deploy=deploy)
            await asyncio.wait_for(serve(store,run,pipeline.handlers(),poll_seconds=.001),10)
            self.assertEqual(store.status(run)['state'],'completed')
            self.assertEqual(len(issued),1)
            self.assertTrue(all(current.values()))
            for name in ('A','B'):
                self.assertIn('contract '+name,issued[0]['reproduction'])

    async def test_group_identity_preserves_all_failures_across_order_and_revision_changes(self):
        first=[{'scenario':'B','reproduction':'Restore B','evidence':'failed at old sha'},
               {'scenario':'A','reproduction':'Restore A','evidence':'failed at old sha'}]
        second=[{**item,'evidence':'failed at new sha'} for item in reversed(first)]
        github=Mock();github.issue.return_value={'number':1}
        pipeline=OvernightPipeline(github=github,discover=AsyncMock(side_effect=[first,second]),
            repair=AsyncMock(),verify=AsyncMock(),deploy=AsyncMock())
        for _ in range(2):
            await pipeline.discover({'payload':{'revision':'a'*40}})
        left,right=[call.args[0] for call in github.issue.call_args_list]
        self.assertEqual(left['scenario'],right['scenario'])
        self.assertEqual(left['reproduction'],right['reproduction'])
        self.assertNotEqual(left['evidence'],right['evidence'])
        self.assertEqual(len(left['failures']),2)
