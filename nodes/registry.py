"""nodes/registry.py — read-only registry of execution nodes.

Phase 0 scope: build a deterministic list of nodes from
:class:`config.Settings`. The VPS node is always present. The
desktop node is opt-in via env vars and defaults to absent so
Telegram/Feishu-only and VPS-only deploys continue to work without
configuration changes.

The registry is **process-local** and does not start any network
listener. Future Computer Use support will add a heartbeat path,
but in this task the desktop node is offline regardless of
whether it is registered — there is no agent that could be online.

Allowed imports: stdlib, ``config``. MUST NOT import the Telegram
SDK, lark_oapi, or anything from ``handlers/`` (it is consumed by
the agent tool layer; the dependency goes the other way).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Iterable, Mapping

from config import Settings

from nodes.types import (
    DESKTOP_STUB_CAPABILITIES,
    NodeInfo,
    NodeStatus,
    NodeType,
    TrustLevel,
    VPS_CAPABILITIES,
)

logger = logging.getLogger(__name__)


# Default identity for the VPS node. Matches the existing "Conveyor
# VPS" copy in the README/architecture docs.
_DEFAULT_VPS_NODE_ID = "vps-main"
_DEFAULT_VPS_DISPLAY_NAME = "Conveyor VPS"


# ---- Defaults for the desktop node ----------------------------------------

#: Default desktop node id when none is configured. We keep an
#: empty string as the "desktop is disabled" sentinel so existing
#: VPS-only deploys do not suddenly show a desktop row in
#: ``/nodes``.
DEFAULT_DESKTOP_NODE_ID = "macbook-payton"
DEFAULT_DESKTOP_NODE_NAME = "Payton MacBook"

#: Env var that opts into registering a desktop node at all. When
#: false / unset / unparseable, ``list_nodes()`` returns only the
#: VPS node. This is independent from ``CONVEYOR_DESKTOP_NODE_ID``
#: so a deploy can keep the env var for documentation but leave
#: the feature off.
DESKTOP_ENABLE_ENV = "CONVEYOR_DESKTOP_NODE_ENABLED"

#: Env var name for the desktop node id (overrides
#: :data:`DEFAULT_DESKTOP_NODE_ID`).
DESKTOP_ID_ENV = "CONVEYOR_DESKTOP_NODE_ID"

#: Env var name for the desktop node display name.
DESKTOP_NAME_ENV = "CONVEYOR_DESKTOP_NODE_NAME"

#: Default Computer Use mode for the desktop node. ``observe_only``
#: is the only safe default until a real local agent + step-by-step
#: confirmation flow exists. Future values (off / confirm_each /
#: confirm_destructive) must require a per-step confirmation token
#: in addition to the current chat binding.
DEFAULT_COMPUTER_USE_MODE = "observe_only"
COMPUTER_USE_MODE_ENV = "CONVEYOR_COMPUTER_USE_DEFAULT_MODE"


def _is_desktop_enabled(settings: Settings | None = None, env: Mapping[str, str] | None = None) -> bool:
    """True when the desktop node should appear in the registry."""
    if env is not None:
        raw = env.get(DESKTOP_ENABLE_ENV, "").strip().lower()
        return raw in ("true", "1", "yes", "on")
    if settings is not None:
        return settings.conveyor_desktop_node_enabled
    raw = os.environ.get(DESKTOP_ENABLE_ENV, "").strip().lower()
    return raw in ("true", "1", "yes", "on")


def _resolve_desktop_id(settings: Settings | None = None, env: Mapping[str, str] | None = None) -> str:
    if env is not None:
        raw = env.get(DESKTOP_ID_ENV, "").strip()
        return raw or DEFAULT_DESKTOP_NODE_ID
    if settings is not None:
        return settings.conveyor_desktop_node_id or DEFAULT_DESKTOP_NODE_ID
    raw = os.environ.get(DESKTOP_ID_ENV, "").strip()
    return raw or DEFAULT_DESKTOP_NODE_ID


def _resolve_desktop_name(settings: Settings | None = None, env: Mapping[str, str] | None = None) -> str:
    if env is not None:
        raw = env.get(DESKTOP_NAME_ENV, "").strip()
        return raw or DEFAULT_DESKTOP_NODE_NAME
    if settings is not None:
        return settings.conveyor_desktop_node_name or DEFAULT_DESKTOP_NODE_NAME
    raw = os.environ.get(DESKTOP_NAME_ENV, "").strip()
    return raw or DEFAULT_DESKTOP_NODE_NAME


def _resolve_computer_use_mode(settings: Settings | None = None, env: Mapping[str, str] | None = None) -> str:
    if env is not None:
        raw = env.get(COMPUTER_USE_MODE_ENV, "").strip().lower()
    elif settings is not None:
        raw = settings.conveyor_computer_use_default_mode.strip().lower()
    else:
        raw = os.environ.get(COMPUTER_USE_MODE_ENV, "").strip().lower()

    if not raw:
        return DEFAULT_COMPUTER_USE_MODE
    # Whitelist future modes to a known set so a typo in .env
    # does not silently enable a more permissive mode.
    if raw in ("observe_only", "off"):
        return raw
    logger.warning(
        "Unknown %s=%r, falling back to %r",
        COMPUTER_USE_MODE_ENV, raw, DEFAULT_COMPUTER_USE_MODE,
    )
    return DEFAULT_COMPUTER_USE_MODE


def build_default_vps_node(
    *,
    node_id: str = _DEFAULT_VPS_NODE_ID,
    display_name: str = _DEFAULT_VPS_DISPLAY_NAME,
) -> NodeInfo:
    """Construct the always-on VPS node.

    The VPS is online because the bot is running on it; there is
    no heartbeat protocol at the moment. ``last_seen_at`` is
    ``None`` (intentional — the VPS is the host itself).
    """
    return NodeInfo(
        node_id=node_id,
        display_name=display_name,
        node_type=NodeType.VPS,
        status=NodeStatus.ONLINE,
        last_seen_at=None,
        capabilities=VPS_CAPABILITIES,
        trust_level=TrustLevel.SERVER,
        metadata={"host_role": "control-plane"},
    )


def build_stub_desktop_node(
    *,
    node_id: str,
    display_name: str,
    computer_use_mode: str = DEFAULT_COMPUTER_USE_MODE,
) -> NodeInfo:
    """Construct the desktop stub node.

    Always offline in this task — there is no local agent, so the
    status would be misleading if it were ``online``. The
    capability list is the stub surface only; real
    ``browser.control`` / ``mouse.click`` / ``keyboard.type`` /
    ``computer_use.step`` are deferred to a future task that
    actually wires a local agent.
    """
    return NodeInfo(
        node_id=node_id,
        display_name=display_name,
        node_type=NodeType.DESKTOP,
        status=NodeStatus.OFFLINE,
        last_seen_at=None,
        capabilities=DESKTOP_STUB_CAPABILITIES,
        trust_level=TrustLevel.LOCAL_DESKTOP,
        metadata={
            "computer_use_mode": computer_use_mode,
            "agent_status": "not_registered",
            "note": (
                "Stub node: no local agent is wired in this task. "
                "Real desktop control is future work."
            ),
        },
    )


def list_nodes(
    settings: Settings | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> list[NodeInfo]:
    """Return the list of nodes known to this process.

    Order is stable: VPS first, then (optional) desktop. The
    function never raises; an invalid config produces a registry
    with only the VPS node (matching the historical behavior of
    the project: deploys that did not opt into a feature are not
    broken by it).
    """
    if env is None and settings is not None:
        from nodes.state import list_runtime_nodes
        return list_runtime_nodes(settings)

    nodes: list[NodeInfo] = [build_default_vps_node()]
    if not _is_desktop_enabled(settings, env):
        return nodes
    nodes.append(build_stub_desktop_node(
        node_id=_resolve_desktop_id(settings, env),
        display_name=_resolve_desktop_name(settings, env),
        computer_use_mode=_resolve_computer_use_mode(settings, env),
    ))
    return nodes


def get_node(node_id: str, settings: Settings | None = None) -> NodeInfo | None:
    """Return a single node by id, or ``None`` if unknown."""
    for node in list_nodes(settings):
        if node.node_id == node_id:
            return node
    return None


def find_nodes_with_capability(
    cap: str, settings: Settings | None = None
) -> list[NodeInfo]:
    """Return every online-or-offline node advertising ``cap``."""
    return [n for n in list_nodes(settings) if n.has_capability(cap)]


def online_node_ids(settings: Settings | None = None) -> tuple[str, ...]:
    """Stable tuple of node ids whose ``status`` is ``ONLINE``."""
    return tuple(
        n.node_id for n in list_nodes(settings) if n.status == NodeStatus.ONLINE
    )


def is_stub_environment(settings: Settings | None = None) -> bool:
    """True when no real desktop agent is wired in (always, for now).

    This is the single switch the future implementation flips
    once a heartbeat path lands. Code that branches on
    "is Computer Use active?" should call this predicate instead
    of inspecting environment variables directly so the contract
    stays in one place.
    """
    return True


__all__ = [
    "DESKTOP_ENABLE_ENV",
    "DESKTOP_ID_ENV",
    "DESKTOP_NAME_ENV",
    "COMPUTER_USE_MODE_ENV",
    "DEFAULT_DESKTOP_NODE_ID",
    "DEFAULT_DESKTOP_NODE_NAME",
    "DEFAULT_COMPUTER_USE_MODE",
    "build_default_vps_node",
    "build_stub_desktop_node",
    "list_nodes",
    "get_node",
    "find_nodes_with_capability",
    "online_node_ids",
    "is_stub_environment",
]


# Lightweight self-test: importing this module must not have side
# effects beyond logging configuration from the host process. The
# smoke scripts assert the registry shape; this is just a safety
# net so a syntax error is visible at import time.
_ = (time.time, Iterable)
