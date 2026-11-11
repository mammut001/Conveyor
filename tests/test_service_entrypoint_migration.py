from pathlib import Path
import tempfile
import unittest
from scripts.check_service_entrypoints import obsolete_command, deployment_root
import subprocess


class EntrypointMigrationTests(unittest.TestCase):
    def test_old_command_requires_migration_but_other_checkouts_do_not(self):
        repo = Path('/opt/conveyor')
        self.assertTrue(obsolete_command('{ argv[]=/opt/conveyor/.venv/bin/python /opt/conveyor/bot.py ; }', repo))
        self.assertFalse(obsolete_command('/opt/conveyor/.venv/bin/python -m conveyor.entrypoints.bot', repo))
        self.assertFalse(obsolete_command('python /srv/other/bot.py', repo))

    def test_rsync_preflight_runs_before_any_source_copy(self):
        source = (Path(__file__).resolve().parents[1] / 'scripts' / 'deploy.sh').read_text()
        self.assertLess(source.index('python3 - ${quoted_remote_dir}'), source.index('rsync -az'))
        self.assertIn('die "Service entrypoint migration is required before source sync"', source)

    def test_candidate_checks_the_live_checkout_location(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / 'live'
            root.mkdir()
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'initial'], check=True)
            candidate = Path(raw).resolve() / 'candidate'
            subprocess.run(['git', '-C', str(root), 'worktree', 'add', '--detach', str(candidate)], check=True, capture_output=True)
            self.assertEqual(deployment_root(candidate), root)
