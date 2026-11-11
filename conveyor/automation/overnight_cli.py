"""Explicit operator entry point for bounded Conveyor preview iteration."""
from __future__ import annotations
import argparse
import asyncio
import fcntl
import json
from pathlib import Path
from conveyor.automation.overnight_store import OvernightStore


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True)
    commands=parser.add_subparsers(dest='action',required=True)
    create=commands.add_parser('create')
    create.add_argument('--objective',required=True)
    create.add_argument('--revision',required=True)
    create.add_argument('--hours',type=float,default=1)
    create.add_argument('--max-tasks',type=int,default=12)
    create.add_argument('--config')
    for action in ('status','cancel','run'):
        sub=commands.add_parser(action);sub.add_argument('run_id')
        if action=='run':
            sub.add_argument('--config',required=True)
    args=parser.parse_args()
    store=OvernightStore(args.db)
    if args.action=='create':
        import re
        if not re.fullmatch(r'[0-9a-f]{40}',args.revision):
            parser.error('revision must be a full deployed preview commit')
        ident=store.create(args.objective,hours=args.hours,max_tasks=args.max_tasks)
        if args.config:
            store.bind_configuration(ident,json.loads(Path(args.config).read_text()))
        store.enqueue(ident,'discover','initial',{'revision':args.revision,'clean_passes':0})
        print(json.dumps(store.status(ident)))
    elif args.action=='status':
        print(json.dumps(store.reconcile(args.run_id)))
    elif args.action=='cancel':
        store.cancel(args.run_id);print(json.dumps(store.status(args.run_id)))
    else:
        status=store.reconcile(args.run_id)
        if status['state'] != 'running':
            print(json.dumps(status))
            return
        from conveyor.automation.overnight_checks import PreviewChecks
        from conveyor.automation.overnight_delivery import PreviewDelivery
        from conveyor.automation.overnight_github import GitHubDelivery
        from conveyor.automation.overnight_pipeline import OvernightPipeline
        from conveyor.automation.overnight_process import Workspaces,IndependentVerifier
        from conveyor.automation.overnight_repair import PiRepair
        from conveyor.automation.overnight_worker import serve
        from conveyor.automation.overnight_preflight import prepare_repository
        from conveyor.automation.overnight_probes import SourceProbes
        from dotenv import dotenv_values
        config=json.loads(Path(args.config).read_text())
        store.bind_configuration(args.run_id,config)
        preview_root=Path(config['preview_root']).resolve()
        if preview_root != Path('/srv/conveyor-preview'):
            parser.error('only the provisioned isolated preview root is supported')
        env=dotenv_values(preview_root/'runtime'/'preview.env')
        checks=PreviewChecks('http://127.0.0.1:18790',env['CONVEYOR_WEB_TOKEN'])
        github=GitHubDelivery(config['github_repo'])
        workspaces=Workspaces(config['repository'],config['workspaces'])
        base_probes=SourceProbes(workspaces,config.get('source_probes',[]))
        objective=None
        if 'objective_planning' in config and not isinstance(config['objective_planning'],bool):
            raise ValueError('objective_planning must be a boolean')
        if config.get('objective_planning',False):
            from conveyor.automation.overnight_objective import ObjectiveChecks,CombinedProbes
            objective=ObjectiveChecks(store,args.run_id,workspaces)
            probes=CombinedProbes(base_probes,objective)
        else:
            probes=base_probes
        verifier=IndependentVerifier(workspaces,repo=config['github_repo'],checks=base_probes.verification_commands()+config['verification_commands'])
        async def verify_and_wait(payload):
            receipt = await verifier(payload)
            if objective is not None:
                receipt['checks'].extend(await objective.verify(payload))
            return await github.wait_verified(payload['candidate']['pr'],receipt)
        async def pinned_discovery(payload):
            expected=payload.get('revision')
            if (preview_root/'current').resolve().name != expected:
                raise RuntimeError('preview revision changed before discovery')
            findings=await checks.discover(payload)
            findings.extend(await probes.discover(payload))
            if (preview_root/'current').resolve().name != expected:
                raise RuntimeError('preview revision changed during discovery')
            return findings
        pipeline=OvernightPipeline(github=github,discover=pinned_discovery,
            prepare=objective.prepare if objective is not None else None,
            repair=PiRepair(workspaces,github),verify=verify_and_wait,
            deploy=PreviewDelivery(config['repository'],preview_root,checks,source_probes=probes))
        # A single host coordinator owns external issue/PR reconciliation. Lock all
        # runs, not just this run, to prevent two coordinators deploying concurrently.
        with Path(str(Path(args.db).resolve())+'.coordinator.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            async def execute():
                await prepare_repository(workspaces.repo,github)
                await serve(store,args.run_id,pipeline.handlers())
            asyncio.run(execute())
        print(json.dumps(store.status(args.run_id)))


if __name__=='__main__':
    main()
