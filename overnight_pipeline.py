"""Role handoffs for a bounded preview iteration; execution adapters are injected."""
from __future__ import annotations
import asyncio
from overnight_worker import StageResult


def repair_batches(findings):
    """Keep complete acceptance repairable when several contracts fail together."""
    unique={}
    for finding in findings:
        if not isinstance(finding,dict):
            raise ValueError('invalid acceptance finding')
        scenario=finding.get('scenario');reproduction=finding.get('reproduction')
        if not isinstance(scenario,str) or not scenario.strip() or not isinstance(reproduction,str) or not reproduction.strip():
            raise ValueError('a finding needs a scenario and reproduction')
        unique.setdefault((scenario.strip(),reproduction.strip()),finding)
    ordered=[unique[key] for key in sorted(unique)]
    if len(ordered)<=1:
        return ordered
    reproduction='Resolve all of these independently reproduced acceptance failures together:\n\n'
    reproduction+='\n\n'.join(f"{index}. {item['scenario']}\n{item['reproduction']}" for index,item in enumerate(ordered,1))
    evidence='\n'.join(f"{item['scenario']}: {item.get('evidence','No evidence supplied')}" for item in ordered)
    return [{'scenario':f'Preview acceptance batch: {len(ordered)} failures','reproduction':reproduction,
             'evidence':evidence,'failures':ordered}]


class OvernightPipeline:
    def __init__(self, *, github, discover, repair, verify, deploy, prepare=None):
        self.prepare = prepare
        self.github = github
        self.discover_check = discover
        self.repair_issue = repair
        self.verify_candidate = verify
        self.deploy_candidate = deploy
        # Serialize reconciliation and issue creation within this coordinator.
        self.issue_lock = asyncio.Lock()

    async def discover(self, task):
        preparation = await self.prepare(task) if self.prepare is not None else None
        observed = await self.discover_check(task['payload'])
        findings = repair_batches(observed)
        successors = []
        issues = []
        async with self.issue_lock:
            for finding in findings:
                issue = await asyncio.to_thread(self.github.issue, finding)
                issues.append(issue['number'])
                successors.append(('repair',f"issue:{issue['number']}",{'issue':issue,'finding':finding,'repair_attempt':1,'run_id':task.get('run_id')}))
        clean_passes = 0
        if not findings:
            revision = task['payload'].get('revision')
            if not revision:
                raise ValueError('discovery requires a pinned preview revision')
            clean_passes = int(task['payload'].get('clean_passes',0))+1
            if clean_passes < 2:
                successors.append(('discover',f'clean:{revision}:{clean_passes}',
                                   {**task['payload'],'clean_passes':clean_passes}))
        result={'issues':issues,'clean':not findings,'clean_passes':clean_passes,
                'failed_scenarios':[item['scenario'] for item in observed]}
        if preparation is not None:
            result['acceptance_plan']=preparation
        return StageResult(result,tuple(successors))

    async def repair(self, task):
        candidate = await self.repair_issue(task['payload'])
        if not isinstance(candidate.get('pr'),int) or not candidate.get('head'):
            raise ValueError('repair adapter must return a pinned PR candidate')
        payload = {**task['payload'], 'candidate':candidate}
        return StageResult({'candidate':candidate},(('verify',f"pr:{candidate['pr']}:{candidate['head']}",payload),))

    async def verify(self, task):
        payload = task['payload']
        try:
            receipt = await self.verify_candidate(payload)
        except Exception as exc:
            attempt = int(payload.get('repair_attempt',1))
            if attempt >= 3:
                raise RuntimeError('independent verification retry limit reached') from exc
            retry = {**payload,'repair_attempt':attempt+1,'verification_failure':type(exc).__name__}
            return StageResult({'verification_failed':True,'attempt':attempt},
                (('repair',f"issue:{payload['issue']['number']}:attempt:{attempt+1}",retry),))
        if receipt.get('passed') is not True:
            raise RuntimeError('independent verification failed; issue remains open')
        candidate = payload['candidate']
        if receipt.get('head') != candidate['head'] or receipt.get('pr') != candidate['pr']:
            raise ValueError('verification receipt does not match repaired commit')
        await asyncio.to_thread(self.github.merge_verified,candidate['pr'],receipt)
        deployment = await self.deploy_candidate(payload,receipt)
        if deployment.get('healthy') is not True or not deployment.get('commit'):
            raise RuntimeError('preview deployment was not verified healthy')
        await asyncio.to_thread(self.github.close_verified_issue,payload['issue']['number'],deployment)
        return StageResult({'receipt':receipt,'deployment':deployment,'issue':payload['issue']},
                           (('discover',f"deployed:{deployment['commit']}",
                             {'revision':deployment['commit'],'clean_passes':0}),))

    def handlers(self):
        return {'discover':self.discover,'repair':self.repair,'verify':self.verify}
