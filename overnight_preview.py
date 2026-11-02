"""Atomic preview releases, serialized independently of production deployments."""
from __future__ import annotations
import fcntl
import json
import os
import re
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path


class PreviewReleases:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if str(self.root) == '/opt/conveyor' or Path('/opt/conveyor') in self.root.parents:
            raise ValueError('production directory is not a preview root')
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ('releases', 'runtime', 'state', 'tasks'):
            path = self.root/name
            if path.is_symlink():
                raise ValueError('preview directories must not be symlinks')
            path.mkdir(mode=0o700, exist_ok=True)

    @contextmanager
    def locked(self):
        with (self.root/'deploy.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _switch(self, target):
        link = self.root/('switch-'+uuid.uuid4().hex)
        link.symlink_to(target)
        os.replace(link, self.root/'current')

    def deploy(self, repo, sha, *, activate, healthy):
        """Callbacks restart/check preview only; a failed activation restores prior release.

        Caller must validate matching-head CI and independent verification before invoking.
        This layer verifies preview branch membership and archives tracked source only.
        """
        if not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise ValueError('a full immutable commit is required')
        repo = Path(repo).resolve()
        def git(*args, **kw):
            return subprocess.run(['git', '-c', f'safe.directory={repo}', '-C', str(repo), *args], check=True,
                                  capture_output=True, **kw)
        refs = git('for-each-ref','--format=%(refname)','refs/remotes/origin/preview').stdout.decode().strip()
        preview_ref = 'refs/remotes/origin/preview' if refs else 'refs/heads/preview'
        git('merge-base', '--is-ancestor', sha, preview_ref)
        with self.locked():
            current = self.root/'current'
            previous = current.resolve() if current.is_symlink() else None
            if current.exists() and not current.is_symlink():
                raise ValueError('current must be a managed symlink')
            if previous and previous.parent != self.root/'releases':
                raise ValueError('previous release outside managed preview')
            release = self.root/'releases'/sha
            if release.is_symlink():
                raise ValueError('release must not be a symlink')
            if not release.exists():
                # Git export excludes untracked .env and live databases.
                import io
                import tarfile
                archive = git('archive', '--format=tar', sha).stdout
                stage = self.root/'releases'/('stage-'+uuid.uuid4().hex)
                stage.mkdir(mode=0o700)
                try:
                    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                        for item in source.getmembers():
                            path = Path(item.name)
                            if path.is_absolute() or '..' in path.parts or not (item.isfile() or item.isdir()):
                                raise ValueError('unsafe release archive member')
                            if path.name == '.env' or path.name.startswith('.env.') and path.name not in {'.env.example', '.env.test.example'}:
                                raise ValueError('release contains environment secrets')
                        source.extractall(stage)
                    stage.chmod(0o755)
                    os.replace(stage, release)
                finally:
                    if stage.exists():
                        import shutil
                        shutil.rmtree(stage)
            # The deployment owner differs from the unprivileged preview user.
            release.chmod(0o755)
            self._switch(release)
            try:
                activate(release)
                if not healthy(release):
                    raise RuntimeError('preview health check failed')
            except BaseException:
                if previous:
                    self._switch(previous)
                    activate(previous)
                    if not healthy(previous):
                        raise RuntimeError('preview rollback health check failed')
                else:
                    current.unlink(missing_ok=True)
                raise
            manifest = {'commit':sha, 'previous':previous.name if previous else None}
            pending = self.root/('manifest-'+uuid.uuid4().hex)
            pending.write_text(json.dumps(manifest))
            os.replace(pending, self.root/'deployment.json')
            return manifest
