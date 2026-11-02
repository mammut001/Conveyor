import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from overnight_sandbox import sandbox_argv


class SandboxTests(unittest.TestCase):
    def test_missing_sandbox_never_falls_back_to_host(self):
        with patch('overnight_sandbox.shutil.which',return_value=None):
            with self.assertRaises(RuntimeError):
                sandbox_argv(['pi'],'/tmp')

    def test_mounts_only_source_and_system_runtime(self):
        with tempfile.TemporaryDirectory() as root, patch('overnight_sandbox.sys.platform','linux'), patch('overnight_sandbox.shutil.which',return_value='/usr/bin/bwrap'):
            argv=sandbox_argv(['pi','--mode','json'],root)
        self.assertNotIn('/opt/conveyor',argv)
        self.assertNotIn('/home/ubuntu',argv)
        self.assertIn('--cap-drop',argv)
        self.assertEqual(argv[-4:],['--','pi','--mode','json'])

    def test_agent_cannot_search_generated_bundles_but_verifier_can(self):
        with tempfile.TemporaryDirectory() as root, patch('overnight_sandbox.sys.platform','linux'), patch('overnight_sandbox.shutil.which',return_value='/usr/bin/bwrap'):
            (Path(root)/'web/dist').mkdir(parents=True)
            agent=sandbox_argv(['pi'],root,source_only=True)
            verifier=sandbox_argv(['python'],root)
            self.assertIn('/workspace/web/dist',agent)
            self.assertIn('--remount-ro',agent)
            self.assertNotIn('/workspace/web/dist',verifier)
