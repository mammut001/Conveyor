import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from conveyor.automation.overnight_process import command, child_environment, CommandFailure


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    def test_verification_environment_excludes_credentials(self):
        with patch.dict(os.environ,{'GITHUB_TOKEN':'github-secret','DEEPSEEK_API_KEY':'provider-secret','CONVEYOR_WEB_TOKEN':'web-secret'}):
            env=child_environment()
            self.assertNotIn('GITHUB_TOKEN',env)
            self.assertNotIn('DEEPSEEK_API_KEY',env)
            self.assertNotIn('CONVEYOR_WEB_TOKEN',env)
            self.assertEqual(child_environment(provider=True)['DEEPSEEK_API_KEY'],'provider-secret')

    async def test_output_limit_stops_process(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(CommandFailure):
                await command([sys.executable,'-c','print("x"*10000)'],cwd=root,output_limit=100)

    async def test_deadline_kills_process(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(asyncio.TimeoutError):
                await command([sys.executable,'-c','import time;time.sleep(30)'],cwd=root,timeout=.03)

    async def test_normal_command_returns_captured_output(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual((await command([sys.executable,'-c','print("ok")'],cwd=root)).strip(),'ok')

    async def test_discarded_event_stream_still_obeys_byte_limit(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(await command([sys.executable,'-c','print("event")'],cwd=root,retain_output=False),'')
            with self.assertRaises(CommandFailure):
                await command([sys.executable,'-c','print("x"*10000)'],cwd=root,retain_output=False,output_limit=100)

    async def test_failure_keeps_program_and_exit_code_without_output(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(CommandFailure) as caught:
                await command([sys.executable,'-c','print("private-provider-output");raise SystemExit(7)'],cwd=root,retain_output=False)
            self.assertEqual(caught.exception.diagnostics['return_code'],7)
            self.assertEqual(caught.exception.diagnostics['category'],'nonzero_exit')
            self.assertNotIn('private-provider-output',str(caught.exception.diagnostics))
