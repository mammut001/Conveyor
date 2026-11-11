import subprocess
import tempfile
import unittest
from pathlib import Path
from conveyor.automation.overnight_preview import PreviewReleases


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

    def test_frontend_is_prepared_before_switch_and_failed_build_preserves_current(self):
        with tempfile.TemporaryDirectory() as root:
            repo=Path(root)/'repo';repo.mkdir()
            def git(*args):
                return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL).decode().strip()
            git('init');git('config','user.email','test@example.com');git('config','user.name','test')
            (repo/'source.txt').write_text('new UI')
            git('add','source.txt');git('commit','-m','UI source');good=git('rev-parse','HEAD');git('branch','preview')
            preview=PreviewReleases(Path(root)/'preview');activated=[]
            def prepare(stage):
                (stage/'served.html').write_text((stage/'source.txt').read_text())
                self.assertFalse((preview.root/'current').exists())
            preview.deploy(repo,good,prepare=prepare,activate=lambda p:activated.append(p),healthy=lambda p:(p/'served.html').read_text()=='new UI')
            (repo/'source.txt').write_text('broken UI');git('add','source.txt');git('commit','-m','broken');bad=git('rev-parse','HEAD');git('branch','-f','preview',bad)
            def broken(stage):
                raise RuntimeError('build failed')
            with self.assertRaisesRegex(RuntimeError,'build failed'):
                preview.deploy(repo,bad,prepare=broken,activate=lambda p:activated.append(p),healthy=lambda p:True)
            self.assertEqual((preview.root/'current').resolve().name,good)
            self.assertEqual(len(activated),1)
            self.assertFalse((preview.root/'releases'/bad).exists())

    def test_interrupted_manifest_commit_recovers_previous_release_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            repo=Path(root)/'repo';repo.mkdir()
            def git(*args):
                return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL).decode().strip()
            git('init');git('config','user.email','test@example.com');git('config','user.name','test')
            (repo/'app.txt').write_text('old');git('add','.');git('commit','-m','old')
            old=git('rev-parse','HEAD');git('branch','preview')
            preview=PreviewReleases(Path(root)/'preview')
            preview.deploy(repo,old,activate=lambda p:None,healthy=lambda p:True)
            (repo/'app.txt').write_text('new');git('add','.');git('commit','-m','new')
            new=git('rev-parse','HEAD');git('branch','-f','preview',new)
            write=preview._write_manifest
            def interrupted(path, manifest):
                if path.name=='deployment.json':
                    raise OSError('process interrupted before manifest commit')
                write(path,manifest)
            preview._write_manifest=interrupted
            with self.assertRaises(OSError):
                preview.deploy(repo,new,activate=lambda p:None,healthy=lambda p:True)
            self.assertEqual((preview.root/'current').resolve().name,new)
            preview._write_manifest=write
            reactivated=[]
            result=preview.deploy(repo,new,activate=lambda p:reactivated.append(p.name),healthy=lambda p:True)
            self.assertEqual(reactivated,[new])
            self.assertEqual(result,{'commit':new,'previous':old})
            self.assertFalse((preview.root/'deployment-pending.json').exists())
            self.assertEqual(preview.deploy(repo,new,activate=lambda p:None,healthy=lambda p:True),result)

    def test_recovery_restarts_service_if_process_died_after_switch_before_activation(self):
        with tempfile.TemporaryDirectory() as root:
            repo=Path(root)/'repo';repo.mkdir()
            def git(*args):
                return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL).decode().strip()
            git('init');git('config','user.email','test@example.com');git('config','user.name','test')
            (repo/'app.txt').write_text('old');git('add','.');git('commit','-m','old')
            old=git('rev-parse','HEAD');git('branch','preview')
            preview=PreviewReleases(Path(root)/'preview');activated=[]
            preview.deploy(repo,old,activate=lambda p:activated.append(p.name),healthy=lambda p:True)
            (repo/'app.txt').write_text('new');git('add','.');git('commit','-m','new')
            new=git('rev-parse','HEAD');git('branch','-f','preview',new)
            switch=preview._switch
            def crash_after_switch(target):
                switch(target)
                raise SystemExit(23)
            preview._switch=crash_after_switch
            with self.assertRaises(SystemExit):
                preview.deploy(repo,new,activate=lambda p:activated.append(p.name),healthy=lambda p:True)
            preview._switch=switch
            self.assertEqual((preview.root/'current').resolve().name,new)
            self.assertEqual(activated,[old])
            # The old process can still answer generic health probes. Recovery
            # must activate the new release before trusting those probes.
            result=preview.deploy(repo,new,activate=lambda p:activated.append(p.name),healthy=lambda p:True)
            self.assertEqual(result,{'commit':new,'previous':old})
            self.assertEqual(activated,[old,new])

            (repo/'app.txt').write_text('bad');git('add','.');git('commit','-m','bad')
            bad=git('rev-parse','HEAD');git('branch','-f','preview',bad)
            preview._switch=crash_after_switch
            with self.assertRaises(SystemExit):
                preview.deploy(repo,bad,activate=lambda p:None,healthy=lambda p:True)
            preview._switch=switch
            def failed_activation(path):
                activated.append(path.name)
                if path.name==bad:
                    raise RuntimeError('activation failed')
            with self.assertRaisesRegex(RuntimeError,'activation failed'):
                preview.deploy(repo,bad,activate=failed_activation,healthy=lambda p:True)
            self.assertEqual((preview.root/'current').resolve().name,new)
            self.assertEqual(activated[-2:],[bad,new])
