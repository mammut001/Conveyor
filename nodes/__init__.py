"""nodes — execution node model + registry for the Conveyor control plane.

This package holds the read-only "what machines can the bot route
work to?" layer. Phase 0 ships:

- :mod:`nodes.types` — ``NodeType``, ``NodeStatus``, ``TrustLevel``,
  ``NodeInfo``, well-known capability constants, and text helpers.
- :mod:`nodes.registry` — process-local list of nodes derived from
  ``Settings`` / env vars. The VPS node is always present; the
  desktop node is opt-in via ``CONVEYOR_DESKTOP_NODE_ENABLED`` and
  is offline regardless (no local agent is wired in this task).

Future Computer Use work will extend :mod:`nodes.registry` with
heartbeat ingestion; the surface in this task is intentionally
small and JSON-friendly so a future agent can post the same shape
back.
"""
from __future__ import annotations

from nodes.registry import (
    COMPUTER_USE_MODE_ENV,
    DEFAULT_COMPUTER_USE_MODE,
    DEFAULT_DESKTOP_NODE_ID,
    DEFAULT_DESKTOP_NODE_NAME,
    DESKTOP_ENABLE_ENV,
    DESKTOP_ID_ENV,
    DESKTOP_NAME_ENV,
    build_default_vps_node,
    build_stub_desktop_node,
    find_nodes_with_capability,
    get_node,
    is_stub_environment,
    list_nodes,
    online_node_ids,
)
from nodes.types import (
    CAP_BROWSER_CONTROL,
    CAP_CODEX_RUN,
    CAP_COMPUTER_USE_STEP,
    CAP_COMPUTER_USE_STUB,
    CAP_DESKTOP_OBSERVE,
    CAP_GITHUB,
    CAP_GIT_APPLY,
    CAP_GIT_DIFF,
    CAP_KB_SEARCH,
    CAP_KEYBOARD_TYPE,
    CAP_LOGS_READ,
    CAP_MOUSE_CLICK,
    CAP_PERSONAL_TOOLS,
    CAP_SCHEDULER,
    CAP_SCREENSHOT,
    CAP_SERVICE_STATUS,
    CAP_WEB_FETCH,
    CAP_WEB_SEARCH,
    DESKTOP_FULL_CAPABILITIES,
    DESKTOP_STUB_CAPABILITIES,
    NodeCapability,
    NodeInfo,
    NodeStatus,
    NodeType,
    TrustLevel,
    VPS_CAPABILITIES,
    format_node_block,
    format_node_line,
)

__all__ = [
    # types
    "NodeType",
    "NodeStatus",
    "TrustLevel",
    "NodeCapability",
    "NodeInfo",
    "VPS_CAPABILITIES",
    "DESKTOP_STUB_CAPABILITIES",
    "DESKTOP_FULL_CAPABILITIES",
    "CAP_CODEX_RUN",
    "CAP_GIT_DIFF",
    "CAP_GIT_APPLY",
    "CAP_SERVICE_STATUS",
    "CAP_LOGS_READ",
    "CAP_SCHEDULER",
    "CAP_PERSONAL_TOOLS",
    "CAP_WEB_FETCH",
    "CAP_WEB_SEARCH",
    "CAP_KB_SEARCH",
    "CAP_GITHUB",
    "CAP_SCREENSHOT",
    "CAP_DESKTOP_OBSERVE",
    "CAP_COMPUTER_USE_STUB",
    "CAP_BROWSER_CONTROL",
    "CAP_MOUSE_CLICK",
    "CAP_KEYBOARD_TYPE",
    "CAP_COMPUTER_USE_STEP",
    "format_node_line",
    "format_node_block",
    # registry
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
