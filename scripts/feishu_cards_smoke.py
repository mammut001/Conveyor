#!/usr/bin/env python3
"""feishu_cards_smoke.py — env-free tests for the Feishu card layer.

Covers:
  - Card JSON builders return valid Feishu interactive-card dicts
    (header, elements, update_multi, wide_screen_mode, action buttons).
  - Action allowlist + parse_action() rejects unknown / malformed
    payloads.
  - action_to_command() mapping.
  - flatten_card_to_text() preserves header + markdown content.
  - end-to-end dispatch mapping for both slash-style and
    confirm-style actions, including sender + token + chat binding.

Run: .venv/bin/python scripts/feishu_cards_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.harness_common import CheckResult, print_results  # noqa: E402

from channel.feishu_cards import (  # noqa: E402
    ALLOWED_ACTIONS,
    action_to_command,
    computer_status_card,
    confirm_action_card,
    diff_preview_card,
    flatten_card_to_text,
    job_failed_card,
    job_finished_card,
    job_started_card,
    node_status_card,
    parse_action,
    status_card,
)


# ---- Card builder shape tests ---------------------------------------------


def _check_card_shape(card: dict, name: str) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.append(CheckResult(
        f"{name}: top-level dict",
        isinstance(card, dict),
        f"type={type(card).__name__}",
    ))
    cfg = card.get("config") or {}
    results.append(CheckResult(
        f"{name}: config.wide_screen_mode=True",
        cfg.get("wide_screen_mode") is True,
        f"config={cfg!r}",
    ))
    results.append(CheckResult(
        f"{name}: config.update_multi=True (so edit_progress still works)",
        cfg.get("update_multi") is True,
        f"config={cfg!r}",
    ))
    header = card.get("header") or {}
    results.append(CheckResult(
        f"{name}: header.title.content non-empty",
        isinstance(header.get("title"), dict)
        and bool(header.get("title", {}).get("content")),
        f"header={header!r}",
    ))
    results.append(CheckResult(
        f"{name}: header.template in tone palette",
        header.get("template") in {
            "blue", "wathet", "turquoise", "green", "yellow",
            "orange", "red", "carmine", "violet", "purple",
            "indigo", "grey",
        },
        f"template={header.get('template')!r}",
    ))
    elements = card.get("elements") or []
    results.append(CheckResult(
        f"{name}: elements non-empty list",
        isinstance(elements, list) and len(elements) >= 1,
        f"len={len(elements) if isinstance(elements, list) else 'n/a'}",
    ))
    return results


def _collect_buttons(card: dict) -> list[dict]:
    buttons: list[dict] = []
    for el in card.get("elements") or []:
        if isinstance(el, dict) and el.get("tag") == "action":
            for btn in el.get("actions") or []:
                if isinstance(btn, dict):
                    buttons.append(btn)
    return buttons


def _check_button_values(card: dict, expected_actions: set[str], name: str) -> list[CheckResult]:
    buttons = _collect_buttons(card)
    seen = set()
    for btn in buttons:
        val = btn.get("value")
        payload = parse_action(val)
        if payload is None:
            return [CheckResult(
                f"{name}: every button value is a valid action payload",
                False,
                f"bad value: {val!r}",
            )]
        seen.add(payload["action"])
    return [CheckResult(
        f"{name}: buttons cover expected actions {sorted(expected_actions)} (got {sorted(seen)})",
        expected_actions.issubset(seen),
        f"expected={sorted(expected_actions)} seen={sorted(seen)}",
    )]


def _test_job_started_card() -> list[CheckResult]:
    card = job_started_card(
        job_id="job-abc",
        prompt="fix the failing test",
        worktree="/srv/conveyor/worktrees/day-2026-07-01",
    )
    results = _check_card_shape(card, "job_started_card")
    results += _check_button_values(card, {"status", "diff", "cancel"}, "job_started_card")
    return results


def _test_job_finished_card() -> list[CheckResult]:
    card = job_finished_card(
        job_id="job-abc",
        summary="Fixed test_parser; 3 assertions now pass.",
        changed_files=["tests/test_parser.py", "src/parser.py"],
    )
    results = _check_card_shape(card, "job_finished_card")
    results += _check_button_values(card, {"status", "diff", "apply", "discard"}, "job_finished_card")
    # Verify changed files are rendered in markdown
    md = ""
    for el in card["elements"]:
        if el.get("tag") == "markdown":
            md = el.get("content", "")
    return results + [CheckResult(
        "job_started_card: changed files appear in markdown body",
        "tests/test_parser.py" in md and "src/parser.py" in md,
        f"body head: {md[:200]!r}",
    )]


def _test_job_failed_card() -> list[CheckResult]:
    card = job_failed_card(
        job_id="job-abc",
        error="Codex process exited with code 1",
    )
    results = _check_card_shape(card, "job_failed_card")
    # Failed card includes status + cancel; no apply/discard.
    return results + _check_button_values(card, {"status", "cancel"}, "job_failed_card")


def _test_diff_preview_card() -> list[CheckResult]:
    card = diff_preview_card(
        job_id="job-abc",
        diff_summary="src/parser.py | 12 +++++++---\ntests/...",
        changed_files=["src/parser.py"],
    )
    results = _check_card_shape(card, "diff_preview_card")
    return results + _check_button_values(card, {"status", "apply", "discard"}, "diff_preview_card")


def _test_confirm_action_card() -> list[CheckResult]:
    card = confirm_action_card(
        token="deadbeef1234",
        title="危险操作需确认",
        body="工具: service_restart\n目标单元: conveyor-telegram-bot",
    )
    results = _check_card_shape(card, "confirm_action_card")
    return results + _check_button_values(card, {"confirm", "cancel_confirm"}, "confirm_action_card")


def _test_status_card() -> list[CheckResult]:
    card = status_card(
        title="Status",
        fields=[("Latest", "job-abc · ok"), ("Success", "92%")],
        tone="green",
    )
    results = _check_card_shape(card, "status_card")
    # status_card has no action buttons by design.
    buttons = _collect_buttons(card)
    return results + [CheckResult(
        "status_card: no action buttons (no spam when not needed)",
        buttons == [],
        f"buttons={buttons}",
    )]


# ---- P5.0 execution-node card builders -----------------------------------


def _test_node_status_card() -> list[CheckResult]:
    card = node_status_card(
        "vps-main · Conveyor VPS · vps · online\n  capabilities: codex.run, ...",
    )
    results = _check_card_shape(card, "node_status_card")
    return results + _check_button_values(
        card, {"nodes_status", "computer_status"}, "node_status_card",
    )


def _test_computer_status_card() -> list[CheckResult]:
    card = computer_status_card(
        "🖥  Computer Use: 未启用 (stub)",
    )
    results = _check_card_shape(card, "computer_status_card")
    return results + _check_button_values(
        card, {"computer_status", "nodes_status"}, "computer_status_card",
    )


# ---- Action parsing tests --------------------------------------------------


def _test_parse_action_accepts_known_shapes() -> list[CheckResult]:
    cases = [
        ({"action": "status"}, {"action": "status"}),
        ({"action": "diff", "job_id": "abc"}, {"action": "diff", "job_id": "abc"}),
        ({"action": "apply", "job_id": "abc"}, {"action": "apply", "job_id": "abc"}),
        ({"action": "discard", "job_id": "abc"}, {"action": "discard", "job_id": "abc"}),
        ({"action": "cancel", "job_id": "abc"}, {"action": "cancel", "job_id": "abc"}),
        ({"action": "confirm", "token": "tk1"}, {"action": "confirm", "token": "tk1"}),
        ({"action": "cancel_confirm", "token": "tk1"}, {"action": "cancel_confirm", "token": "tk1"}),
        (json.dumps({"action": "diff", "job_id": "abc"}), {"action": "diff", "job_id": "abc"}),
    ]
    results: list[CheckResult] = []
    for raw, expected in cases:
        out = parse_action(raw)
        results.append(CheckResult(
            f"parse_action accepts {raw!r}",
            out == expected,
            f"got={out!r} expected={expected!r}",
        ))
    return results


def _test_parse_action_rejects_unknown() -> list[CheckResult]:
    cases = [
        None,
        "",
        "not-json",
        123,
        [],
        {},
        {"action": "rm-rf"},
        {"action": "status", "job_id": 123},  # wrong type for job_id
        {"action": "status", "token": 456},    # wrong type for token
        {"action": ""},
        {"action": None},
        {"action": "status", "job_id": ""},     # empty job_id is malformed
        {"action": "confirm", "token": ""},     # empty token is malformed
        {"action": "status", "token": "abc"},  # slash actions reject token
        {"action": "confirm"},                  # confirm requires token
        {"action": "diff", "extra": "x"},       # unknown field rejected
        # P5.0: nodes_status and computer_status are slash-style;
        # a token on them must be rejected by symmetry.
        {"action": "nodes_status", "token": "abc"},
        {"action": "computer_status", "token": "abc"},
    ]
    results: list[CheckResult] = []
    for raw in cases:
        out = parse_action(raw)
        results.append(CheckResult(
            f"parse_action rejects {raw!r}",
            out is None,
            f"got={out!r}",
        ))
    return results


def _test_parse_action_accepts_node_actions() -> list[CheckResult]:
    cases = [
        ({"action": "nodes_status"}, {"action": "nodes_status"}),
        ({"action": "computer_status"}, {"action": "computer_status"}),
    ]
    results: list[CheckResult] = []
    for raw, expected in cases:
        out = parse_action(raw)
        results.append(CheckResult(
            f"parse_action accepts {raw!r}",
            out == expected,
            f"got={out!r} expected={expected!r}",
        ))
    return results


def _test_action_allowlist_is_frozen() -> CheckResult:
    return CheckResult(
        "ALLOWED_ACTIONS is a frozenset (cannot be mutated by callers)",
        isinstance(ALLOWED_ACTIONS, frozenset),
        f"type={type(ALLOWED_ACTIONS).__name__}",
    )


def _test_action_to_command_mapping() -> list[CheckResult]:
    cases = [
        ("status", "status"),
        ("diff", "diff"),
        ("apply", "apply"),
        ("discard", "discard"),
        ("cancel", "cancel"),
        ("confirm", None),
        ("cancel_confirm", None),
        # P5.0: execution-node actions
        ("nodes_status", "nodes"),
        ("computer_status", "computer_status"),
        ("unknown", None),
    ]
    return [
        CheckResult(
            f"action_to_command({a!r}) -> {expected!r}",
            action_to_command(a) == expected,
            f"got={action_to_command(a)!r}",
        )
        for a, expected in cases
    ]


# ---- flatten_card_to_text tests -------------------------------------------


def _test_flatten_preserves_header_and_md() -> CheckResult:
    card = job_finished_card(
        job_id="job-abc",
        summary="All green.",
    )
    text = flatten_card_to_text(card)
    return CheckResult(
        "flatten_card_to_text: header + markdown body preserved",
        "Codex job finished" in text and "All green." in text,
        f"text head: {text[:200]!r}",
    )


def _test_flatten_truncates_long_bodies() -> CheckResult:
    card = status_card(
        title="Status",
        fields=[("Big", "x" * 5000)],
    )
    text = flatten_card_to_text(card)
    return CheckResult(
        "flatten_card_to_text: long bodies are truncated (no silent inflation)",
        len(text) <= 3500 and "truncated" in text,
        f"len={len(text)}",
    )


def _test_flatten_handles_empty() -> CheckResult:
    return CheckResult(
        "flatten_card_to_text: empty card returns empty string",
        flatten_card_to_text({}) == "",
        f"got={flatten_card_to_text({})!r}",
    )


# ---- End-to-end card action mapping tests --------------------------------


def _test_dispatch_known_actions() -> list[CheckResult]:
    """Verify action → (synthesized text, port calls) for safe actions."""
    from channel.feishu_cards import extract_card_action  # noqa: E402

    cases = [
        # (action_value, expected_action_label)
        ({"action": "status"}, "status"),
        ({"action": "diff", "job_id": "abc"}, "diff"),
        ({"action": "apply", "job_id": "abc"}, "apply"),
        ({"action": "discard", "job_id": "abc"}, "discard"),
        ({"action": "cancel", "job_id": "abc"}, "cancel"),
        ({"action": "confirm", "token": "tk1"}, "confirm"),
        ({"action": "cancel_confirm", "token": "tk1"}, "cancel_confirm"),
        # P5.0: execution-node actions
        ({"action": "nodes_status"}, "nodes_status"),
        ({"action": "computer_status"}, "computer_status"),
    ]
    results: list[CheckResult] = []
    for value, label in cases:
        msg = SimpleNamespace(
            event=SimpleNamespace(
                operator=SimpleNamespace(open_id="ou_user"),
                context=SimpleNamespace(open_chat_id="oc_chat", open_message_id="om_msg"),
                action=SimpleNamespace(value=value),
            )
        )
        out = extract_card_action(msg)
        if out is None:
            results.append(CheckResult(
                f"extract_card_action accepts {label}",
                False,
                "extraction returned None",
            ))
            continue
        identity, payload = out
        results.append(CheckResult(
            f"extract_card_action {label}: operator + chat parsed",
            identity["operator_id"] == "ou_user" and identity["chat_id"] == "oc_chat",
            f"identity={identity!r}",
        ))
        results.append(CheckResult(
            f"extract_card_action {label}: payload action={payload['action']!r}",
            payload["action"] == value["action"],
            f"got={payload!r}",
        ))
        if "token" in value:
            results.append(CheckResult(
                f"extract_card_action {label}: token preserved",
                payload.get("token") == value["token"],
                f"got token={payload.get('token')!r}",
            ))
        if "job_id" in value:
            results.append(CheckResult(
                f"extract_card_action {label}: job_id preserved",
                payload.get("job_id") == value["job_id"],
                f"got job_id={payload.get('job_id')!r}",
            ))
    return results


def _test_dispatch_rejects_unknown_action() -> list[CheckResult]:
    from channel.feishu_cards import extract_card_action  # noqa: E402

    bad_values: list[Any] = [
        None,
        {"action": "rm-rf"},
        {"action": 1},
    ]
    results: list[CheckResult] = []
    for value in bad_values:
        msg = SimpleNamespace(
            event=SimpleNamespace(
                operator=SimpleNamespace(open_id="ou_user"),
                context=SimpleNamespace(open_chat_id="oc_chat"),
                action=SimpleNamespace(value=value),
            )
        )
        out = extract_card_action(msg)
        results.append(CheckResult(
            f"extract_card_action rejects {value!r}",
            out is None,
            f"got={out!r}",
        ))
    return results


def _test_dispatch_rejects_missing_operator() -> list[CheckResult]:
    from channel.feishu_cards import extract_card_action  # noqa: E402

    msg = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id=None),
            context=SimpleNamespace(open_chat_id="oc_chat"),
            action=SimpleNamespace(value={"action": "status"}),
        )
    )
    out = extract_card_action(msg)
    return [CheckResult(
        "extract_card_action rejects missing operator.open_id",
        out is None,
        f"got={out!r}",
    )]


def _test_dispatch_rejects_missing_chat() -> list[CheckResult]:
    from channel.feishu_cards import extract_card_action  # noqa: E402

    msg = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_user"),
            context=SimpleNamespace(open_chat_id=None),
            action=SimpleNamespace(value={"action": "status"}),
        )
    )
    out = extract_card_action(msg)
    return [CheckResult(
        "extract_card_action rejects missing context.open_chat_id",
        out is None,
        f"got={out!r}",
    )]


def _test_dispatch_accepts_dict_form_event() -> list[CheckResult]:
    """Some lark-oapi versions pass the event as a dict, not a SimpleNamespace."""
    from channel.feishu_cards import extract_card_action  # noqa: E402

    msg = {
        "event": {
            "operator": {"open_id": "ou_user"},
            "context": {"open_chat_id": "oc_chat"},
            "action": {"value": {"action": "diff", "job_id": "abc"}},
        }
    }
    out = extract_card_action(msg)
    results: list[CheckResult] = []
    results.append(CheckResult(
        "extract_card_action accepts dict-form event",
        out is not None,
        f"got={out!r}",
    ))
    if out is not None:
        identity, payload = out
        results.append(CheckResult(
            "extract_card_action dict-form: operator + chat parsed",
            identity["operator_id"] == "ou_user" and identity["chat_id"] == "oc_chat",
            f"identity={identity!r}",
        ))
        results.append(CheckResult(
            "extract_card_action dict-form: payload action=diff job_id=abc",
            payload.get("action") == "diff" and payload.get("job_id") == "abc",
            f"payload={payload!r}",
        ))
    return results


def main() -> int:
    results: list[CheckResult] = []
    results += _test_job_started_card()
    results += _test_job_finished_card()
    results += _test_job_failed_card()
    results += _test_diff_preview_card()
    results += _test_confirm_action_card()
    results += _test_status_card()
    results += _test_node_status_card()
    results += _test_computer_status_card()
    results += _test_parse_action_accepts_known_shapes()
    results += _test_parse_action_accepts_node_actions()
    results += _test_parse_action_rejects_unknown()
    results.append(_test_action_allowlist_is_frozen())
    results += _test_action_to_command_mapping()
    results.append(_test_flatten_preserves_header_and_md())
    results.append(_test_flatten_truncates_long_bodies())
    results.append(_test_flatten_handles_empty())
    results += _test_dispatch_known_actions()
    results += _test_dispatch_rejects_unknown_action()
    results += _test_dispatch_rejects_missing_operator()
    results += _test_dispatch_rejects_missing_chat()
    results += _test_dispatch_accepts_dict_form_event()
    print_results(results)
    ok = all(r.ok for r in results)
    print("feishu cards smoke ok" if ok else "feishu cards smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
