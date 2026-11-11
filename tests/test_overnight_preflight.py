import unittest
from pathlib import Path
from unittest.mock import Mock,patch,AsyncMock
from conveyor.automation.overnight_preflight import prepare_repository


class PreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_credentials_are_repository_local_and_remote_is_checked(self):
        github=Mock(repo='owner/repo')
        with patch('conveyor.automation.overnight_preflight.shutil.which',return_value='/tools/gh'), patch('conveyor.automation.overnight_preflight.command',new_callable=AsyncMock) as command:
            command.side_effect=['https://github.com/owner/repo.git','']
            await prepare_repository(Path('/fixture'),github)
            self.assertIn('--local',command.call_args.args[0])
            self.assertEqual(command.call_args.args[0][-1],'!/tools/gh auth git-credential')

    async def test_wrong_remote_cannot_receive_credentials(self):
        github=Mock(repo='owner/repo')
        with patch('conveyor.automation.overnight_preflight.shutil.which',return_value='/tools/gh'), patch('conveyor.automation.overnight_preflight.command',new_callable=AsyncMock,return_value='https://example.test/wrong.git') as command:
            with self.assertRaises(ValueError):
                await prepare_repository(Path('/fixture'),github)
            self.assertEqual(command.await_count,1)
