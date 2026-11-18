import asyncio
import logging
from typing import Any, NamedTuple

class BackgroundWorkers(NamedTuple):
    usage_refresh: Any
    agent_retention_worker: Any
    delegation_worker: Any
    relay_consumer: Any

def start_background_workers(loop: asyncio.AbstractEventLoop, server: Any, settings: Any, runner: Any) -> BackgroundWorkers:
    logger = logging.getLogger("conveyor.web")
    
    from conveyor.automation import routines
    routines.start_routines_worker(loop, settings, runner)
    
    if getattr(settings, "memory_v2_enabled", False):
        from personal_tools.memory_v2 import start_purge_worker
        start_purge_worker(loop, settings)
    
    from conveyor.channels import approval_relay
    relay_consumer = approval_relay.start_relay_worker(loop, settings, channel="web")
    
    from agent_coordination.agent_delegation_worker import start as start_delegation
    delegation_worker = start_delegation(loop, settings)
    
    from conveyor.agents import agent_security
    agent_retention_worker = agent_security.start(loop, settings)
    
    from conveyor.agents import worker_usage
    usage_refresh = worker_usage.start_background_refresh(loop, settings)
    
    from conveyor.agents import supervisor_service
    if supervisor_service.is_enabled(settings):
        # Start at boot: startup recovery, task claiming, goal deadlines and quota
        # auto-resume must not wait for the first HTTP request after a restart.
        def _start_supervisor() -> None:
            supervisor_service.ensure_server_worker(server, settings, wake=True)
            logger.info("supervisor worker started at boot")

        loop.call_soon(_start_supervisor)
        
    def _resume_goals() -> None:
        try:
            from conveyor.web.web_chat import resume_goal_loops
            count = resume_goal_loops(settings, runner)
            if count:
                logger.info("resumed %d /goal run(s)", count)
        except Exception:
            logger.exception("could not resume /goal runs")

    loop.call_soon(_resume_goals)
    
    return BackgroundWorkers(
        usage_refresh=usage_refresh,
        agent_retention_worker=agent_retention_worker,
        delegation_worker=delegation_worker,
        relay_consumer=relay_consumer
    )
