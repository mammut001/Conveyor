import copy
import json
import tempfile
import unittest
from pathlib import Path
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from conveyor.automation.overnight_objective import ObjectiveChecks,validate_checks
from conveyor.automation.overnight_process import CommandFailure
from conveyor.automation.overnight_store import OvernightStore


class ObjectiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=OvernightStore(self.root/'runs.sqlite3')
        self.run=self.store.create('Explain nightly task controls',hours=1,max_tasks=12)
        self.store.enqueue(self.run,'discover','initial',{'revision':'a'*40})
        self.task=self.store.claim(self.run,'discover')
        self.revisions=[]
        @asynccontextmanager
        async def detached(revision):
            self.revisions.append(revision)
            yield self.root
        self.adapter=ObjectiveChecks(self.store,self.run,SimpleNamespace(detached=detached))
        self.generated={'checks':[{'name':'nightly controls','reproduction':'Welcome explains controls',
                                  'python':"from pathlib import Path\nassert 'nightly' in Path('welcome.txt').read_text()"}]}

    async def prepare(self,*,cancel=False):
        async def agent(argv,**kwargs):
            self.assertTrue(kwargs['provider']);self.assertTrue(kwargs['sandboxed'])
            self.assertIn(self.store.status(self.run)['objective'],kwargs['stdin'])
            (Path(kwargs['output_root'])/'plan.json').write_text(json.dumps(self.generated))
            if cancel:
                self.store.cancel(self.run)
            return ''
        with patch('conveyor.automation.overnight_objective.command',side_effect=agent):
            return await self.adapter.prepare(self.task)

    async def test_goal_plan_is_frozen_and_reused_after_restart(self):
        metadata=await self.prepare()
        self.assertEqual(metadata['scenarios'],['nightly controls'])
        self.assertEqual(len(metadata['sha256']),64)
        with patch('conveyor.automation.overnight_objective.command',new_callable=AsyncMock) as agent:
            again=ObjectiveChecks(OvernightStore(self.root/'runs.sqlite3'),self.run,self.adapter.workspaces)
            self.assertEqual(await again.prepare(self.task),metadata)
            agent.assert_not_awaited()

    async def test_cancelled_planning_cannot_publish_late_plan(self):
        with self.assertRaisesRegex(ValueError,'lease'):
            await self.prepare(cancel=True)
        self.assertIsNone(self.store.acceptance(self.run))

    async def test_failed_frozen_checks_reproduce_twice_then_create_finding(self):
        await self.prepare()
        failure=CommandFailure('failed',category='nonzero_exit')
        with patch('conveyor.automation.overnight_objective.command',new_callable=AsyncMock,side_effect=failure) as process:
            findings=await self.adapter.discover({'revision':'a'*40})
        self.assertEqual(process.await_count,2)
        self.assertEqual(findings[0]['scenario'],'nightly controls')
        self.assertIn(self.adapter.frozen()['digest'],findings[0]['evidence'])
        self.assertFalse(process.await_args.kwargs['network'])
        self.assertTrue(process.await_args.kwargs['read_only'])
        self.assertNotIn('provider',process.await_args.kwargs)

    async def test_transient_failure_is_not_an_issue_and_verify_uses_pinned_head(self):
        await self.prepare()
        failure=CommandFailure('failed',category='nonzero_exit')
        with patch('conveyor.automation.overnight_objective.command',new_callable=AsyncMock,side_effect=[failure,'']) as process:
            self.assertEqual(await self.adapter.discover({'revision':'a'*40}),[])
        with patch('conveyor.automation.overnight_objective.command',new_callable=AsyncMock,return_value='') as process:
            checks=await self.adapter.verify({'candidate':{'head':'b'*40}})
        self.assertEqual(self.revisions[-1],'b'*40)
        self.assertIn(self.adapter.frozen()['digest'],checks[0])
        self.assertEqual(process.await_args.args[0][-1],self.generated['checks'][0]['python'])

    async def test_runner_failure_is_not_misreported_as_application_bug(self):
        await self.prepare()
        with patch('conveyor.automation.overnight_objective.command',new_callable=AsyncMock,side_effect=CommandFailure('limit',category='output_limit')):
            with self.assertRaises(CommandFailure):
                await self.adapter.discover({'revision':'a'*40})

    def test_malformed_empty_vacuous_and_duplicate_checks_are_rejected(self):
        invalid=[{}, {'checks':[]}, {'checks':[{'name':'x'}]}]
        constant=copy.deepcopy(self.generated);constant['checks'][0]['python']='assert True';invalid.append(constant)
        duplicate=copy.deepcopy(self.generated);duplicate['checks']*=2;invalid.append(duplicate)
        oversized=copy.deepcopy(self.generated);oversized['checks'][0]['python']='x'*12001;invalid.append(oversized)
        for document in invalid:
            with self.subTest(document=document),self.assertRaises(ValueError):
                validate_checks(document)

    async def test_repeat_goal_and_config_reuse_exact_contract_without_model(self):
        self.store.bind_configuration(self.run,{'objective_planning':True})
        await self.prepare()
        repeat=self.store.create('Explain nightly task controls',hours=1,max_tasks=12)
        self.store.bind_configuration(repeat,{'objective_planning':True})
        self.store.enqueue(repeat,'discover','initial',{'revision':'c'*40})
        claim=self.store.claim(repeat,'discover')
        adapter=ObjectiveChecks(self.store,repeat,self.adapter.workspaces)
        with patch('conveyor.automation.overnight_objective.command',new_callable=AsyncMock) as agent:
            await adapter.prepare(claim)
            agent.assert_not_awaited()
        self.assertEqual(adapter.frozen()['document']['checks'],self.generated['checks'])
        self.assertEqual(adapter.frozen()['document']['revision'],'c'*40)
        other=self.store.create('Explain nightly task controls',hours=1,max_tasks=12)
        self.store.bind_configuration(other,{'objective_planning':True,'new_scope':True})
        self.assertIsNone(self.store.reusable_acceptance(other))
