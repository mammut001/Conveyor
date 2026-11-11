"""Linux execution boundary: expose source and system runtimes, never host state."""
from __future__ import annotations
from pathlib import Path
import shutil
import sys


def sandbox_argv(argv, workspace, *, source_only=False, read_only=False, network=True, output_root=None):
    if sys.platform != 'linux' or not shutil.which('bwrap'):
        raise RuntimeError('nightly agent execution requires Linux bubblewrap')
    workspace=Path(workspace).resolve()
    if not workspace.is_dir() or workspace == Path('/') or workspace == Path('/opt/conveyor'):
        raise ValueError('invalid isolated workspace')
    # Do not expose /home, /opt, /srv or the coordinator repository. A worktree's
    # .git pointer cannot be followed because the host admin repo is not mounted.
    args=['bwrap','--die-with-parent','--new-session','--unshare-pid','--unshare-ipc',
          '--unshare-uts','--cap-drop','ALL','--ro-bind','/usr','/usr',
          '--symlink','usr/bin','/bin','--symlink','usr/sbin','/sbin',
          '--symlink','usr/lib','/lib','--dir','/etc',
          '--ro-bind','/etc/resolv.conf','/etc/resolv.conf',
          '--ro-bind','/etc/ssl','/etc/ssl','--dev','/dev','--proc','/proc',
          '--tmpfs','/tmp','--dir','/tmp/home','--setenv','HOME','/tmp/home',
          '--setenv','PI_CODING_AGENT_DIR','/tmp/home/pi',
          '--ro-bind' if read_only or output_root else '--bind',str(workspace),'/workspace','--chdir','/workspace']
    if not network:
        args += ['--unshare-net']
    if output_root is not None:
        output_root=Path(output_root).resolve()
        if not output_root.is_dir() or output_root == Path('/'):
            raise ValueError('invalid discovery output directory')
        args += ['--bind',str(output_root),'/output']
    if source_only:
        # Minified bundles are generated artifacts, not repair input. Hiding them
        # prevents expensive searches over a single enormous line, while leaving
        # tracked host files intact for the coordinator's diff validation.
        for generated in ('web/dist','web/node_modules','node_modules'):
            if (workspace/generated).is_dir():
                target='/workspace/'+generated
                args += ['--tmpfs',target,'--remount-ro',target]
    if Path('/lib64').exists():
        args += ['--symlink','usr/lib64','/lib64']
    venv = Path('/srv/conveyor-preview/venv')
    if venv.is_dir():
        args += ['--ro-bind',str(venv),str(venv)]
    return [*args,'--',*argv]
