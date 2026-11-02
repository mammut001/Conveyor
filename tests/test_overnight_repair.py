import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch,AsyncMock
from contextlib import asynccontextmanager
from overnight_repair import PiRepair


class RepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_pr_resumes_without_rerunning_agent(self):
        github=Mock();github._json.return_value=[{'number':34,'headRefOid':'a'*40}]
        adapter=PiRepair(SimpleNamespace(repo=Path('/unused')),github)
        with patch('overnight_repair.command',new_callable=AsyncMock) as process:
            self.assertEqual(await adapter({'issue':{'number':12}}),{'pr':34,'head':'a'*40})
            process.assert_not_awaited()

    async def test_out_of_scope_change_never_pushes_or_creates_pr(self):
        @asynccontextmanager
        async def detached(sha):
            yield Path('/fixture')
        github=Mock();github._json.return_value=[]
        workspaces=SimpleNamespace(repo=Path('/fixture'),detached=detached)
        commands=[]
        async def process(argv,**kw):
            commands.append(argv)
            if argv[:3]==['git','rev-parse','refs/remotes/origin/preview']:
                return 'a'*40
            if argv[:4]==['git','diff','--cached','--name-only']:
                return '.github/workflows/ci.yml\n'
            return ''
        with patch('overnight_repair.command',side_effect=process):
            with self.assertRaises(ValueError):
                await PiRepair(workspaces,github)({'issue':{'number':12},'finding':{'scenario':'fixture'}})
        self.assertFalse(any('push' in argv for argv in commands))
        github.command.assert_not_called()

    async def test_saved_commit_is_published_without_agent_rerun(self):
        github=Mock();github._json.return_value=[]
        adapter=PiRepair(SimpleNamespace(repo=Path('/fixture')),github)
        adapter._publish=AsyncMock(return_value={'pr':42,'head':'b'*40})
        async def process(argv,**kw):
            if argv[:2]==['git','fetch']:
                return ''
            if argv[:2]==['git','for-each-ref']:
                return 'b'*40
            if argv[:2]==['git','show']:
                return 'Fix preview issue #12'
            return 'a'*40
        with patch('overnight_repair.command',side_effect=process):
            result=await adapter({'issue':{'number':12}})
        self.assertEqual(result,{'pr':42,'head':'b'*40})
        adapter._publish.assert_awaited_once()

    async def test_repair_branch_is_unique_to_run(self):
        github=Mock();github._json.return_value=[{'number':34,'headRefOid':'a'*40,'state':'OPEN'}]
        adapter=PiRepair(SimpleNamespace(repo=Path('/unused')),github)
        await adapter({'issue':{'number':12},'run_id':'c'*32})
        self.assertIn('codex/nightly-issue-12-attempt-1-run-'+'c'*32,github._json.call_args.args)

    async def test_publication_pushes_saved_sha_not_checkout_head(self):
        github=Mock(repo='owner/repo');github.command.return_value='https://github.com/owner/repo/pull/42'
        adapter=PiRepair(SimpleNamespace(repo=Path('/fixture')),github)
        with patch('overnight_repair.command',new_callable=AsyncMock) as process:
            result=await adapter._publish(Path('/fixture'),'codex/repair',12,'a'*40,'b'*40)
        self.assertEqual(process.await_args.args[0][-1],'b'*40+':refs/heads/codex/repair')
        self.assertEqual(result['head'],'b'*40)
