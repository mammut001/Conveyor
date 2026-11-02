"""Check coordinator GitHub capabilities and configure repository-local credentials."""
from __future__ import annotations
import asyncio
import shlex
import shutil
from overnight_process import command


async def prepare_repository(repo, github):
    executable=shutil.which('gh')
    if not executable:
        raise RuntimeError('GitHub CLI is required')
    # This capability check catches old distributions without headRefOid support.
    await asyncio.to_thread(github._json,'pr','list','--limit','1','--json','number,headRefOid')
    remote=(await command(['git','remote','get-url','origin'],cwd=repo)).strip()
    permitted={f'https://github.com/{github.repo}',f'https://github.com/{github.repo}.git'}
    if remote not in permitted:
        raise ValueError('nightly origin must match the configured GitHub repository')
    helper='!'+shlex.quote(executable)+' auth git-credential'
    await command(['git','config','--local','credential.https://github.com.helper',helper],cwd=repo)
