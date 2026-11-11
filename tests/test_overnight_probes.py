import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from conveyor.automation.overnight_probes import SourceProbes
from conveyor.automation.overnight_process import CommandFailure


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_reproduced_failure_becomes_finding_and_verification_command(self):
        @asynccontextmanager
        async def detached(revision):
            self.assertEqual(revision,'a'*40)
            yield Path('/fixture')
        probe={'name':'stream whitespace','reproduction':'expected exact delta whitespace','argv':['python','-c','immutable probe']}
        probes=SourceProbes(SimpleNamespace(detached=detached),[probe])
        with patch('conveyor.automation.overnight_probes.command',new_callable=AsyncMock,side_effect=CommandFailure('failed')) as process:
            findings=await probes.discover({'revision':'a'*40})
        self.assertEqual(process.await_count,2)
        self.assertEqual(findings[0]['scenario'],probe['name'])
        self.assertEqual(probes.verification_commands(),[probe['argv']])

    async def test_one_success_does_not_raise_false_issue(self):
        @asynccontextmanager
        async def detached(revision):
            yield Path('/fixture')
        probes=SourceProbes(SimpleNamespace(detached=detached),[{'name':'x','reproduction':'x','argv':['python']}])
        with patch('conveyor.automation.overnight_probes.command',new_callable=AsyncMock,side_effect=[CommandFailure('transient'),'ok']):
            self.assertEqual(await probes.discover({'revision':'a'*40}),[])
