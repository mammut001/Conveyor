"""GitHub delivery boundary for the preview-only overnight coordinator."""
from __future__ import annotations
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path


class GitHubDelivery:
    def __init__(self, repo, *, command=None):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+', repo):
            raise ValueError('invalid repository')
        self.repo = repo
        self.command = command or self._command

    @staticmethod
    def _command(args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=90)
        if result.returncode:
            # Do not persist stderr, which can include provider credentials.
            raise RuntimeError('GitHub command failed')
        return result.stdout

    def _json(self, *args):
        return json.loads(self.command(['gh', *args, '--repo', self.repo]))

    def issue(self, finding):
        """Reconcile by marker before creating. Caller serializes issue creation."""
        scenario = finding.get('scenario', '').strip()
        reproduction = finding.get('reproduction', '').strip()
        if not scenario or not reproduction:
            raise ValueError('finding needs a scenario and reproduction')
        identity = f'{self.repo}\n{scenario}\n{reproduction}'
        digest = hashlib.sha256(identity.encode()).hexdigest()
        marker = f'<!-- conveyor-nightly:{digest} -->'
        rows = self._json('issue', 'list', '--state', 'all', '--search', digest,
                          '--limit', '100', '--json', 'number,body,state,url')
        matches = [row for row in rows if marker in row.get('body', '')]
        if matches:
            return min(matches, key=lambda row:row['number'])
        body = (f'{marker}\n\n## Scenario\n{scenario}\n\n## Reproduction\n{reproduction}'
                f'\n\n## Evidence\n{finding.get("evidence", "No evidence supplied")}\n')
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'body.md'
            path.write_text(body)
            url = self.command(['gh','issue','create','--repo',self.repo,
                                '--title',f'[Nightly] {scenario[:160]}', '--body-file',str(path)]).strip()
        match = re.fullmatch(r'https://github.com/'+re.escape(self.repo)+r'/issues/(\d+)', url)
        if not match:
            raise RuntimeError('GitHub did not return an issue URL; reconcile before retry')
        return {'number':int(match.group(1)), 'url':url, 'state':'OPEN'}

    def candidate(self, number):
        if not isinstance(number,int) or number <= 0:
            raise ValueError('invalid PR number')
        return self._json('pr','view',str(number),'--json',
                          'number,state,isDraft,baseRefName,headRefOid,headRepository,headRepositoryOwner,statusCheckRollup,mergeable')

    def merge_verified(self, number, receipt):
        """Re-read the head and CI immediately before GitHub's SHA-guarded merge."""
        candidate = self.candidate(number)
        assert_mergeable(self.repo, candidate, receipt)
        self.command(['gh','pr','merge',str(number),'--repo',self.repo,'--merge',
                      '--match-head-commit',receipt['head']])
        return receipt['head']


def assert_mergeable(repo, candidate, receipt):
    owner, name = repo.split('/')
    if (candidate.get('baseRefName') != 'preview' or candidate.get('state') != 'OPEN'
            or candidate.get('isDraft') or candidate.get('mergeable') != 'MERGEABLE'):
        raise ValueError('only mergeable, open preview PRs can be delivered')
    if (candidate.get('headRepository',{}).get('name') != name
            or candidate.get('headRepositoryOwner',{}).get('login') != owner):
        raise ValueError('fork candidates are not supported')
    head = candidate.get('headRefOid', '')
    if (not re.fullmatch(r'[0-9a-f]{40}',head) or receipt.get('head') != head
            or receipt.get('repo') != repo or receipt.get('pr') != candidate.get('number')
            or receipt.get('passed') is not True or not receipt.get('checks')):
        raise ValueError('independent verification does not match candidate')
    checks = candidate.get('statusCheckRollup') or []
    if not checks:
        raise ValueError('candidate has no CI evidence')
    for check in checks:
        if check.get('__typename') == 'StatusContext':
            valid = check.get('state') == 'SUCCESS'
        else:
            valid = check.get('status') == 'COMPLETED' and check.get('conclusion') == 'SUCCESS'
        if not valid:
            raise ValueError('all candidate CI checks must succeed')
