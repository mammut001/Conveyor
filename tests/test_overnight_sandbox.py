import tempfile
import unittest
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
