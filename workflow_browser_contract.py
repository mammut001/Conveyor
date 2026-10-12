"""Typed fixture-only browser execution contracts.

Deliberately *not* a production BrowserProvider: no networking, subprocesses,
credentials, real browsers, payments, uploads or external submissions.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

FIXTURE = "visitor600_mock"
PAGES = frozenset({"idle", "welcome", "details", "review"})
OPERATIONS = frozenset({"open", "click", "fill", "assert_field", "assert_text"})
FIELDS = frozenset({"purpose", "funding"})
CONTROLS = frozenset({"next"})
TEXT_MARKERS = frozenset({"mock_welcome", "mock_review"})
PLAN_ID = re.compile(r"^[a-z][a-z0-9_-]{7,63}$")
MAX_ACTIONS = 24


class InvalidBrowserPlan(ValueError):
    """The plan is outside the deliberately small fixture capability."""


@dataclass(frozen=True)
class BrowserAction:
    kind: str
    on_page: str
    target: str
    expect_page: str
    value: str = ""


@dataclass(frozen=True)
class BrowserPlan:
    plan_id: str
    fixture: str
    actions: tuple[BrowserAction, ...]
    plan_digest: str


def parse_plan(raw: Any) -> BrowserPlan:
    """Allowlisted schema only. No URLs, selectors, JS, credentials or file paths."""
    if not isinstance(raw, dict) or set(raw) != {"plan_id", "fixture", "actions"}:
        raise InvalidBrowserPlan("invalid top-level plan keys")
    plan_id = raw["plan_id"]
    if not isinstance(plan_id, str) or not PLAN_ID.fullmatch(plan_id):
        raise InvalidBrowserPlan("invalid plan_id")
    if raw["fixture"] != FIXTURE:
        raise InvalidBrowserPlan("only the local mock fixture is available")
    ops = raw["actions"]
    if not isinstance(ops, list) or not 1 <= len(ops) <= MAX_ACTIONS:
        raise InvalidBrowserPlan("invalid action count")
    parsed = []
    for op in ops:
        if not isinstance(op, dict):
            raise InvalidBrowserPlan("action must be an object")
        kind = op.get("kind")
        expected_keys = {"kind", "on_page", "target", "expect_page"}
        if kind in ("fill", "assert_field"):
            expected_keys.add("value")
        if set(op) != expected_keys or kind not in OPERATIONS:
            raise InvalidBrowserPlan("unexpected action keys or kind")
        current = op["on_page"]
        after = op["expect_page"]
        target = op["target"]
        if current not in PAGES or after not in PAGES or not isinstance(target, str):
            raise InvalidBrowserPlan("invalid page or target")
        value = op.get("value", "")
        if kind == "open":
            if target != "welcome" or after != "welcome" or current != "idle":
                raise InvalidBrowserPlan("only the fixture welcome page may be opened")
        elif kind == "click":
            expected_after = {"welcome": "details", "details": "review"}.get(current)
            if target not in CONTROLS or expected_after is None or after != expected_after:
                raise InvalidBrowserPlan("only known fixture navigation is allowed")
        elif kind in ("fill", "assert_field"):
            if current != "details" or after != "details" or target not in FIELDS:
                raise InvalidBrowserPlan("unsupported fixture field")
            if (not isinstance(value, str) or not 1 <= len(value) <= 32
                    or not re.fullmatch(r"[A-Za-z_-]+", value)):
                raise InvalidBrowserPlan("only synthetic fixture values are allowed")
        elif kind == "assert_text":
            if target not in TEXT_MARKERS or after != current:
                raise InvalidBrowserPlan("unsupported fixture text check")
            if (current, target) not in (("welcome", "mock_welcome"), ("review", "mock_review")):
                raise InvalidBrowserPlan("text marker not on requested page")
        parsed.append(BrowserAction(kind, current, target, after, value))
    # A digest binds immutable fixture action definitions to durable journal entries.
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return BrowserPlan(plan_id, FIXTURE, tuple(parsed), digest)
