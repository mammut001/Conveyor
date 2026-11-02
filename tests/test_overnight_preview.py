import subprocess
import tempfile
import unittest
from pathlib import Path
from overnight_preview import PreviewReleases


class PreviewTests(unittest.TestCase):
    def test_deployment_exports_only_committed_source_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as root:
            repo=Path(root)/'repo';repo.mkdir()
            def git(*args):
                return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL).decode().strip()
            git('init');git('config','user.email','test@example.com');git('config','user.name','test')
            (repo/'app.txt').write_text('working')
            git('add','app.txt');git('commit','-m','working');good=git('rev-parse','HEAD')
            git('branch','preview')
            (repo/'.env').write_text('production secret')
            preview=PreviewReleases(Path(root)/'preview')
            activated=[]
            preview.deploy(repo,good,activate=lambda p:activated.append(p.name),healthy=lambda p:True)
            self.assertFalse((preview.root/'current'/'.env').exists())
            (repo/'app.txt').write_text('broken');git('add','app.txt');git('commit','-m','broken')
            bad=git('rev-parse','HEAD');git('branch','-f','preview',bad)
            with self.assertRaisesRegex(RuntimeError,'health check failed'):
                preview.deploy(repo,bad,activate=lambda p:activated.append(p.name),healthy=lambda p:p.name==good)
            self.assertEqual((preview.root/'current').resolve().name,good)
            self.assertEqual(activated,[good,bad,good])

    def test_production_root_rejected(self):
        with self.assertRaises(ValueError):
            PreviewReleases('/opt/conveyor/preview')
