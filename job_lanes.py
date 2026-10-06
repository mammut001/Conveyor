"""job_lanes.py — which Codex jobs may run side by side.

A lane is a queue of jobs that must not overlap. Everything is in the
``default`` lane unless ``CONVEYOR_AGENT_PARALLEL_JOBS`` is above 1; then each
agent that has its own project folder gets a lane of its own, because its jobs
touch a different repository and cannot collide with anyone else's.

Each lane has its own runner object (its own "current job" and "last job"),
so commands sent in an agent's conversation — /status, /cancel, /diff,
/apply — act on that agent's jobs.
"""
from __future__ import annotations

import threading
from typing import Any

DEFAULT_LANE = "default"
_lock = threading.Lock()


def parallel_limit(settings: Any) -> int:
    try:
        return max(1, min(4, int(getattr(settings, "agent_parallel_jobs", 1) or 1)))
    except (TypeError, ValueError):
        return 1


def lane_for_chat(settings: Any, channel: str, chat_id: str) -> str:
    """Lane of a conversation's jobs."""
    if parallel_limit(settings) <= 1:
        return DEFAULT_LANE
    try:
        import agents

        agent = agents.agent_for_chat(settings, channel, chat_id)
    except Exception:
        return DEFAULT_LANE
    if agent and not agent.get("is_default") and agent.get("workspace_path"):
        return str(agent["id"])
    return DEFAULT_LANE


def _own(obj: Any, name: str) -> Any:
    """An attribute the object itself carries. Plain getattr would be fooled
    by test doubles that answer every attribute with another double."""
    try:
        return vars(obj).get(name)
    except TypeError:
        return None


def _base_of(runner: Any) -> Any:
    return _own(runner, "_lane_base") or runner


def lane_runners(runner: Any) -> dict[str, Any]:
    """Every runner that shares this base runner's lanes, keyed by lane."""
    base = _base_of(runner)
    registry = _own(base, "_lane_runners")
    if not isinstance(registry, dict):
        registry = {DEFAULT_LANE: base}
        try:
            base._lane_runners = registry
        except AttributeError:
            return {DEFAULT_LANE: base}
    return registry


def runner_for_lane(runner: Any, lane: str) -> Any:
    """The runner that owns `lane`, created on first use.

    A runner that cannot be cloned (a test double, another backend) keeps
    everything in its own lane: jobs then simply queue behind each other.
    """
    if not lane or lane == DEFAULT_LANE:
        return _base_of(runner)
    with _lock:
        registry = lane_runners(runner)
        existing = registry.get(lane)
        if existing is not None:
            return existing
        base = registry[DEFAULT_LANE]
        try:
            from runner import CodexRunner

            if type(base) is not CodexRunner:
                return base
            clone = CodexRunner(base.settings)
            clone.lane = lane
            clone._lane_base = base
        except Exception:
            return base
        registry[lane] = clone
        return clone


def runner_for_chat(runner: Any, settings: Any, channel: str, chat_id: str) -> Any:
    return runner_for_lane(runner, lane_for_chat(settings, channel, chat_id))


def runner_of_job(runner: Any, queue_job_id: str) -> Any | None:
    """The runner currently executing a queue job, if it runs in this process."""
    for candidate in lane_runners(runner).values():
        current = getattr(candidate, "current_job", None)
        if current is not None and str(getattr(current, "external_id", "") or "") == str(queue_job_id):
            return candidate
    return None
