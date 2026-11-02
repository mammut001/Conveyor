"""Build frontend assets inside an isolated staging release before activation."""
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
from overnight_process import command


def build_frontend(release):
    root=Path(release);web=root/'web'
    if not (web/'package-lock.json').is_file():
        raise ValueError('preview frontend requires a pinned npm lockfile')
    async def build():
        await command(['npm','ci','--ignore-scripts','--no-audit','--no-fund'],cwd=web,
                      sandboxed=True,timeout=300,output_limit=1_000_000,retain_output=False)
        await command(['npm','run','build'],cwd=web,sandboxed=True,timeout=300,
                      output_limit=1_000_000,retain_output=False)
    try:
        asyncio.run(build())
        index=web/'dist/index.html'
        if not index.is_file():
            raise RuntimeError('frontend build did not produce an entry point')
        dist=web/'dist'
        for artifact in [dist,*dist.rglob('*')]:
            if artifact.is_symlink():
                raise RuntimeError('frontend artifacts must not be symlinks')
            artifact.chmod(0o755 if artifact.is_dir() else 0o644)
        evidence={'lock_sha256':hashlib.sha256((web/'package-lock.json').read_bytes()).hexdigest(),
                  'index_sha256':hashlib.sha256(index.read_bytes()).hexdigest()}
        (root/'preview-build.json').write_text(json.dumps(evidence))
        (root/'preview-build.json').chmod(0o644)
    finally:
        dependencies=web/'node_modules'
        if dependencies.exists():
            shutil.rmtree(dependencies)
