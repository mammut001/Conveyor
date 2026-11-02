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
