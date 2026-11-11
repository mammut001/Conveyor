import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CliTests(unittest.TestCase):
    def test_create_status_cancel_are_persistent_commands(self):
        with tempfile.TemporaryDirectory() as root:
            base=[sys.executable,'-m','conveyor.automation.overnight_cli','--db',str(Path(root)/'run.db')]
            created=json.loads(subprocess.check_output([*base,'create','--objective','check preview','--revision','a'*40,'--hours','.1','--max-tasks','3']))
            self.assertEqual(created['state'],'running')
            ident=created['id']
            cancelled=json.loads(subprocess.check_output([*base,'cancel',ident]))
            self.assertEqual(cancelled['state'],'cancelled')
            status=json.loads(subprocess.check_output([*base,'status',ident]))
            self.assertEqual(status['tasks'][0]['state'],'cancelled')
