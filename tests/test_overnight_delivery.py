import unittest
from unittest.mock import Mock,AsyncMock,patch
from tempfile import TemporaryDirectory
from conveyor.automation.overnight_delivery import PreviewDelivery, verified_merge_commit
from conveyor.automation.overnight_github import GitHubDelivery


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_candidate_cannot_restart_preview(self):
        with TemporaryDirectory() as root:
            restart=Mock()
            delivery=PreviewDelivery('/unused',root,Mock(),restart=restart)
            with self.assertRaises(ValueError):
                await delivery({'candidate':{'head':'a'*40}},{'head':'b'*40,'passed':True})
            restart.assert_not_called()

    def test_issue_cannot_close_on_failed_deployment(self):
        command=Mock();github=GitHubDelivery('o/r',command=command)
        with self.assertRaises(ValueError):
            github.close_verified_issue(1,{'healthy':False,'commit':'a'*40})
        command.assert_not_called()
        github.close_verified_issue(1,{'healthy':True,'commit':'a'*40})
        self.assertIn('completed',command.call_args.args[0])

    def test_deploy_pins_candidate_merge_instead_of_newer_preview_tip(self):
        head='a'*40;expected='b'*40;newer='c'*40;base='d'*40
        history=f"{newer} {expected} {'e'*40}\n{expected} {base} {head}"
        self.assertEqual(verified_merge_commit(history,head),expected)
        with self.assertRaises(ValueError):
            verified_merge_commit(history,'f'*40)
