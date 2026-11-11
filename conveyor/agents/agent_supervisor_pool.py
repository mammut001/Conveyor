"""Supervisor "pool" executor: rotates roles across the 执行者 pool (workers.py).

The pool only chooses *which* worker runs. The actual run is delegated to the
existing Supervisor executor for that worker, so read-only and sandbox rules
stay where they already are. Workers without a Supervisor adapter (grok,
codex) are never used here.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any, Callable

from conveyor.agents import agent_supervisor as ags
from conveyor.agents import workers
from conveyor.agents.agent_supervisor import ExecutionResult, RoleConfig

logger = logging.getLogger("conveyor.agent_supervisor_pool")

POOL_TO_SUPERVISOR = {
    "claude-code": "claude",
    "agy": "antigravity",
    "cmd": "command-code",
    "pi": "pi",
}
NO_WORKER_REASON = "执行者池里没有可用的 Claude Code / Antigravity / Command Code / Pi（内置）"
ALL_QUOTA_PREFIX = "执行者池里的执行者额度都用完了："


def _default_factory(settings: Any, name: str) -> Any:
    return ags.get_executor(settings, name)


def _identity(worktree_path: str) -> dict[str, str] | None:
    try:
        return ags.target_identity(worktree_path)
    except Exception:
        return None


def _worker_model(worker: dict[str, Any]) -> str | None:
    """The worker's own saved model only; Supervisor role models never reach pool workers.

    Pi chooses its provider and model via resolve_pi_provider (CONVEYOR_PI_* >
    saved 模型设置 > Pi default), whose bridged models.json only registers the
    saved 模型设置 model, so a pool worker model is never passed to Pi.
    """
    if worker.get("id") == "pi":
        return None
    value = worker.get("model")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _over_threshold(worker: dict[str, Any], now: float) -> bool:
    if "usage" in worker:
        threshold = worker.get("threshold_percent", workers.DEFAULT_THRESHOLD_PERCENT)
        return workers.usage_over_threshold(worker["usage"], threshold, now)
    return bool(worker.get("over_threshold"))


class PoolExecutor:
    name = "pool"

    def __init__(
        self,
        settings: Any,
        *,
        executor_factory: Callable[[Any, str], Any] | None = None,
        workers_provider: Callable[[Any], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.settings = settings
        self._factory = executor_factory or _default_factory
        self._workers_provider = workers_provider or (
            lambda s: workers.get_workers_payload(s)["workers"]
        )

    def _pool_workers(self, mode: str) -> list[dict[str, Any]]:
        try:
            workers_list = self._workers_provider(self.settings)
        except Exception as exc:
            logger.warning("pool: failed to read workers: %s", exc)
            return []
        result = []
        for worker in workers_list:
            wid = worker.get("id")
            if wid not in POOL_TO_SUPERVISOR or not workers._is_eligible(worker):
                continue
            if not workers.mode_eligibility(self.settings, wid, mode)[0]:
                continue
            result.append(worker)
        return result

    def available(self) -> tuple[bool, str]:
        try:
            if self._pool_workers("run"):
                return True, "ok"
        except Exception as exc:
            logger.warning("pool: availability check failed: %s", exc)
        return False, NO_WORKER_REASON

    def candidates(self, role: RoleConfig) -> list[dict[str, Any]]:
        mode = "fix" if "write" in role.tools else "run"
        usable = [
            worker for worker in self._pool_workers(mode)
            if self._factory(self.settings, POOL_TO_SUPERVISOR[worker["id"]]).available()[0]
        ]
        now = time.time()
        # Stable sort: workers under their usage threshold keep the owner's order first.
        return sorted(usable, key=lambda w: _over_threshold(w, now))

    async def run(self, *, role: RoleConfig, prompt: str, worktree_path: str,
                  timeout_seconds: int, task_id: str = "",
                  context: Any = None) -> ExecutionResult:
        candidates = self.candidates(role)
        if not candidates:
            now = ags._utc_now()
            return ExecutionResult(success=False, error=NO_WORKER_REASON,
                                   started_at=now, finished_at=now)

        write = "write" in role.tools
        attempts: list[dict[str, Any]] = []
        result: ExecutionResult | None = None
        quota_exhausted = False
        for idx, worker in enumerate(candidates):
            wid = worker["id"]
            label = worker.get("label", wid)
            supervisor_name = POOL_TO_SUPERVISOR[wid]
            before = _identity(worktree_path)
            delegate = self._factory(self.settings, supervisor_name)
            attempt_role = dataclasses.replace(
                role, executor=supervisor_name, model=_worker_model(worker),
            )
            result = await delegate.run(
                role=attempt_role, prompt=prompt, worktree_path=worktree_path,
                timeout_seconds=timeout_seconds, task_id=task_id, context=context,
            )
            attempts.append({
                "worker_id": wid,
                "label": label,
                "executor": supervisor_name,
                "success": bool(result.success),
                "error": str(result.error or "")[:300],
                "execution_id": result.execution_id,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
            })
            quota_exhausted = False
            if result.success or result.cancelled:
                break
            failure = workers.classify_failure(f"{result.error}\n{(result.output or '')[-4000:]}")
            if failure is None or failure[0] != "quota":
                break  # a real task failure: do not rotate
            # Writers that already changed the worktree must not be re-run elsewhere.
            if write and (before is None or _identity(worktree_path) != before):
                break
            quota_exhausted = True
            try:
                workers.mark_cooling(self.settings, wid, failure[1], reason="supervisor")
                if idx + 1 < len(candidates):
                    workers.record_switch(
                        self.settings, wid, candidates[idx + 1]["id"], "quota_error",
                        f"{label} 额度已用完，Supervisor 换下一个执行者",
                    )
            except Exception as exc:
                logger.warning("pool: failed to cool/record worker %s: %s", wid, exc)

        assert result is not None
        if quota_exhausted and not result.success:
            result.error = f"{ALL_QUOTA_PREFIX}{result.error}"
        last = attempts[-1]
        result.evidence = {
            **(result.evidence or {}),
            "pool": {"worker_id": last["worker_id"], "label": last["label"], "attempts": attempts},
        }
        return result

    async def cancel(self, execution_id: str) -> bool:
        return ags.cancel_execution(self.settings, execution_id)
