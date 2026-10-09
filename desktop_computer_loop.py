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
    goal_needs_browser,
    goal_needs_loaded_page,
    is_observe_only_goal,
    maybe_followup_label_action,
    maybe_observe_only_action,
    maybe_simple_digit_action,
    resolve_clicked_label,
)
from desktop_computer_requests import (
    HOST_SCOPE,
    append_trajectory,
    cancel_computer_task,
    cancel_pending_computer_step,
    contains_blocked_keyword,
    create_computer_step,
    create_computer_task,
    get_computer_task,
    is_action_allowed,
    normalize_action,
    BROWSER_PAGE_STATES,
    browser_page_state_from_title,
    redact_computer_action,
    set_task_status,
    task_scope,
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
        if (
            action.get("window_id") is None
            and str(action["pid"]) == str(observation.get("pid"))
            and observation.get("window_id") is not None
        ):
            return dict(action, window_id=observation["window_id"])
        return action
    pid = observation.get("pid")
    if pid is None:
        return action
    bound = dict(action)
    bound["pid"] = pid
    if bound.get("window_id") is None and observation.get("window_id") is not None:
        bound["window_id"] = observation["window_id"]
    return bound


def _mutation_identity(action: dict) -> tuple:
    """Identity of one mutating action. Text is hashed so it is not stored."""
    import hashlib
    act = str(action.get("action") or "")
    if act == "click":
        payload: tuple = (action.get("x"), action.get("y"), action.get("button") or "left")
    elif act == "type":
        raw = str(action.get("text") or "").encode("utf-8")
        payload = (hashlib.sha256(raw).hexdigest()[:16], len(raw))
    elif act == "hotkey":
        payload = tuple(str(k) for k in (action.get("keys") or []))
    elif act == "scroll":
        payload = (action.get("dx"), action.get("dy"))
    else:
        payload = ()

    def _num(value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return (act, payload, _num(action.get("pid")), _num(action.get("window_id")))


def _focus_identity(observation: dict) -> tuple:
    if not isinstance(observation, dict):
        return (None, None, None)
    pid = observation.get("pid")
    wid = observation.get("window_id")
    try:
        pid_i = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        pid_i = None
    try:
        wid_i = int(wid) if wid is not None else None
    except (TypeError, ValueError):
        wid_i = None
    return (observation.get("active_app"), pid_i, wid_i)


def _reported_window_title(observation: dict) -> str:
    """Title the backend already reported, or the matching window's title.

    Legacy Mac observations list windows but omit ``window_title``. Only a
    row that matches the observed pid or window id is used. Nothing is
    invented when that identity is missing.
    """
    if not isinstance(observation, dict):
        return ""
    raw = observation.get("window_title")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:120]
    windows = observation.get("windows")
    if not isinstance(windows, list):
        return ""
    pid = observation.get("pid")
    wid = observation.get("window_id")
    for row in windows:
        if not isinstance(row, dict):
            continue
        matched = False
        if wid is not None:
            try:
                matched = int(row.get("window_id")) == int(wid)
            except (TypeError, ValueError):
                matched = False
        if not matched and pid is not None and wid is None:
            try:
                matched = int(row.get("pid")) == int(pid)
            except (TypeError, ValueError):
                matched = False
        if not matched:
            continue
        title = row.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()[:120]
    return ""


def _windows_have_title(observation: dict) -> bool:
    windows = observation.get("windows")
    if not isinstance(windows, list):
        return False
    return any(isinstance(row, dict) and isinstance(row.get("title"), str) and row.get("title").strip() for row in windows)


def _fresh_browser_page_state(result: dict) -> str:
    """State for this result only. A raw title is not kept.

    Legacy Mac results omit ``browser_page_state`` and put a short title on
    the matching window row. That title is classified and discarded.
    """
    raw = result.get("browser_page_state")
    if isinstance(raw, str) and raw in BROWSER_PAGE_STATES:
        return raw
    if not _reported_window_title(result) and "window_title" not in result and not _windows_have_title(result):
        return "unknown"
    return browser_page_state_from_title(_reported_window_title(result))


class _VisualProgress:
    """Stall only when the same mutation is still on the same window and pixels.

    A matching PNG by itself is not failure: independent desktops can keep
    serving one fixture image while clicks, typing and hotkeys do different
    work. Waits and loading titles are not stalls.
    """

    def __init__(self) -> None:
        self.pending: tuple | None = None
        self.last_identity: tuple | None = None
        self.same_unchanged = 0
        self.recovery_used = False
        self.recover_next = False
        self.stalled_hash: str | None = None

    def reset(self) -> None:
        self.pending = None
        self.last_identity = None
        self.same_unchanged = 0
        self.recovery_used = False
        self.recover_next = False
        self.stalled_hash = None

    def note_mutation(self, action: dict, before_hash: object, observation: dict) -> None:
        if str(action.get("action") or "") not in _MUTATING_ACTIONS:
            return
        before = before_hash if isinstance(before_hash, str) and before_hash else None
        self.pending = (_mutation_identity(action), before, _focus_identity(observation))

    def note_observe(self, result: dict) -> str:
        if self.recover_next:
            return "recovery"
        if self.pending is None:
            return "ignore"
        identity, before, focus = self.pending
        self.pending = None
        if _fresh_browser_page_state(result) == "loading" or result.get("effect") == "loading":
            return "ignore"
        after = result.get("sha256")
        if not isinstance(before, str) or not isinstance(after, str):
            return "ignore"
        if after != before or _focus_identity(result) != focus:
            self.last_identity = None
            self.same_unchanged = 0
            self.recovery_used = False
            self.stalled_hash = None
            return "progress"
        if identity == self.last_identity:
            self.same_unchanged += 1
        else:
            self.last_identity = identity
            self.same_unchanged = 1
            self.recovery_used = False
            self.stalled_hash = None
        if self.same_unchanged >= 2 and not self.recovery_used:
            self.recovery_used = True
            self.stalled_hash = after
            self.recover_next = True
            return "needs_recovery"
        if self.same_unchanged >= 2 and self.recovery_used:
            return "stall"
        return "unchanged"

    def note_recovery_observe(self, result: dict) -> bool:
        """True when the one recovery observe is still the stalled image."""
        self.recover_next = False
        after = result.get("sha256") if isinstance(result, dict) else None
        loading = isinstance(result, dict) and _fresh_browser_page_state(result) == "loading"
        if self.stalled_hash and after == self.stalled_hash and not loading:
            return True
        self.reset()
        return False


def _browser_kind(app: object) -> str | None:
    from desktop_linux_browser import BROWSERS, canonical_browser

    if str(app or "").strip().lower() == "safari":
        return "Safari"  # Completion compatibility, never a Linux launch capability.
    browser = canonical_browser(app)
    return browser if browser in BROWSERS else None


def _observed_browser_kind(observation: dict) -> str | None:
    app = observation.get("active_app")
    if app and app != "Unknown":
        return _browser_kind(app)
    return _browser_kind(observation.get("ax_app"))


def _browser_foreground_observed(observation: dict) -> bool:
    """Do not let the model declare a browser task done from XFCE panels.

    This is only a browser-window gate, not proof that a webpage loaded or
    that a weather forecast was read. End-to-end page verification is separate.
    """
    if not isinstance(observation, dict):
        return False
    return _observed_browser_kind(observation) is not None


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


def build_backend(settings: Settings, task_id: str | None = None) -> Any:
    """Executor for a task's steps.

    A task that belongs to an agent with its own desktop runs right here on
    that display. Everything else goes to the host's desktop node as before.
    """
    if settings.conveyor_computer_backend == "fake":
        return FakeComputerBackend(settings)
    task = get_computer_task(settings, task_id) if task_id else None
    scope = task_scope(task)
    if scope != HOST_SCOPE and isinstance(task, dict) and task.get("agent_id"):
        import agents
        from desktop_x11 import X11ComputerBackend

        agent = agents.AgentStore(settings).get(str(task["agent_id"]))
        if agent and agent.get("display") is not None and not agent["archived"]:
            return X11ComputerBackend(settings, agent_id=agent["id"], display=int(agent["display"]), scope=scope)
        raise ComputerBackendError("agent_desktop_missing")
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
    open_with_observe: bool = False,
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
    fallback_observe = False
    target_app_suppressed = False
    last_failed_signature: tuple | None = None
    repeated_failures = 0
    progress = _VisualProgress()
    nav: _NavigationSettle | None = None
    nav_unsettled = False
    submission = _BrowserSubmission(goal)
    browser_goal = goal_needs_browser(goal) and not is_observe_only_goal(goal)
    takeover_store = HumanTakeoverStore(settings)
    # The lease that pauses this task is the one for the desktop it acts on.
    scope = task_scope(get_computer_task(settings, task_id))

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
            takeover = takeover_store.current(scope)
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
                progress.reset()
                # The human may have changed the page. Drop the in-flight
                # navigation samples and look at the desktop again.
                nav = None
                nav_unsettled = False
                submission = _BrowserSubmission(goal)
                # The human may have changed the desktop. Consecutive
                # pre-takeover failures are not evidence about the new screen.
                repeated_failures = 0
                last_failed_signature = None

            if nav is not None and nav.active:
                now = _monotonic()
                if time.monotonic() - start > max_seconds:
                    set_task_status(settings, task_id, "stopped", blocked_reason="max_seconds reached")
                    break
                if now >= nav.deadline:
                    nav.active = False
                    nav_unsettled = True
                    goto_execute = False
                elif now < nav.next_sample_at:
                    await _sleep(min(0.25, nav.next_sample_at - now))
                    continue
                else:
                    action = {"action": "observe"}
                    # No planner call and no extra key while the submission paints.
                    goto_execute = True
            else:
                goto_execute = False

            if goto_execute:
                pass
            elif followup_observe or fallback_observe or progress.recover_next:
                # A failed targeted observation must be followed by a plain
                # screenshot: never re-inject the same broken target_app.
                # One verified browser refocus is a different recovery from
                # repeating the click that did not change the screen.
                action = {"action": "observe"}
                if progress.recover_next and browser_goal and not fallback_observe:
                    action["ensure_browser"] = True
                followup_observe = False
                fallback_observe = False
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
                    # "再点等号" presses that button. A missing label falls
                    # through so Codex can use the screenshot.
                    action = maybe_followup_label_action(
                        goal=goal,
                        observation=observation,
                        trajectory=trajectory,
                    )
                if (
                    action is None
                    and open_with_observe
                    and not trajectory
                    and not observation.get("screenshot_id")
                ):
                    # The model cannot see the screen until a screenshot
                    # exists. Looking first saves one Codex round trip.
                    action = {"action": "observe"}
                    if browser_goal:
                        # An OS-controlled browser preflight (Linux only);
                        # independent X11 desktops have their own bootstrap.
                        action["ensure_browser"] = True
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
                                observation=dict(observation, browser_navigation_status=submission.status(observation)),
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
                if (
                    target_app and not target_app_suppressed
                    and action.get("action") == "observe" and not action.get("target_app")
                ):
                    action["target_app"] = target_app
            action = normalize_action(action)
            if action.get("ensure_browser") is not True or not browser_goal or is_observe_only_goal(goal):
                action.pop("ensure_browser", None)
            action = _with_observed_target(action, observation)
            act = action.get("action")

            if (
                browser_goal and act in {"type", "hotkey", "scroll"}
                and not _browser_foreground_observed(observation)
            ):
                # A blind plan, or an observation of a launcher, is not a
                # keyboard target. Resolve the browser before asking again.
                action = {"action": "observe", "ensure_browser": True}
                target = infer_target_app(goal)
                if target:
                    action["target_app"] = target
                act = "observe"

            if act == "done":
                # A planner claim is not evidence that Firefox is on screen.
                # The browser gate checks a fresh observation and, when the
                # desktop supplied a title, a blank or error page. It does
                # not read the screenshot.
                reason = _reject_done(
                    goal=goal,
                    browser_goal=browser_goal,
                    observation=observation,
                    trajectory=trajectory,
                    progress=progress,
                    navigation_pending=bool(nav is not None and nav.active) or nav_unsettled,
                    address_pending=submission.pending,
                )
                if reason == "no_visual_progress":
                    set_task_status(settings, task_id, "stopped", blocked_reason=reason)
                    break
                if reason:
                    false_done += 1
                    trajectory.append({
                        "action_type": "done",
                        "result_ok": False,
                        "error": reason,
                    })
                    if false_done >= 2:
                        set_task_status(
                            settings,
                            task_id,
                            "error",
                            blocked_reason=reason,
                        )
                        break
                    # Look again before the next done. A second claim has to
                    # see the same invalid page, not a screenshot-less fallback.
                    if (
                        reason == "unverified_browser_window"
                        and _browser_foreground_observed(observation)
                    ):
                        followup_observe = True
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
            if takeover_store.current(scope) is not None:
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
            # Compare the actual post-action screenshot, not just the tool's
            # success return code. PNG hashes are retained only as metadata.
            before_visual_hash = observation.get("sha256") if isinstance(observation, dict) else None
            recovering = progress.recover_next and act == "observe"
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
            success = bool(result.get("result_ok", True)) if isinstance(result, dict) else False
            error_code = str((result or {}).get("error") or "") if isinstance(result, dict) else "invalid_result"
            settle_outcome = None
            if (
                nav is not None and nav.active and act == "observe" and success
                and isinstance(result, dict)
            ):
                settle_outcome = nav.sample(result, _monotonic())
                if settle_outcome == "settled":
                    nav_unsettled = False
            # Samples taken while a navigation is still painting are not stalls.
            # A loading title is not a stable sample and is not a stall either.
            if act == "observe" and success and isinstance(result, dict) and settle_outcome not in {"sampling", "loading"}:
                if recovering:
                    if progress.note_recovery_observe(result):
                        result["effect"] = "no_visible_change"
                    else:
                        result["effect"] = "visible_change"
                else:
                    effect = progress.note_observe(result)
                    if effect == "progress":
                        result["effect"] = "visible_change"
                    elif effect in {"unchanged", "needs_recovery", "stall"}:
                        result["effect"] = "no_visible_change"
            elif act in _MUTATING_ACTIONS and success:
                submission.note_success(action, observation)
                progress.note_mutation(action, before_visual_hash, observation)
                if _is_browser_navigation(action, observation):
                    nav = _NavigationSettle(_monotonic())
                    nav_unsettled = False
            # A successful step does not clear repeated_failures. An observe
            # inserted between two failed attempts is not a new plan, and a
            # later success of a different action is not proof the failing
            # attempt started working.

            if not success:
                signature = _failure_signature(action, error_code)
                repeated_failures = repeated_failures + 1 if signature == last_failed_signature else 1
                last_failed_signature = signature
                if (
                    error_code in {"target_app_not_found", "target_app_activate_failed", "target_app_not_running"}
                    or error_code.startswith("browser_") or error_code == "xdotool_missing"
                ):
                    target_app_suppressed = True
                    fallback_observe = True
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
            if isinstance(result, dict) and result.get("effect"):
                entry["effect"] = result["effect"]
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
                merged.pop("window_title", None)
                if result.get("screenshot_id"):
                    # Bind this screenshot only. A previous loaded state does
                    # not carry onto a later capture that has no state of its own.
                    merged["browser_page_state"] = _fresh_browser_page_state(result)
                else:
                    merged.pop("browser_page_state", None)
                observation = merged
            steps_used += 1
            if act != "observe":
                followup_observe = True
            if nav is not None and nav.active:
                followup_observe = False
            if repeated_failures >= 3:
                set_task_status(settings, task_id, "error", blocked_reason="repeated_action_failure")
                break
            if act == "observe" and success and isinstance(result, dict) and settle_outcome not in {"sampling", "loading"}:
                if (recovering and result.get("effect") == "no_visible_change") or (
                    progress.same_unchanged >= 2 and progress.recovery_used and not progress.recover_next
                ):
                    set_task_status(settings, task_id, "stopped", blocked_reason="no_visual_progress")
                    break

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

            # Hard caps. Running out of steps is a stop, same as running
            # out of time. A "done" row would look like the goal was met.
            if steps_used >= max_steps:
                set_task_status(
                    settings,
                    task_id,
                    "stopped",
                    blocked_reason="max_steps reached",
                )
                break
            if time.monotonic() - start > max_seconds:
                set_task_status(settings, task_id, "stopped", blocked_reason="max_seconds reached")
                break
    finally:
        pass

    final = get_computer_task(settings, task_id) or {}
    screenshot_id = ""
    for step in reversed(trajectory):
        if isinstance(step, dict) and isinstance(step.get("screenshot_id"), str):
            screenshot_id = step["screenshot_id"]
            break
    return {
        "ok": True,
        "task_id": task_id,
        "status": final.get("status"),
        "summary": final.get("summary"),
        "blocked_reason": final.get("blocked_reason"),
        "steps_used": steps_used,
        "trajectory_len": len(trajectory),
        "screenshot_id": screenshot_id,
    }


_MUTATING_ACTIONS = frozenset({"click", "type", "hotkey", "scroll"})
# After Enter or a browser click returns, Firefox can still be painting the
# previous document. These bound the wait; they are not a content proof.
_NAV_PAINT_SECONDS = 1.0
_NAV_SAMPLE_SECONDS = 0.3
_NAV_TIMEOUT_SECONDS = 5.0


def _monotonic() -> float:
    """Loop clock. Tests replace this without touching asyncio's clock."""
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    """Loop wait. Tests replace this without replacing asyncio.sleep."""
    await asyncio.sleep(seconds)


def _is_browser_navigation(action: dict, observation: dict) -> bool:
    """True for a browser Enter/Return or click. Address-bar keys do not count.

    Calculator and other non-browser fronts are unchanged. The check uses
    the pre-action observation, not a new screenshot.
    """
    if not isinstance(action, dict) or not _browser_foreground_observed(observation):
        return False
    act = str(action.get("action") or "")
    if act == "click":
        return True
    if act != "hotkey":
        return False
    keys = action.get("keys")
    if not isinstance(keys, list) or len(keys) != 1:
        return False
    return str(keys[0]).strip().lower() in {"enter", "return"}


class _BrowserSubmission:
    """Do not confuse an edited address bar with a loaded destination.

    Keep only focus identity and booleans, never the address or search text.
    Enter must be executed successfully on the same browser window. Explicit
    draft-only goals remain valid without submitting their text.
    """

    def __init__(self, goal: str) -> None:
        text = (goal or "").lower()
        draft_only = any(token in text for token in (
            "without submitting", "do not submit", "don't submit", "without pressing enter",
            "do not press enter", "don't press enter", "不要提交", "不要按回车", "不按回车",
        ))
        self.enabled = goal_needs_loaded_page(goal) and not draft_only
        self.address_focus: tuple | None = None
        self.pending_focus: tuple | None = None

    @property
    def pending(self) -> bool:
        return self.pending_focus is not None

    def status(self, observation: dict) -> str:
        focus = _focus_identity(observation)
        if self.pending_focus is not None:
            return "awaiting_submit"
        if self.address_focus == focus:
            return "address_focused"
        return "unknown"

    def note_success(self, action: dict, observation: dict) -> None:
        if not self.enabled or not _browser_foreground_observed(observation):
            return
        focus = _focus_identity(observation)
        act = action.get("action")
        keys = {str(k).strip().lower() for k in (action.get("keys") or [])}
        if act == "hotkey":
            if keys in ({"ctrl", "l"}, {"control", "l"}, {"cmd", "l"},
                        {"command", "l"}, {"meta", "l"}, {"alt", "d"}, {"f6"}):
                self.address_focus = focus
            elif keys in ({"enter"}, {"return"}):
                if self.pending_focus == focus:
                    self.pending_focus = None
                self.address_focus = None
        elif act == "type":
            text = str(action.get("text") or "").strip().lower()
            if self.address_focus == focus or text.startswith(("http://", "https://")):
                self.pending_focus = focus
        elif act == "click":
            # Clicking might dismiss the suggestions; it does not prove that
            # an edited address was submitted.
            self.address_focus = None


class _NavigationSettle:
    """Fresh observes until the GUI stops changing, or the deadline passes.

    Stability is an indicator. It does not prove the page body is correct.
    """

    def __init__(self, now: float) -> None:
        self.deadline = now + _NAV_TIMEOUT_SECONDS
        self.next_sample_at = now + _NAV_PAINT_SECONDS
        self.last: tuple | None = None
        self.matches = 0
        self.active = True

    def sample(self, result: dict, now: float) -> str:
        self.next_sample_at = now + _NAV_SAMPLE_SECONDS
        state = _fresh_browser_page_state(result)
        if state == "loading" or result.get("effect") == "loading":
            self.last = None
            self.matches = 0
            return "loading"
        digest = result.get("sha256")
        if not isinstance(digest, str) or not digest:
            self.last = None
            self.matches = 0
            return "sampling"
        sig = (digest, _focus_identity(result), state)
        if sig == self.last:
            self.matches += 1
        else:
            self.last = sig
            self.matches = 1
        if self.matches >= 2:
            self.active = False
            return "settled"
        return "sampling"


def _failure_signature(action: dict, error_code: str) -> tuple:
    """Group retries of the same attempt.

    Target-app text is omitted: a forced bare observe strips a broken
    target, and that must not look like a brand-new failure.
    """
    return (str(action.get("action") or ""), error_code, _mutation_identity(action)[1])


def _reject_done(
    *,
    goal: str,
    browser_goal: bool,
    observation: dict,
    trajectory: list[dict],
    progress: _VisualProgress,
    navigation_pending: bool = False,
    address_pending: bool = False,
) -> str | None:
    """Return a reason to refuse a planner ``done``, or None to accept it."""
    if address_pending:
        return "browser_navigation_not_submitted: press Enter in the address bar, then verify the new page"
    if navigation_pending:
        # A loaded title captured before the next document paints is not done.
        return "navigation_unsettled"
    if progress.same_unchanged >= 2 and progress.recovery_used:
        return "no_visual_progress"
    if not isinstance(observation, dict) or not observation.get("screenshot_id"):
        return "unverified_done"
    if not observation.get("sha256"):
        return "unverified_done"
    if progress.pending is not None:
        return "unverified_done"
    if _latest_mutation_failed(trajectory):
        return "unverified_done"
    if not browser_goal:
        return None
    last = trajectory[-1] if trajectory else None
    fresh = (
        isinstance(last, dict)
        and last.get("action_type") == "observe"
        and last.get("result_ok", True)
        and last.get("screenshot_id")
        and last.get("screenshot_id") == observation.get("screenshot_id")
    )
    if not fresh or not _browser_foreground_observed(observation):
        return "unverified_browser_window" if not _browser_foreground_observed(observation) else "unverified_done"
    requested = _browser_kind(infer_target_app(goal))
    if requested and _observed_browser_kind(observation) != requested:
        return "unverified_browser_window"
    if goal_needs_loaded_page(goal):
        # Loaded is required. A title is not content proof, and unknown,
        # blank, error, and loading all fail this gate.
        state = observation.get("browser_page_state")
        if state not in BROWSER_PAGE_STATES or state != "loaded":
            return "unverified_browser_window"
    return None


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
