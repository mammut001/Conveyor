"""Linux execution boundary: expose source and system runtimes, never host state."""
from __future__ import annotations
from pathlib import Path
import shutil
import sys


def sandbox_argv(argv, workspace):
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
          '--bind',str(workspace),'/workspace','--chdir','/workspace']
    if Path('/lib64').exists():
        args += ['--symlink','usr/lib64','/lib64']
    return [*args,'--',*argv]
