"""Deploy the preview integration commit and prove health before closing issues."""
from __future__ import annotations
import asyncio
import re
import subprocess
from conveyor.automation.overnight_preview import PreviewReleases
from conveyor.automation.overnight_build import build_frontend


class PreviewDelivery:
    def __init__(self, repo, root, checks, *, restart=None, source_probes=None):
        self.repo=repo
        self.releases=PreviewReleases(root)
        self.checks=checks
        self.restart=restart or self._restart
        self.source_probes=source_probes

    @staticmethod
    def _restart(_release):
        from conveyor.automation.overnight_guard import check_guard
        check_guard()
        result=subprocess.run(['sudo','-n','systemctl','restart','conveyor-preview.service'],
                              capture_output=True,timeout=45)
        if result.returncode:
            raise RuntimeError('preview restart failed')

    def _deploy(self, payload, receipt):
        head=payload['candidate']['head']
        if receipt.get('head') != head or receipt.get('passed') is not True:
            raise ValueError('deployment needs matching independent verification')
        def git(*args):
            from conveyor.automation.overnight_guard import check_guard
            check_guard()
            result=subprocess.run(['git','-c',f'safe.directory={self.repo}','-C',str(self.repo),*args],capture_output=True,text=True,timeout=90)
            if result.returncode:
                raise RuntimeError('preview repository reconciliation failed')
            return result.stdout.strip()
        git('fetch','origin','preview')
        history=git('log','--first-parent','--merges','--format=%H %P','refs/remotes/origin/preview')
        commit=verified_merge_commit(history,head)
        if not re.fullmatch(r'[0-9a-f]{40}',commit):
            raise ValueError('invalid preview integration commit')
        git('merge-base','--is-ancestor',head,commit)
        def healthy(_release):
            # systemd may report start before the listener is ready.
            import time
            for _ in range(10):
                findings=asyncio.run(self.checks.discover({}))
                if not findings:
                    if self.source_probes is not None and _release.name==commit:
                        return not asyncio.run(self.source_probes.discover({'revision':commit}))
                    return True
                time.sleep(.5)
            return False
        manifest=self.releases.deploy(self.repo,commit,activate=self.restart,healthy=healthy,prepare=build_frontend)
        return {**manifest,'healthy':True,'verified_head':head}

    async def __call__(self,payload,receipt):
        return await asyncio.to_thread(self._deploy,payload,receipt)


def verified_merge_commit(history, head):
    """Pin the actual two-parent merge of this verified head, not a newer tip."""
    for line in history.splitlines():
        fields=line.split()
        if len(fields)==3 and fields[2]==head and all(re.fullmatch(r'[0-9a-f]{40}',field) for field in fields):
            return fields[0]
    raise ValueError('verified candidate has no matching preview merge commit')
