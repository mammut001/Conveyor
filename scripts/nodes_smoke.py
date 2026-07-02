#!/usr/bin/env python3
"""nodes_smoke.py — env-free tests for the execution-node layer (P5.0).

Covers:
  - Default registry contains only the VPS node.
  - Enabling ``CONVEYOR_DESKTOP_NODE_ENABLED`` adds a desktop node
    (always offline in phase 0).
  - Desktop node id / display name / computer use mode env vars
    override the defaults.
  - Invalid ``CONVEYOR_COMPUTER_USE_DEFAULT_MODE`` falls back to
    ``observe_only`` with a logged warning.
  - ``nodes.status`` and ``computer.status`` executors return text
    that explicitly says "not implemented" / "offline".
  - Intent router sends Mac/MacBook/desktop anchored phrases to
    ``computer.status`` (NOT Codex), so the operator never gets a
    fabricated answer about a screen the VPS cannot see.
  - Intent router sends "我的节点" / "机器状态" / "主机状态" to
    ``nodes.status``.
  - Feishu ``node_status_card`` / ``computer_status_card`` return
    valid card JSON.
  - Card action allowlist + action_to_command include the new
    ``nodes_status`` and ``computer_status`` actions and reject
    tokens on them (parity with existing slash-style actions).
  - Existing behavior is preserved: ambiguous "open Xcode"
    (no machine anchor) still falls through to Codex.

Run: .venv/bin/python scripts/nodes_smoke.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Minimal env so importing config.py at the helper layer does not
# require a real .env. Most of these tests do not import Settings
# at all; we still set them defensively for the executor tests.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/test_nodes_workspace")
os.environ.setdefault("CODEX_MEMORY_ROOT", "/tmp/test_nodes_memory")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "1")

from scripts.harness_common import CheckResult, print_results  # noqa: E402

FAILURES: list[str] = []


def _fail(name: str, detail: str) -> None:
    print(f"[fail] {name}: {detail}")
    FAILURES.append(name)


# ---- Registry: defaults ---------------------------------------------------


def _test_default_registry_has_vps_only() -> None:
    from nodes.registry import list_nodes
    from nodes.types import NodeType, NodeStatus

    nodes = list_nodes()
    if not nodes:
        _fail("default_registry_has_vps_only", "registry empty")
        return
    if len(nodes) != 1:
        _fail("default_registry_has_vps_only", f"len={len(nodes)} nodes={nodes}")
        return
    vps = nodes[0]
    if vps.node_id != "vps-main":
        _fail("default_registry_has_vps_only", f"id={vps.node_id}")
        return
    if vps.node_type != NodeType.VPS:
        _fail("default_registry_has_vps_only", f"type={vps.node_type}")
        return
    if vps.status != NodeStatus.ONLINE:
        _fail("default_registry_has_vps_only", f"status={vps.status}")
        return
    if "codex.run" not in vps.capabilities:
        _fail("default_registry_has_vps_only", f"caps={vps.capabilities}")
        return
    print("[pass] default_registry_has_vps_only")


def _test_desktop_node_absent_by_default() -> None:
    from nodes.registry import list_nodes
    from nodes.types import NodeType

    nodes = list_nodes()
    desktop = [n for n in nodes if n.node_type == NodeType.DESKTOP]
    if desktop:
        _fail("desktop_node_absent_by_default", f"got={desktop}")
        return
    print("[pass] desktop_node_absent_by_default")


# ---- Registry: env-var driven -------------------------------------------


def _test_desktop_node_enabled_via_env(monkeypatch: dict[str, str]) -> None:
    from nodes.registry import list_nodes
    from nodes.types import NodeStatus, NodeType

    nodes = list_nodes(env=monkeypatch)
    desktop = [n for n in nodes if n.node_type == NodeType.DESKTOP]
    if len(desktop) != 1:
        _fail("desktop_node_enabled_via_env", f"desktop={desktop}")
        return
    if desktop[0].status != NodeStatus.OFFLINE:
        _fail("desktop_node_enabled_via_env", f"status={desktop[0].status}")
        return
    if "screen.screenshot" not in desktop[0].capabilities:
        _fail("desktop_node_enabled_via_env", f"caps={desktop[0].capabilities}")
        return
    if "browser.control" in desktop[0].capabilities:
        _fail(
            "desktop_node_enabled_via_env",
            "real desktop capabilities leaked into stub",
        )
        return
    print("[pass] desktop_node_enabled_via_env")


def _test_desktop_node_env_overrides(monkeypatch: dict[str, str]) -> None:
    from nodes.registry import list_nodes
    from nodes.types import NodeType

    nodes = list_nodes(env=monkeypatch)
    desktop = [n for n in nodes if n.node_type == NodeType.DESKTOP]
    if len(desktop) != 1:
        _fail("desktop_node_env_overrides", f"desktop={desktop}")
        return
    if desktop[0].node_id != "my-mbp":
        _fail("desktop_node_env_overrides", f"id={desktop[0].node_id}")
        return
    if desktop[0].display_name != "My MacBook Pro":
        _fail("desktop_node_env_overrides", f"name={desktop[0].display_name}")
        return
    if desktop[0].metadata.get("computer_use_mode") != "off":
        _fail(
            "desktop_node_env_overrides",
            f"mode={desktop[0].metadata.get('computer_use_mode')}",
        )
        return
    print("[pass] desktop_node_env_overrides")


def _test_invalid_computer_use_mode_falls_back(monkeypatch: dict[str, str]) -> None:
    """Unknown mode must fall back to observe_only (logged warning)."""
    from nodes.registry import list_nodes
    from nodes.types import NodeType

    nodes = list_nodes(env=monkeypatch)
    desktop = [n for n in nodes if n.node_type == NodeType.DESKTOP]
    if len(desktop) != 1:
        _fail("invalid_computer_use_mode_falls_back", f"desktop={desktop}")
        return
    if desktop[0].metadata.get("computer_use_mode") != "observe_only":
        _fail(
            "invalid_computer_use_mode_falls_back",
            f"mode={desktop[0].metadata.get('computer_use_mode')}",
        )
        return
    print("[pass] invalid_computer_use_mode_falls_back")


def _test_is_stub_environment_default() -> None:
    from nodes.registry import is_stub_environment

    if not is_stub_environment():
        _fail("is_stub_environment_default", "expected True in phase 0")
        return
    print("[pass] is_stub_environment_default")


# ---- Executors ------------------------------------------------------------


def _test_exec_nodes_status_text() -> None:
    """The /nodes tool output is text + contains vps-main."""
    import asyncio

    from config import Settings
    from handlers.tools.executors import exec_nodes_status

    settings = Settings(
        telegram_bot_token="t", telegram_allowed_user_id=1,
        codex_workspace_root=Path("/tmp"),
        codex_bin="codex",
        codex_task_root=Path("/tmp/t"),
        codex_model=None,
        codex_timeout_seconds=3600,
        telegram_progress_seconds=3,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=Path("/tmp/m"),
        user_timezone="UTC",
    )
    text = asyncio.run(exec_nodes_status(settings, ""))
    if "vps-main" not in text:
        _fail("exec_nodes_status_text", f"missing vps-main: {text[:200]!r}")
        return
    if "Conveyor VPS" not in text:
        _fail("exec_nodes_status_text", f"missing display name: {text[:200]!r}")
        return
    if "codex.run" not in text:
        _fail("exec_nodes_status_text", f"missing cap: {text[:200]!r}")
        return
    print("[pass] exec_nodes_status_text")


def _test_exec_computer_status_stub() -> None:
    """The /computer_status tool explicitly says not implemented."""
    import asyncio

    from config import Settings
    from handlers.tools.executors import exec_computer_status

    settings = Settings(
        telegram_bot_token="t", telegram_allowed_user_id=1,
        codex_workspace_root=Path("/tmp"),
        codex_bin="codex",
        codex_task_root=Path("/tmp/t"),
        codex_model=None,
        codex_timeout_seconds=3600,
        telegram_progress_seconds=3,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=Path("/tmp/m"),
        user_timezone="UTC",
    )
    text = asyncio.run(exec_computer_status(settings, ""))
    if "未实现" not in text and "未启用" not in text and "not implemented" not in text.lower():
        _fail("exec_computer_status_stub", f"text={text[:200]!r}")
        return
    if "未触发" not in text and "没有触发" not in text and "no desktop action" not in text.lower():
        _fail("exec_computer_status_stub", "did not say 'no action was performed'")
        return
    print("[pass] exec_computer_status_stub")


# ---- Intent router --------------------------------------------------------


def _test_intent_nodes_status_routes() -> None:
    """'我的节点' / '机器状态' / '主机状态' → nodes.status."""
    from handlers.intent import route_intent

    for phrase in ("我的节点", "机器状态", "主机状态", "nodes status", "node status"):
        result = route_intent(phrase)
        if result.kind != "deterministic":
            _fail("intent_nodes_status_routes", f"{phrase!r} kind={result.kind}")
            return
        if "nodes.status" not in result.tools:
            _fail("intent_nodes_status_routes", f"{phrase!r} tools={result.tools}")
            return
    print("[pass] intent_nodes_status_routes")


def _test_intent_computer_use_routes_to_stub() -> None:
    """Desktop anchored phrases → computer.status, NOT Codex."""
    from handlers.intent import route_intent

    cases = [
        "帮我在 Mac 上打开 Xcode",
        "操作我的电脑",
        "用我的 Mac 打开浏览器",
        "看一下我 MacBook 上的 Xcode",
        "computer use my Mac",
    ]
    for phrase in cases:
        result = route_intent(phrase)
        if result.kind != "deterministic":
            _fail(
                "intent_computer_use_routes_to_stub",
                f"{phrase!r} kind={result.kind} (must not fall to Codex)",
            )
            return
        if "computer.status" not in result.tools:
            _fail(
                "intent_computer_use_routes_to_stub",
                f"{phrase!r} tools={result.tools}",
            )
            return
    print("[pass] intent_computer_use_routes_to_stub")


def _test_intent_screenshot_observe_routes() -> None:
    from handlers.intent import route_intent

    cases = [
        "截图看看我电脑现在是什么",
        "看一下 MacBook 屏幕",
        "take a screenshot on my desktop",
    ]
    for phrase in cases:
        result = route_intent(phrase)
        if result.kind != "deterministic":
            _fail("intent_screenshot_observe_routes", f"{phrase!r} kind={result.kind}")
            return
        if "desktop.screenshot.status" not in result.tools:
            _fail("intent_screenshot_observe_routes", f"{phrase!r} tools={result.tools}")
            return
    print("[pass] intent_screenshot_observe_routes")


def _test_intent_ambiguous_open_xcode_not_hijacked() -> None:
    """Without a machine anchor, 'open Xcode' is still coding intent."""
    from handlers.intent import route_intent

    result = route_intent("open Xcode")
    # "open Xcode" alone has no "my Mac" / "MacBook" / "desktop" anchor,
    # so it must fall through to llm (Codex). The point of the new
    # pattern set is to NOT hijack plain coding requests.
    if result.kind == "deterministic" and result.tools and "computer.status" in result.tools:
        _fail(
            "intent_ambiguous_open_xcode_not_hijacked",
            f"tools={result.tools} (computer.status must not match without anchor)",
        )
        return
    print("[pass] intent_ambiguous_open_xcode_not_hijacked")


# ---- Feishu card builders -------------------------------------------------


def _test_node_status_card_shape() -> None:
    from channel.feishu_cards import node_status_card, parse_action

    card = node_status_card("vps-main · Conveyor VPS · vps · online")
    cfg = card.get("config") or {}
    if not cfg.get("wide_screen_mode"):
        _fail("node_status_card_shape", f"cfg={cfg}")
        return
    elements = card.get("elements") or []
    if not elements:
        _fail("node_status_card_shape", "empty elements")
        return
    # Find the action row
    action_row = next((el for el in elements if isinstance(el, dict) and el.get("tag") == "action"), None)
    if action_row is None:
        _fail("node_status_card_shape", "no action row")
        return
    button_actions = []
    for btn in action_row.get("actions") or []:
        if not isinstance(btn, dict):
            continue
        payload = parse_action(btn.get("value"))
        if payload is None:
            _fail("node_status_card_shape", f"bad button value: {btn!r}")
            return
        button_actions.append(payload["action"])
    for required in ("nodes_status", "computer_status"):
        if required not in button_actions:
            _fail("node_status_card_shape", f"missing {required} in {button_actions}")
            return
    print("[pass] node_status_card_shape")


def _test_computer_status_card_shape() -> None:
    from channel.feishu_cards import computer_status_card, parse_action

    card = computer_status_card("🖥  Computer Use: 未启用")
    elements = card.get("elements") or []
    action_row = next((el for el in elements if isinstance(el, dict) and el.get("tag") == "action"), None)
    if action_row is None:
        _fail("computer_status_card_shape", "no action row")
        return
    seen = set()
    for btn in action_row.get("actions") or []:
        if not isinstance(btn, dict):
            continue
        payload = parse_action(btn.get("value"))
        if payload is None:
            _fail("computer_status_card_shape", f"bad value: {btn!r}")
            return
        seen.add(payload["action"])
    if "computer_status" not in seen:
        _fail("computer_status_card_shape", f"missing refresh: {seen}")
        return
    print("[pass] computer_status_card_shape")


def _test_action_allowlist_includes_node_actions() -> None:
    from channel.feishu_cards import ALLOWED_ACTIONS, action_to_command, parse_action

    if "nodes_status" not in ALLOWED_ACTIONS:
        _fail("action_allowlist_includes_node_actions", f"set={ALLOWED_ACTIONS}")
        return
    if "computer_status" not in ALLOWED_ACTIONS:
        _fail("action_allowlist_includes_node_actions", f"set={ALLOWED_ACTIONS}")
        return
    if action_to_command("nodes_status") != "nodes":
        _fail("action_allowlist_includes_node_actions", f"mapping nodes_status->{action_to_command('nodes_status')!r}")
        return
    if action_to_command("computer_status") != "computer_status":
        _fail("action_allowlist_includes_node_actions", f"mapping computer_status->{action_to_command('computer_status')!r}")
        return
    # Token must be rejected on these slash-style actions.
    payload = parse_action({"action": "nodes_status", "token": "abc"})
    if payload is not None:
        _fail("action_allowlist_includes_node_actions", f"token leaked: {payload}")
        return
    payload = parse_action({"action": "computer_status", "token": "abc"})
    if payload is not None:
        _fail("action_allowlist_includes_node_actions", f"token leaked: {payload}")
        return
    print("[pass] action_allowlist_includes_node_actions")


# ---- Config: env wiring --------------------------------------------------


def _test_settings_carries_node_fields() -> None:
    """Settings loads the new fields from _load_codex_fields."""
    from config import _load_codex_fields

    fields = _load_codex_fields("/dev/null")
    for key in (
        "conveyor_desktop_node_enabled",
        "conveyor_desktop_node_id",
        "conveyor_desktop_node_name",
        "conveyor_desktop_agent_token",
        "conveyor_computer_use_default_mode",
    ):
        if key not in fields:
            _fail("settings_carries_node_fields", f"missing {key}")
            return
    if fields["conveyor_desktop_node_enabled"] is not False:
        _fail("settings_carries_node_fields", f"enabled default={fields['conveyor_desktop_node_enabled']!r}")
        return
    if fields["conveyor_computer_use_default_mode"] != "observe_only":
        _fail("settings_carries_node_fields", f"mode default={fields['conveyor_computer_use_default_mode']!r}")
        return
    print("[pass] settings_carries_node_fields")


def _test_sensitive_fields_includes_agent_token() -> None:
    from config import SENSITIVE_FIELDS

    if "conveyor_desktop_agent_token" not in SENSITIVE_FIELDS:
        _fail("sensitive_fields_includes_agent_token", f"set={SENSITIVE_FIELDS}")
        return
    print("[pass] sensitive_fields_includes_agent_token")


# ---- Run ----------------------------------------------------------------


def main() -> int:
    _test_default_registry_has_vps_only()
    _test_desktop_node_absent_by_default()
    _test_desktop_node_enabled_via_env({
        "CONVEYOR_DESKTOP_NODE_ENABLED": "true",
    })
    _test_desktop_node_env_overrides({
        "CONVEYOR_DESKTOP_NODE_ENABLED": "true",
        "CONVEYOR_DESKTOP_NODE_ID": "my-mbp",
        "CONVEYOR_DESKTOP_NODE_NAME": "My MacBook Pro",
        "CONVEYOR_COMPUTER_USE_DEFAULT_MODE": "off",
    })
    _test_invalid_computer_use_mode_falls_back({
        "CONVEYOR_DESKTOP_NODE_ENABLED": "true",
        "CONVEYOR_COMPUTER_USE_DEFAULT_MODE": "permissive",
    })
    _test_is_stub_environment_default()
    _test_exec_nodes_status_text()
    _test_exec_computer_status_stub()
    _test_intent_nodes_status_routes()
    _test_intent_computer_use_routes_to_stub()
    _test_intent_screenshot_observe_routes()
    _test_intent_ambiguous_open_xcode_not_hijacked()
    _test_node_status_card_shape()
    _test_computer_status_card_shape()
    _test_action_allowlist_includes_node_actions()
    _test_settings_carries_node_fields()
    _test_sensitive_fields_includes_agent_token()

    total = 17
    failed = len(FAILURES)
    passed = total - failed
    print(f"\n{'=' * 60}")
    print(f"Nodes smoke: {passed}/{total} passed")
    if FAILURES:
        print(f"FAILURES: {', '.join(FAILURES)}")
        return 1
    print("All tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
