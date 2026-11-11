#!/usr/bin/env python3
"""Reject deployment while installed services still reference removed launchers."""
from pathlib import Path
import re
import shutil
import subprocess
import sys

ENTRYPOINTS = ('bot', 'feishu_bot', 'web_console', 'web_console_takeover',
               'desktop_agent', 'desktop_agent_server', 'desktop_chat_window',
               'agent_desktops', 'handoff_sidecar')
UNITS = ('conveyor-telegram-bot', 'conveyor-feishu-bot', 'conveyor-web',
         'conveyor-desktop-agent', 'conveyor-agent-desktops', 'conveyor-handoff',
         'conveyor-desktop-chat', 'conveyor-vps-computer')


def obsolete_command(command: str, repo: Path) -> bool:
    return any(re.search(re.escape(str(repo / (name + '.py'))) + r'(?:[\s;}]|$)', command)
               for name in ENTRYPOINTS)


def deployment_root(source: Path) -> Path:
    # A detached deployment candidate shares the live checkout's .git directory.
    value = subprocess.check_output(['git', '-C', str(source), 'rev-parse',
                                     '--git-common-dir'], text=True).strip()
    common = Path(value)
    if not common.is_absolute():
        common = source / common
    return common.resolve().parent


def main() -> int:
    if not shutil.which('systemctl'):
        return 0
    repo = (Path(sys.argv[1]).resolve() if len(sys.argv) == 2
            else deployment_root(Path(__file__).resolve().parents[1]))
    failures = []
    for unit in UNITS:
        result = subprocess.run(['systemctl', 'show', unit, '-p', 'ExecStart', '--value'],
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and obsolete_command(result.stdout, repo):
            failures.append(unit)
    if failures:
        print('Deployment stopped: installed services reference removed root launchers: '
              + ', '.join(failures), file=sys.stderr)
        print('Follow docs/entrypoint-migration.md before updating this installation.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
