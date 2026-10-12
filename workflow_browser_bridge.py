"""Executable *mock-only* browser bridge.

This is deliberately not registered with WebRuntime or the production workflow
engine. An actual Playwright/X11 adapter will require a separate reviewed PR.
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
        last = plan.actions[completed - 1]
        if not browser.readback(last):
            return BridgeResult("reconciliation_required", completed, completed - 1, "page_state_drifted")
    if completed == len(plan.actions):
        return BridgeResult("verified", completed, None, "fixture_checks_passed")

    for index in range(completed, len(plan.actions)):
        op = plan.actions[index]
        if journal.state(plan.plan_id, index) is not None:
            return BridgeResult("reconciliation_required", completed, index, "outcome_unknown")
        if browser.page != op.on_page:
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
            journal.verify(plan.plan_id, index, plan.plan_digest)
        except (FixtureOperationError, JournalConflict):
            return BridgeResult("reconciliation_required", completed, index, "outcome_unknown")
        completed += 1
    return BridgeResult("verified", completed, None, "fixture_checks_passed")
