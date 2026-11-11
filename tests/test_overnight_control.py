import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from conveyor.automation.overnight_control import Controller


class ControlTests(unittest.TestCase):
    def test_start_status_cancel_and_failed_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'coordinator';root.mkdir()
            release=Path(directory)/'releases'/('a'*40);release.mkdir(parents=True)
            (Path(directory)/'current').symlink_to(release)
            (root/'config.json').write_text(json.dumps({'checks':['fixed']}))
            start,stop=Mock(),Mock();control=Controller(root,start=start,stop=stop)
            run=control.request({'action':'start','objective':'Check preview'})
            start.assert_called_once_with(run['id'])
            self.assertEqual(control.request({'action':'status','run_id':run['id']})['state'],'running')
            self.assertEqual(control.request({'action':'cancel','run_id':run['id']})['state'],'cancelled')
            stop.assert_called_once_with(run['id'])
            start.side_effect=RuntimeError('service failed')
            with self.assertRaises(RuntimeError):
                control.request({'action':'start','objective':'check'})

    def test_arbitrary_service_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            control=Controller(root,start=Mock(),stop=Mock())
            with self.assertRaises(ValueError):
                control.request({'action':'cancel','run_id':'conveyor-web.service'})
            control.stop.assert_not_called()

    def test_control_restart_resumes_only_active_runs(self):
        with tempfile.TemporaryDirectory() as root:
            start=Mock();control=Controller(root,start=start,stop=Mock())
            running=control.store.create('recover')
            control.store.enqueue(running,'discover','initial',{'revision':'a'*40})
            cancelled=control.store.create('cancelled');control.store.cancel(cancelled)
            self.assertEqual(control.resume_runs(),[running])
            start.assert_called_once_with(running)
