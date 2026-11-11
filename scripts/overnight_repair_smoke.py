#!/usr/bin/env python3
"""Opt-in real-model rehearsal against a synthetic local Git fixture.

No GitHub API calls, public issues/PRs or preview deployments are performed.
Uses the installed Pi CLI and explicit DEEPSEEK_API_KEY. Never runs in CI by default.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from conveyor.automation.overnight_process import Workspaces,IndependentVerifier
from conveyor.automation.overnight_repair import PiRepair


async def rehearse(report):
    if not os.environ.get('DEEPSEEK_API_KEY'):
        raise ValueError('explicit DEEPSEEK_API_KEY is required')
    plan={'objective':'Synthetic local Git repair and independent verification rehearsal',
          'acceptance':['Pi changes arithmetic subtraction to addition','candidate committed and pushed to local bare remote',
                        'independent detached checkout passes immutable arithmetic check'],
          'limits':{'agent_seconds':600,'repair_attempts':1},
          'external_writes':False,'production_or_preview_changes':False}
    report=Path(report);report.write_text(json.dumps({'plan':plan},indent=2))
    with tempfile.TemporaryDirectory(prefix='conveyor-repair-rehearsal-') as directory:
        root=Path(directory);source=root/'source';source.mkdir();remote=root/'remote.git'
        def git(*args):
            result=subprocess.run(['git',*args],capture_output=True,text=True,timeout=30)
            if result.returncode:
                raise RuntimeError('fixture Git operation failed')
            return result.stdout.strip()
        git('init','--bare',str(remote));git('-C',str(source),'init')
        (source/'web_console.py').write_text('def add(a, b):\n    return a - b\n')
        (source/'.gitignore').write_text('__pycache__/\n*.pyc\n')
        git('-C',str(source),'add','web_console.py','.gitignore')
        git('-C',str(source),'-c','user.name=Fixture','-c','user.email=fixture@example.test','commit','-m','synthetic arithmetic defect')
        git('-C',str(source),'branch','preview')
        git('-C',str(source),'remote','add','origin',str(remote))
        git('-C',str(source),'push','origin','preview')
        class LocalPublication:
            repo='fixture/Conveyor'
            def _json(self,*args):
                return []
            def command(self,args):
                assert args[:3]==['gh','pr','create']
                assert args[args.index('--base')+1]=='preview'
                return 'https://github.com/fixture/Conveyor/pull/1'
        spaces=Workspaces(source,root/'workspaces')
        repair=PiRepair(spaces,LocalPublication())
        payload={'issue':{'number':1},'finding':{'scenario':'Arithmetic sum contract',
                 'reproduction':'Calling web_console.add(2, 3) returns -1 instead of 5. Fix the implementation.',
                 'evidence':'Synthetic isolated fixture; expected add(-2, 3)==1 and add(2, 3)==5.'},'repair_attempt':1}
        candidate=await repair(payload)
        verify=IndependentVerifier(spaces,repo='fixture/Conveyor',checks=[[
            sys.executable,'-c','from web_console import add; assert add(2,3)==5; assert add(-2,3)==1; assert add(0,0)==0']])
        receipt=await verify({**payload,'candidate':candidate})
        pushed=git('--git-dir',str(remote),'rev-parse','refs/heads/codex/nightly-issue-1-attempt-1')
        assert pushed==candidate['head']
        result={'plan':plan,'candidate':candidate,'receipt':receipt,'local_remote_head':pushed,'passed':True}
        report.write_text(json.dumps(result,indent=2))
        print(json.dumps({'passed':True,'head':pushed,'verification':receipt['checks'],
                          'scope':'synthetic fixture; GitHub publication stubbed; no deployment'}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',required=True)
    options=parser.parse_args()
    asyncio.run(rehearse(options.report))
