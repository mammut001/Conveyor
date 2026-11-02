"""Bounded subprocesses and detached workspaces for nightly repair/verification."""
from __future__ import annotations
import asyncio
import os
import re
import signal
from contextlib import asynccontextmanager
from pathlib import Path
import uuid


class CommandFailure(RuntimeError):
    pass


def child_environment(*, provider=False):
    allowed = {'PATH','LANG','LC_ALL','SSL_CERT_FILE','SSL_CERT_DIR','SYSTEMROOT'}
    if provider:
        allowed |= {'DEEPSEEK_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY'}
    result = {key:value for key,value in os.environ.items() if key in allowed}
    result.update(PI_OFFLINE='1',PI_TELEMETRY='0',GIT_TERMINAL_PROMPT='0')
    return result


async def command(argv, *, cwd, timeout=120, stdin=None, provider=False, output_limit=100_000, sandboxed=False):
    if not argv or not 0 < timeout <= 1800:
        raise ValueError('invalid subprocess limits')
    if sandboxed:
        from overnight_sandbox import sandbox_argv
        argv = sandbox_argv(argv,cwd)
    process = await asyncio.create_subprocess_exec(
        *argv,cwd=cwd,env=child_environment(provider=provider),
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,start_new_session=True)
    captured = bytearray()
    async def read():
        while True:
            chunk = await process.stdout.read(8192)
            if not chunk:
                return
            if len(captured)+len(chunk) > output_limit:
                raise CommandFailure('subprocess output limit exceeded')
            captured.extend(chunk)
    async def run():
        if stdin is not None:
            process.stdin.write(stdin.encode())
            await process.stdin.drain()
            process.stdin.close()
        await read()
        return await process.wait()
    try:
        code = await asyncio.wait_for(run(),timeout)
        if code:
            # Avoid including generated output or credentials in exception messages.
            raise CommandFailure(f'subprocess exited with status {code}')
        return captured.decode('utf-8',errors='replace')
    finally:
        # Kill the whole process group even when the leader exited: grandchildren
        # must not outlive a cancelled verification or expired task lease.
        try:
            os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


class Workspaces:
    def __init__(self, repo, root):
        self.repo=Path(repo).resolve();self.root=Path(root).resolve()
        if self.repo == Path('/opt/conveyor') or Path('/opt/conveyor') in self.root.parents:
            raise ValueError('nightly execution requires an independent repository')
        self.root.mkdir(parents=True,exist_ok=True)

    @asynccontextmanager
    async def detached(self, sha):
        if not re.fullmatch(r'[0-9a-f]{40}',sha):
            raise ValueError('workspace requires pinned full commit')
        path=self.root/uuid.uuid4().hex
        await command(['git','worktree','add','--detach',str(path),sha],cwd=self.repo)
        try:
            yield path
        finally:
            await command(['git','worktree','remove','--force',str(path)],cwd=self.repo)


class IndependentVerifier:
    def __init__(self, workspaces, *, repo, checks):
        # Checks are operator-controlled configuration, never issue/model commands.
        if not checks or any(not argv for argv in checks):
            raise ValueError('independent checks are required')
        self.workspaces=workspaces;self.repo=repo;self.checks=checks

    async def __call__(self, payload):
        candidate=payload['candidate'];completed=[]
        async with self.workspaces.detached(candidate['head']) as path:
            for index,argv in enumerate(self.checks):
                await command(argv,cwd=path,timeout=600)
                completed.append(f'configured-check-{index+1}')
        return {'repo':self.repo,'pr':candidate['pr'],'head':candidate['head'],
                'passed':True,'checks':completed}
