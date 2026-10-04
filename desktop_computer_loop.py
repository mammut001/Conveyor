"""desktop_computer_loop.py — the Codex action loop engine (P5.6).

``run_computer_loop`` is the single shared core used by both the real
``/computer_task`` path and the smoke suite. It is deliberately
backend- and planner-agnostic so it can be exercised end-to-end with a
``ScriptedPlanner`` + ``FakeComputerBackend`` and zero network/Cua.

Safety enforced here (see docs/desktop_security.md):
- The task must be created already; the caller gates on enabled +
  direct mode. The loop additionally re-checks the task is still
  running each step (so ``/computer_stop`` takes effect).
- Every action is allow-listed (``is_action_allowed``) and scanned for
  blocked keywords (``contains_blocked_keyword``). Either hit stops
  the task and records why.
- Human takeover owns the GUI exclusively: while a takeover session is
  open the loop performs no observe/click/type/hotkey action and waits
  for the operator to complete or cancel the handoff.
- ``max_steps`` / ``max_seconds`` hard caps; human-owned pause time does
  not consume the automation wall-clock budget.
- Typed text / hotkey payloads are redacted before they enter the
  trajectory (``redact_computer_action``).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from config import Settings
from desktop_computer_planner import (
    infer_target_app,
    maybe_observe_only_action,
    maybe_simple_digit_action,
    resolve_clicked_label,
)
from desktop_computer_requests import (
    append_trajectory,
    cancel_computer_task,
    cancel_pending_computer_step,
    contains_blocked_keyword,
    create_computer_step,
    create_computer_task,
    get_computer_task,
    is_action_allowed,
    normalize_action,
    redact_computer_action,
    set_task_status,
)
from desktop_cua import CuaDriver, FakeCuaTransport
from human_takeover import HumanTakeoverStore


def _with_observed_target(action: dict, observation: dict) -> dict:
    """Fill pid for type/hotkey/scroll from the window just observed.

    The Linux driver rejects those actions when pid is missing. The
    observation pid is the front window the planner was just shown.
    """
    if not isinstance(action, dict) or not isinstance(observation, dict):
        return action
    if action.get("action") not in {"type", "hotkey", "scroll"}:
        return action
    if action.get("pid") is not None:
        return action
    pid = observation.get("pid")
    if pid is None:
        return action
    bound = dict(action)
    bound["pid"] = pid
    if bound.get("window_id") is None and observation.get("window_id") is not None:
        bound["window_id"] = observation["window_id"]
    return bound


class ComputerBackendError(Exception):
    """Raised when a step cannot be executed / observed."""


class HttpComputerBackend:
    """Wait for the Mac agent to claim + complete the step on the VPS store.

    The Mac polls the control plane (which writes the same store), so the
    VPS executor only needs to poll the local store for completion.
    """

    def __init__(self, settings: Settings, *, poll_interval: float = 2.0) -> None:
        self.settings = settings
        self.poll_interval = poll_interval

    async def execute_step(self, settings: Settings, task_id: str, step_id: str, action: dict) -> dict:
        deadline = time.monotonic() + settings.conveyor_computer_max_seconds
        takeover_store = HumanTakeoverStore(settings)
        while time.monotonic() < deadline:
            task = get_computer_task(settings, task_id)
            if not isinstance(task, dict):
                raise ComputerBackendError("task_missing")
            step = task.get("steps", {}).get(step_id)
            if not isinstance(step, dict):
                raise ComputerBackendError("step_missing")
            status = step.get("status")
            if status == "pending" and takeover_store.current() is not None:
                cancel_pending_computer_step(
                    settings,
                    task_id,
                    step_id,
                    reason="human_takeover_active",
                )
                raise ComputerBackendError("human_takeover_active")
            if status == "completed":
                result = step.get("result") or {}
                if not isinstance(result, dict):
                    result = {}
                return result
            if status == "failed":
                # A bad click or a type that missed its window is feedback
                # for the planner, not the end of the task.
                return {
                    "result_ok": False,
                    "error": str(step.get("error") or "step_failed")[:128],
                }
            if status in ("expired", "cancelled"):
                if status == "cancelled" and (
                    step.get("error") == "human_takeover_active"
                    or takeover_store.current() is not None
                ):
                    raise ComputerBackendError("human_takeover_active")
                raise ComputerBackendError(f"step_{status}")
            await asyncio.sleep(self.poll_interval)
        raise ComputerBackendError("step_timeout")


class FakeComputerBackend:
    """Deterministic backend: run the action through a fake Cua driver and
    immediately complete the step in the store. No network, no real Cua."""

    def __init__(self, settings: Settings, *, node_id: str = "") -> None:
        self.settings = settings
        self.node_id = node_id or (settings.conveyor_desktop_node_id or "macbook-payton")
        self.driver = CuaDriver(settings, node_id=self.node_id, transport=FakeCuaTransport())

    async def execute_step(self, settings: Settings, task_id: str, step_id: str, action: dict) -> dict:
        result = self.driver.execute(action)
        from desktop_computer_requests import complete_computer_step
        complete_computer_step(settings, step_id, self.node_id, result)
        return result


def build_backend(settings: Settings) -> Any:
    if settings.conveyor_computer_backend == "fake":
        return FakeComputerBackend(settings)
    return HttpComputerBackend(settings)


async def run_computer_loop(
    settings: Settings,
    goal: str,
    *,
    planner: Any,
    backend: Any,
    operator_id: str = "",
    chat_id: str = "",
    channel: str = "",
    max_steps: int,
    max_seconds: int,
    direct_mode: bool,
    stop_check: Callable[[], bool] | None = None,
    task_id: str | None = None,
) -> dict:
    """Run the action loop. Returns a summary dict.

    Pre-conditions (caller's job): CONVEYOR_COMPUTER_USE_ENABLED and
    direct mode must already be satisfied. This function focuses on the
    loop + safety enforcement.
    """
    if task_id is None:
        created = create_computer_task(
            settings,
            goal,
            direct_mode=direct_mode,
            max_steps=max_steps,
            max_seconds=max_seconds,
            operator_id=operator_id,
            chat_id=chat_id,
            channel=channel,
        )
        if not created.get("ok"):
            return {"ok": False, "error": created.get("error"), "message": created.get("message")}
        task_id = created["task_id"]
    else:
        existing = get_computer_task(settings, task_id)
        if not isinstance(existing, dict):
            return {"ok": False, "error": "task_not_running", "task_id": task_id}
        if existing.get("status") != "running":
            # A background executor can start after /computer_stop has
            # already finalized the pre-created task. Preserve that terminal
            # state so operator_stop is not reported as task_not_running.
            return {
                "ok": True,
                "task_id": task_id,
                "status": existing.get("status"),
                "summary": existing.get("summary"),
                "blocked_reason": existing.get("blocked_reason"),
                "steps_used": existing.get("step_seq", 0),
                "trajectory_len": len(existing.get("trajectory", []) or []),
            }
    start = time.monotonic()
    pause_started: float | None = None
    steps_used = 0
    false_done = 0
    observation: dict[str, Any] = {"initial": True}
    trajectory: list[dict] = []
    followup_observe = False
    takeover_store = HumanTakeoverStore(settings)

    try:
        while steps_used < max_steps:
            # Operator stop or external cancel remains authoritative even
            # while a human owns the GUI.
            if stop_check is not None and stop_check():
                set_task_status(settings, task_id, "stopped", blocked_reason="operator_stop")
                break
            task = get_computer_task(settings, task_id)
            if not isinstance(task, dict) or task.get("status") != "running":
                # Cancelled by /computer_stop.
                break

            # Human takeover is an exclusive GUI lease. Do not even observe
            # the desktop while a human may be typing secrets/payment data.
            # The store has its own TTL, so a lost browser session cannot
            # pause automation forever.
            takeover = takeover_store.current()
            if takeover is not None:
                if pause_started is None:
                    pause_started = time.monotonic()
                await asyncio.sleep(0.25)
                continue
            if pause_started is not None:
                start += time.monotonic() - pause_started
                pause_started = None
                # Discard any planner result and refresh the desktop after a
                # handoff. The old plan was made against a pre-human screen.
                followup_observe = True

            if followup_observe:
                # Refresh the desktop after every mutating/wait action before
                # asking the planner for its next decision. This prevents a
                # successful click from being mistaken for a verified UI state.
                action = {"action": "observe"}
                followup_observe = False
            else:
                # Explicit read-only goals use a deterministic observe->done
                # policy so the planner cannot add an unnecessary click.
                action = maybe_observe_only_action(goal=goal, trajectory=trajectory)
                if action is None:
                    # Simple single-digit goals bypass Codex to avoid multi-click
                    # thrash (e.g. display ending as 113 instead of 1).
                    action = maybe_simple_digit_action(
                        goal=goal,
                        observation=observation,
                        trajectory=trajectory,
                    )
                if action is None:
                    try:
                        remaining = max_seconds - (time.monotonic() - start)
                        if remaining <= 0:
                            set_task_status(settings, task_id, "stopped", blocked_reason="max_seconds reached")
                            break
                        planner_timeout = min(float(settings.codex_timeout_seconds), remaining)
                        action = await asyncio.wait_for(
                            planner.next_action(
                                goal=goal,
                                observation=observation,
                                trajectory=trajectory,
                                steps_used=steps_used,
                                max_steps=max_steps,
                            ),
                            timeout=max(0.1, planner_timeout),
                        )
                    except asyncio.TimeoutError:
                        set_task_status(settings, task_id, "stopped", blocked_reason="planner_timeout")
                        break
                    except Exception as exc:  # pragma: no cover - defensive
                        set_task_status(
                            settings,
                            task_id,
                            "error",
                            blocked_reason=f"planner_error:{type(exc).__name__}",
                        )
                        break
                target_app = infer_target_app(goal)
                if target_app and action.get("action") == "observe" and not action.get("target_app"):
                    action["target_app"] = target_app
            action = normalize_action(action)
            action = _with_observed_target(action, observation)
            act = action.get("action")

            if act == "done":
                # A planner summary is not a desktop change. Three failed
                # clicks followed by "the file manager is already open"
                # left the window where it was.
                if _latest_mutation_failed(trajectory):
                    false_done += 1
                    trajectory.append({
                        "action_type": "done",
                        "result_ok": False,
                        "error": "unverified_done",
                    })
                    if false_done >= 2:
                        set_task_status(
                            settings,
                            task_id,
                            "error",
                            blocked_reason="unverified_done",
                        )
                        break
                    continue
                set_task_status(settings, task_id, "done", summary=action.get("summary") or "completed")
                break
            if act == "stop":
                set_task_status(settings, task_id, "stopped", blocked_reason=action.get("reason") or "planner_stop")
                break

            # Allow-list + blocked-keyword guard on the action itself.
            if not is_action_allowed(settings, action):
                set_task_status(settings, task_id, "blocked", blocked_reason=f"disallowed_action:{act}")
                break
            hit = contains_blocked_keyword(settings, _action_text(action))
            if hit is not None:
                set_task_status(settings, task_id, "blocked", blocked_reason=f"blocked_keyword:{hit}")
                break

            # Re-check the exclusive lease immediately before creating a
            # mutating/observe step, closing the planner-to-executor race.
            if takeover_store.current() is not None:
                if pause_started is None:
                    pause_started = time.monotonic()
                continue

            # Create + execute the step.
            step = create_computer_step(settings, task_id, action)
            if not step.get("ok"):
                if step.get("error") == "human_takeover_active":
                    if pause_started is None:
                        pause_started = time.monotonic()
                    followup_observe = True
                    continue
                set_task_status(settings, task_id, "error", blocked_reason=step.get("error", "step_create_failed"))
                break
            step_id = step["step_id"]
            step_start = time.monotonic()
            try:
                result = await backend.execute_step(settings, task_id, step_id, action)
            except ComputerBackendError as exc:
                # /computer_stop can cancel the task while the Mac is
                # completing the current step. Preserve operator_stop rather
                # than overwriting the deliberate stopped state with error.
                current_task = get_computer_task(settings, task_id)
                if isinstance(current_task, dict) and current_task.get("status") == "stopped":
                    break
                if str(exc) == "human_takeover_active":
                    # The pending step was never claimed/executed. Re-enter
                    # the lease wait and, after release, observe before using
                    # the planner again.
                    followup_observe = True
                    continue
                if str(exc) in {"task_not_running", "step_cancelled"}:
                    set_task_status(settings, task_id, "stopped", blocked_reason="operator_stop")
                    break
                set_task_status(settings, task_id, "error", blocked_reason=str(exc))
                break
            duration_ms = int((time.monotonic() - step_start) * 1000)

            # Record a redacted trajectory entry (include short clicked_label for completion).
            clicked_label = resolve_clicked_label(action, observation)
            entry = {
                "action_type": act,
                "action_redacted": redact_computer_action(action),
                "result_ok": bool(result.get("result_ok", True)) if isinstance(result, dict) else True,
                "screenshot_id": (result or {}).get("screenshot_id"),
                "screenshot_hash": (result or {}).get("sha256"),
                "error": (result or {}).get("error"),
                "duration_ms": duration_ms,
            }
            # Preserve only the already allow-listed, short driver metadata
            # that makes a desktop click auditable without storing UI text.
            if isinstance(result, dict):
                for field in ("active_app", "click_method", "pid", "window_id", "ax_app"):
                    value = result.get(field)
                    if value is not None:
                        entry[field] = value
            if clicked_label:
                entry["clicked_label"] = clicked_label
            append_trajectory(settings, task_id, entry)
            trajectory.append(entry)
            if isinstance(result, dict):
                # Preserve AX hints across non-observe steps so simple-digit
                # completion still sees labels after a click result.
                merged = dict(result)
                for k in ("pid", "window_id", "element_hints", "ax_app"):
                    if merged.get(k) is None and observation.get(k) is not None:
                        merged[k] = observation.get(k)
                observation = merged
            steps_used += 1
            if act != "observe":
                followup_observe = True

            # Post-step app gate: blocklist always; allowlist only for
            # mutating actions. Bare observe often reports frontmost=Codex
            # while the goal app is Calculator — must not stop the task.
            active_app = (result or {}).get("active_app")
            if active_app:
                from desktop_computer_requests import (
                    action_enforces_app_allowlist,
                    check_app_allowlist_blocklist,
                )
                is_ok, reason = check_app_allowlist_blocklist(
                    settings,
                    active_app,
                    enforce_allowlist=action_enforces_app_allowlist(action),
                )
                if not is_ok:
                    set_task_status(settings, task_id, "stopped", blocked_reason=reason)
                    break

            # Hard caps.
            if steps_used >= max_steps:
                set_task_status(settings, task_id, "done", summary="max_steps reached")
                break
            if time.monotonic() - start > max_seconds:
                set_task_status(settings, task_id, "stopped", blocked_reason="max_seconds reached")
                break
    finally:
        pass

    final = get_computer_task(settings, task_id) or {}
    return {
        "ok": True,
        "task_id": task_id,
        "status": final.get("status"),
        "summary": final.get("summary"),
        "blocked_reason": final.get("blocked_reason"),
        "steps_used": steps_used,
        "trajectory_len": len(trajectory),
    }


_MUTATING_ACTIONS = frozenset({"click", "type", "hotkey", "scroll"})


def _latest_mutation_failed(trajectory: list[dict]) -> bool:
    """True when the newest click/type/hotkey/scroll did not succeed."""
    for entry in reversed(trajectory or []):
        if not isinstance(entry, dict):
            continue
        if entry.get("action_type") not in _MUTATING_ACTIONS:
            continue
        return not bool(entry.get("result_ok", True))
    return False


def _action_text(action: dict) -> str:
    """Flatten an action's payload for blocked-keyword scanning."""
    if not isinstance(action, dict):
        return ""
    parts = [str(action.get("action", ""))]
    for key in ("text", "keys", "summary", "reason"):
        v = action.get(key)
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            parts.extend(str(x) for x in v)
    return " ".join(parts)
