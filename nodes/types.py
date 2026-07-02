"""nodes/types.py — node model for the Conveyor control plane.

A *node* is an execution target the bot can route a natural-language
request to. The control plane always runs on the VPS, but the work
itself can be sent to:

- the VPS itself (Codex, server tools, scheduled jobs, web search, etc.)
- (future) a local desktop agent that drives the operator's MacBook
  for screenshot / mouse / keyboard / browser actions

This task adds the model and the read-only registry. The desktop
node is a **stub**: it is registered with ``status="offline"`` and
capabilities ``screen.screenshot``, ``desktop.observe``,
``computer_use.stub``. No screenshot, no mouse, no keyboard, no
browser control, no Gemini Computer Use call is implemented yet —
those require a local desktop agent that is **not** in this repo.
See ``docs/desktop_agent_protocol.md`` for the planned protocol
and ``docs/desktop_security.md`` for the safety contract.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class NodeType(str, enum.Enum):
    """Kind of execution target a node represents."""

    VPS = "vps"
    DESKTOP = "desktop"


class NodeStatus(str, enum.Enum):
    """Reachability / readiness for a node.

    - ``online``   : heartbeat fresh, tool calls are accepted
    - ``offline``  : last heartbeat is stale or never received
    - ``unknown``  : no heartbeat protocol in place yet (e.g. desktop
                     in this task; reserved for future telemetry)
    """

    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class TrustLevel(str, enum.Enum):
    """Trust relationship between the control plane and the node.

    - ``server``        : the control plane IS the node (VPS). The
                          sandbox, audit, and allowlist rules already
                          cover the operator.
    - ``local_desktop`` : the operator's personal machine. Future
                          desktop actions must require per-step
                          confirmation and produce an audit entry —
                          the model is "the operator is right there"
                          but the bot must still be predictable.
    """

    SERVER = "server"
    LOCAL_DESKTOP = "local_desktop"


# String capability tokens. ``str`` (not enum) so future / external
# nodes can register their own capabilities without a code change.
# The registry exposes helper predicates for the well-known ones
# (see :mod:`nodes.registry`).
NodeCapability = str


# ---- Well-known capabilities ----------------------------------------------

# VPS server-side capabilities (the Conveyor control plane).
CAP_CODEX_RUN = "codex.run"
CAP_GIT_DIFF = "git.diff"
CAP_GIT_APPLY = "git.apply"
CAP_SERVICE_STATUS = "service.status"
CAP_LOGS_READ = "logs.read"
CAP_SCHEDULER = "scheduler"
CAP_PERSONAL_TOOLS = "personal_tools"
CAP_WEB_FETCH = "web.fetch"
CAP_WEB_SEARCH = "web.search"
CAP_KB_SEARCH = "kb.search"
CAP_GITHUB = "github"

VPS_CAPABILITIES: tuple[NodeCapability, ...] = (
    CAP_CODEX_RUN,
    CAP_GIT_DIFF,
    CAP_GIT_APPLY,
    CAP_SERVICE_STATUS,
    CAP_LOGS_READ,
    CAP_SCHEDULER,
    CAP_PERSONAL_TOOLS,
    CAP_WEB_FETCH,
    CAP_WEB_SEARCH,
    CAP_KB_SEARCH,
    CAP_GITHUB,
)

# Desktop node STUB capabilities (no real desktop control implemented
# in this task). Once a real local agent is wired up these become
# real; until then the desktop node is offline and tool calls return
# a "not implemented" stub message.
CAP_SCREENSHOT = "screen.screenshot"
CAP_DESKTOP_OBSERVE = "desktop.observe"
CAP_COMPUTER_USE_STUB = "computer_use.stub"
CAP_BROWSER_CONTROL = "browser.control"
CAP_MOUSE_CLICK = "mouse.click"
CAP_KEYBOARD_TYPE = "keyboard.type"
CAP_COMPUTER_USE_STEP = "computer_use.step"

DESKTOP_STUB_CAPABILITIES: tuple[NodeCapability, ...] = (
    CAP_SCREENSHOT,
    CAP_DESKTOP_OBSERVE,
    CAP_COMPUTER_USE_STUB,
)

# Full desktop capability surface (future). Not exposed in this task
# because the desktop node is offline.
DESKTOP_FULL_CAPABILITIES: tuple[NodeCapability, ...] = (
    CAP_SCREENSHOT,
    CAP_DESKTOP_OBSERVE,
    CAP_BROWSER_CONTROL,
    CAP_MOUSE_CLICK,
    CAP_KEYBOARD_TYPE,
    CAP_COMPUTER_USE_STEP,
)


@dataclass(frozen=True)
class NodeInfo:
    """Static + dynamic description of one execution target.

    Fields are intentionally small and JSON-friendly. The registry
    holds a tiny live cache (last_seen_at, status) but the dataclass
    itself is immutable so a snapshot can be safely passed across
    the channel / handler boundary.

    ``metadata`` is a free-form ``dict`` for transport-specific
    details (desktop host:port, agent version, last known screen
    resolution, etc.). Keep it small and JSON-serialisable.
    """

    node_id: str
    display_name: str
    node_type: NodeType
    status: NodeStatus = NodeStatus.UNKNOWN
    last_seen_at: float | None = None
    capabilities: tuple[NodeCapability, ...] = field(default_factory=tuple)
    trust_level: TrustLevel = TrustLevel.SERVER
    metadata: dict[str, Any] = field(default_factory=dict)

    def has_capability(self, cap: NodeCapability) -> bool:
        return cap in self.capabilities


# ---- Text rendering helpers (channel-agnostic) ---------------------------


def format_node_line(node: NodeInfo) -> str:
    """Single-line human-readable node summary.

    Example::

        - vps-main · Conveyor VPS · vps · online
    """
    return (
        f"- {node.node_id} · {node.display_name} · "
        f"{node.node_type.value} · {node.status.value}"
    )


def format_node_block(node: NodeInfo) -> str:
    """Multi-line block: summary line + capability list.

    Used by ``nodes.status`` and the Feishu ``node_status_card``
    builder. Empty capability list is rendered as ``(no capabilities)``
    so the operator can tell stub nodes apart from real ones.
    """
    caps = ", ".join(node.capabilities) if node.capabilities else "(no capabilities)"
    return f"{format_node_line(node)}\n  capabilities: {caps}"


__all__ = [
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
]
