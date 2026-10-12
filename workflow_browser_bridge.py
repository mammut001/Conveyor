"""Bounded browser bridge: in-memory default, opt-in local Playwright runner.

Not registered with WebRuntime or the production workflow engine; real mode
can only use the bundled localhost fixture and needs explicit opt-in.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from workflow_browser_contract import BrowserAction, BrowserPlan, parse_plan
from workflow_browser_journal import BrowserAttemptJournal, JournalConflict


class FixtureOperationError(Exception):
    pass


@dataclass(frozen=True)
class BridgeResult:
    status: str  # verified | blocked | reconciliation_required
    completed: int
    action_index: int | None
    reason: str  # fixed reason code, never source text or private values


class FixtureBrowser:
    """In-memory, no-IO pretend portal; not a network/browser automation driver."""

    def __init__(self, *, interrupt_after_operation: int | None = None) -> None:
        self.page = "idle"
        self.fields: dict[str, str] = {}
        self.invocations = 0
        self.interrupt_after_operation = interrupt_after_operation

    def apply(self, op: BrowserAction) -> None:
        if self.page != op.on_page:
            raise FixtureOperationError("page_precondition_failed")
        if op.kind == "open":
            self.page = "welcome"
        elif op.kind == "click":
            if self.page == "welcome":
                self.page = "details"
            elif self.page == "details":
                self.page = "review"
            else:
                raise FixtureOperationError("unexpected_fixture_navigation")
        elif op.kind == "fill":
            self.fields[op.target] = op.value
        elif op.kind == "assert_field":
            if self.fields.get(op.target) != op.value:
                raise FixtureOperationError("field_assertion_failed")
        elif op.kind == "assert_text":
            if (self.page, op.target) not in (
                ("welcome", "mock_welcome"), ("review", "mock_review"),
            ):
                raise FixtureOperationError("text_assertion_failed")
        else:
            raise FixtureOperationError("unsupported_fixture_action")
        self.invocations += 1
        if self.interrupt_after_operation == self.invocations:
            # The action DID happen but the caller cannot tell whether it did.
            raise FixtureOperationError("simulated_process_interruption")

    def field_matches(self, name: str, value: str) -> bool:
        return self.fields.get(name) == value

    def readback(self, op: BrowserAction) -> bool:
        if self.page != op.expect_page:
            return False
        if op.kind in ("fill", "assert_field"):
            return self.fields.get(op.target) == op.value
        if op.kind == "assert_text":
            return (self.page, op.target) in (
                ("welcome", "mock_welcome"), ("review", "mock_review"),
            )
        return True


def execute_fixture_plan(
    raw_plan: Any, *, browser: FixtureBrowser, journal: BrowserAttemptJournal,
) -> BridgeResult:
    """Prevalidated fixture operations with read-back and no speculative retries.

    A state of 'dispatched' is treated as unknown external outcome even if the
    fake page looks right: don't replay, and don't silently mark committed.
    """
    if type(browser) is not FixtureBrowser:
        raise ValueError("only the in-memory fixture browser is supported")
    return _execute_plan(raw_plan, browser=browser, journal=journal)


def _execute_plan(raw_plan: Any, *, browser: Any, journal: BrowserAttemptJournal,
                  guard=None) -> BridgeResult:
    """Share journal/readback semantics, never auto-replay a dispatched action."""
    plan: BrowserPlan = parse_plan(raw_plan)
    # The journal is authoritative about completed actions. Verify one contiguous
    # prefix, then reconcile against the latest checkpoint, not old page states:
    # a form on "review" must not be forced back to earlier "welcome" pages.
    completed = 0
    for index, op in enumerate(plan.actions):
        prior = journal.state(plan.plan_id, index)
        if prior is None:
            break
        if (prior["plan_digest"] != plan.plan_digest or prior["kind"] != op.kind
                or prior["before_page"] != op.on_page
                or prior["after_page"] != op.expect_page):
            return BridgeResult("blocked", completed, index, "plan_changed")
        if prior["status"] != "verified":
            return BridgeResult("reconciliation_required", completed, index, "outcome_unknown")
        completed += 1
    if completed:
        if guard is not None:
            denied = guard()
            if denied:
                return BridgeResult("blocked", completed, completed - 1, denied)
        last = plan.actions[completed - 1]
        try:
            last_matches = browser.readback(last)
        except Exception:
            last_matches = False
        if not last_matches:
            return BridgeResult("reconciliation_required", completed, completed - 1, "page_state_drifted")
        # The most recent action may just assert the review page exists.
        # Replaying a verified prefix must also re-check the values filled
        # earlier, not report success while a prior form field drifted.
        latest_fields: dict[str, str] = {}
        for previous in plan.actions[:completed]:
            if previous.kind in ("fill", "assert_field"):
                latest_fields[previous.target] = previous.value
        try:
            fields_match = all(browser.field_matches(key, value)
                               for key, value in latest_fields.items())
        except Exception:
            fields_match = False
        if not fields_match:
            return BridgeResult("reconciliation_required", completed, completed - 1, "field_state_drifted")
    if completed == len(plan.actions):
        return BridgeResult("verified", completed, None, "fixture_checks_passed")

    for index in range(completed, len(plan.actions)):
        op = plan.actions[index]
        if guard is not None:
            denied = guard()
            if denied:
                return BridgeResult("blocked", completed, index, denied)
        if journal.state(plan.plan_id, index) is not None:
            return BridgeResult("reconciliation_required", completed, index, "outcome_unknown")
        try:
            on_expected_page = browser.page == op.on_page
        except Exception:
            on_expected_page = False
        if not on_expected_page:
            return BridgeResult("blocked", completed, index, "page_precondition_failed")
        try:
            claim = journal.dispatch(
                plan.plan_id, index, plan.plan_digest, op.kind, op.on_page, op.expect_page,
            )
        except JournalConflict:
            return BridgeResult("blocked", completed, index, "plan_changed")
        if claim != "dispatched_new":
            return BridgeResult("reconciliation_required", completed, index, "outcome_unknown")
        try:
            browser.apply(op)
            if not browser.readback(op):
                return BridgeResult("reconciliation_required", completed, index, "readback_failed")
            if guard is not None and guard():
                return BridgeResult("reconciliation_required", completed, index, "human_or_workflow_interruption")
            journal.verify(plan.plan_id, index, plan.plan_digest)
        except Exception:
            return BridgeResult("reconciliation_required", completed, index, "outcome_unknown")
        completed += 1
    return BridgeResult("verified", completed, None, "fixture_checks_passed")


def execute_workflow_browser_plan(
    raw_plan: Any, *, browser: Any, journal: BrowserAttemptJournal,
    workflow_store: Any, workflow_id: str,
) -> BridgeResult:
    """Opt-in local real browser or in-memory fixture bound to a running CAS step.

    Operator must explicitly create and begin the browser workflow step. An
    interrupted/human-owned checkpoint never triggers an automatic transition.
    Journal verification precedes the workflow checkpoint; a crash in between
    is recoverable without re-executing browser side effects.
    """
    from durable_workflows import WorkflowConflict, WorkflowNotFound
    from workflow_browser_playwright import LocalPlaywrightBrowser, real_browser_enabled

    if type(browser) is not FixtureBrowser and type(browser) is not LocalPlaywrightBrowser:
        raise ValueError("unknown browser executor")
    if type(browser) is LocalPlaywrightBrowser and not real_browser_enabled():
        return BridgeResult("blocked", 0, None, "real_browser_disabled")
    plan = parse_plan(raw_plan)

    try:
        workflow = workflow_store.get(workflow_id)
    except (WorkflowNotFound, ValueError):
        return BridgeResult("blocked", 0, None, "workflow_not_found")
    active = next((step for step in workflow["steps"] if step["state"] != "completed"), None)
    if active is None or active["kind"] != "browser":
        return BridgeResult("blocked", 0, None, "browser_step_required")
    ordinal = active["ordinal"]
    start_revision = workflow["revision"]

    def guard() -> str | None:
        if type(browser) is LocalPlaywrightBrowser and not real_browser_enabled():
            return "real_browser_disabled"
        try:
            item = workflow_store.get(workflow_id)
        except WorkflowNotFound:
            return "workflow_not_found"
        if item["state"] == "waiting_for_human":
            return "human_takeover"
        if item["state"] in ("paused", "needs_reverification"):
            return "workflow_paused"
        if item["revision"] != start_revision:
            return "workflow_revision_changed"
        steps = item["steps"]
        if (item["state"] != "active" or steps[ordinal]["kind"] != "browser" or
                steps[ordinal]["state"] != "running" or
                any(s["state"] != "completed" for s in steps[:ordinal])):
            return "workflow_not_running"
        return None

    denied = guard()
    if denied:
        return BridgeResult("blocked", 0, None, denied)
    try:
        journal.bind_workflow(plan.plan_id, workflow_id, ordinal, plan.plan_digest)
    except JournalConflict:
        return BridgeResult("blocked", 0, None, "plan_changed")
    result = _execute_plan(raw_plan, browser=browser, journal=journal, guard=guard)
    if result.status != "verified":
        return result
    denied = guard()
    if denied:
        return BridgeResult("blocked", result.completed, None, denied)

    current = workflow_store.get(workflow_id)
    try:
        workflow_store.transition(
            workflow_id, action="complete", expected_revision=current["revision"],
            request_id="browser-complete-" + plan.plan_digest[:32],
        )
    except WorkflowConflict:
        return BridgeResult("reconciliation_required", result.completed, None,
                            "workflow_revision_changed")
    return result
