"""Provider-health guard around each Codex provider attempt."""
from __future__ import annotations

from typing import Any

from provider_config import get_provider_config
from provider_health import get_provider_health
from runner.operators.run import _run_codex_attempt as _base_run_codex_attempt


def _snapshot(settings: Any) -> tuple[dict[str, Any], Any]:
    config = get_provider_config(settings)
    store = get_provider_health(settings)
    return config, store


def _label(config: dict[str, Any]) -> str:
    return str(config.get("provider_name") or config.get("provider_id") or "Provider")


async def guarded_run_codex_attempt(self: Any, job: Any, on_progress: Any) -> None:
    """Run one attempt unless the active provider is in a cooling-off window.

    A provider-specific rate-limit/auth/network failure opens the circuit using
    the shared SQLite health store. When the circuit opens we deliberately
    replace the raw provider error/last-event text with a provider-neutral
    fail-fast message so the existing runner does not enter its 300/900/1800s
    rate-limit retry sleep and monopolize the single-concurrency queue.
    """
    config, store = _snapshot(self.settings)
    provider_id = str(config.get("provider_id") or "unknown")
    revision = str(config.get("config_revision") or "")
    allowed, health = store.can_run(provider_id, revision)
    if not allowed:
        retry = int(health.get("retry_after_seconds") or 0)
        job.return_code = 75
        job.error = f"{_label(config)} is temporarily unavailable; retry in about {retry}s."
        job.last_event = "provider circuit open"
        await on_progress(job.error)
        return

    await _base_run_codex_attempt(self, job, on_progress)

    if job.return_code == 0 and not job.error:
        store.record_success(provider_id, revision)
        return

    health = store.record_failure(provider_id, revision, f"{job.error}\n{job.last_event}")
    if health.get("circuit_open"):
        retry = int(health.get("retry_after_seconds") or 0)
        kind = str(health.get("last_error_kind") or "unavailable")
        job.error = f"{_label(config)} is temporarily unavailable; retry in about {retry}s."
        job.last_event = f"provider unavailable ({kind})"
        await on_progress(job.error)
