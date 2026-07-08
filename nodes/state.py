"""nodes/state.py — shared file-backed state for registered desktop nodes."""
from __future__ import annotations

import json
import os
import time
import threading
from pathlib import Path
from typing import Any

from config import Settings
from nodes.types import (
    DESKTOP_DIRECT_CAPABILITIES,
    DESKTOP_STUB_CAPABILITIES,
    NodeInfo,
    NodeStatus,
    NodeType,
    TrustLevel,
)


def _desktop_capabilities(settings: Settings) -> tuple[str, ...]:
    """Return the desktop node capability set for the current config.

    Stub unless CONVEYOR_COMPUTER_USE_ENABLED is set; the direct
    surface is never advertised implicitly.
    """
    if getattr(settings, "conveyor_computer_use_enabled", False):
        return DESKTOP_DIRECT_CAPABILITIES
    return DESKTOP_STUB_CAPABILITIES

_lock = threading.Lock()


def _safe_str(value: Any, max_len: int) -> str:
    """Ensure value is a string and is truncated to max_len."""
    if not isinstance(value, str):
        value = str(value) if value is not None else ""
    return value[:max_len]


def _safe_host_info(host: Any) -> dict[str, str]:
    """Sanitize host dictionary to contain only safe string fields platform/hostname/arch."""
    if not isinstance(host, dict):
        return {}
    sanitized = {}
    for k in ["platform", "hostname", "arch"]:
        v = host.get(k)
        if v is not None:
            sanitized[k] = _safe_str(v, 128)
    return sanitized


def desktop_state_path(settings: Settings) -> Path:
    """Return the Path to the shared desktop nodes JSON file."""
    return settings.codex_memory_root / "state" / "desktop_nodes.json"


def load_desktop_state(settings: Settings) -> dict[str, Any]:
    """Load the shared desktop nodes state, handling corrupt JSON or missing file gracefully."""
    path = desktop_state_path(settings)
    if not path.exists():
        return {}
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return {}
        data = json.loads(content)
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        # Corrupt JSON or read error: treat as empty and do not crash
        return {}


def save_desktop_state(settings: Settings, state: dict[str, Any]) -> None:
    """Save the state atomically to the shared JSON file."""
    path = desktop_state_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # Avoid leaving a temp file or raising a crash if write fails
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise


def register_desktop_node(
    settings_or_node_id: Settings | str,
    *args,
    **kwargs
) -> NodeInfo:
    """Register a new desktop node and record its initial status in the shared state file."""
    if isinstance(settings_or_node_id, Settings):
        settings = settings_or_node_id
        node_id = args[0] if len(args) > 0 else kwargs.pop("node_id")
        display_name = args[1] if len(args) > 1 else kwargs.pop("display_name")
        agent_version = args[2] if len(args) > 2 else kwargs.pop("agent_version")
        host_info = args[3] if len(args) > 3 else kwargs.pop("host_info")
        now = args[4] if len(args) > 4 else kwargs.get("now", None)
    else:
        from config import load_settings
        settings = load_settings()
        node_id = settings_or_node_id
        display_name = args[0] if len(args) > 0 else kwargs.pop("display_name")
        agent_version = args[1] if len(args) > 1 else kwargs.pop("agent_version")
        host_info = args[2] if len(args) > 2 else kwargs.pop("host_info")
        now = args[3] if len(args) > 3 else kwargs.get("now", None)

    if now is None:
        now = time.time()

    # Sanitize inputs
    node_id_str = _safe_str(node_id, 128)
    display_name_str = _safe_str(display_name, 100)
    agent_version_str = _safe_str(agent_version, 64)
    sanitized_host = _safe_host_info(host_info)

    with _lock:
        state = load_desktop_state(settings)
        node_state = {
            "node_id": node_id_str,
            "display_name": display_name_str,
            "agent_version": agent_version_str,
            "host": sanitized_host,
            "last_seen_at": now,
            "agent_state": "registered",
            "last_action": "register",
        }
        state[node_id_str] = node_state
        save_desktop_state(settings, state)

    return NodeInfo(
        node_id=node_id_str,
        display_name=display_name_str,
        node_type=NodeType.DESKTOP,
        status=NodeStatus.ONLINE,
        last_seen_at=now,
        capabilities=_desktop_capabilities(settings),
        trust_level=TrustLevel.LOCAL_DESKTOP,
        metadata={
            "agent_version": agent_version_str,
            "host": sanitized_host,
            "agent_state": "registered",
            "last_action": "register",
        },
    )


def record_heartbeat(
    settings_or_node_id: Settings | str,
    *args,
    **kwargs
) -> NodeInfo | None:
    """Update last_seen_at and state for a registered desktop node in the shared JSON state."""
    if isinstance(settings_or_node_id, Settings):
        settings = settings_or_node_id
        node_id = args[0] if len(args) > 0 else kwargs.pop("node_id")
        agent_state = args[1] if len(args) > 1 else kwargs.pop("agent_state")
        last_action = args[2] if len(args) > 2 else kwargs.get("last_action", None)
        now = args[3] if len(args) > 3 else kwargs.get("now", None)
    else:
        from config import load_settings
        settings = load_settings()
        node_id = settings_or_node_id
        agent_state = args[0] if len(args) > 0 else kwargs.pop("agent_state")
        last_action = args[1] if len(args) > 1 else kwargs.get("last_action", None)
        now = args[2] if len(args) > 2 else kwargs.get("now", None)

    if now is None:
        now = time.time()

    node_id_str = _safe_str(node_id, 128)
    agent_state_str = _safe_str(agent_state, 64)
    last_action_str = _safe_str(last_action, 128) if last_action is not None else None

    with _lock:
        state = load_desktop_state(settings)
        node_state = state.get(node_id_str)
        if not isinstance(node_state, dict):
            return None

        node_state["last_seen_at"] = now
        node_state["agent_state"] = agent_state_str
        if last_action_str is not None:
            node_state["last_action"] = last_action_str
        else:
            node_state["last_action"] = "heartbeat"

        save_desktop_state(settings, state)

        display_name = _safe_str(node_state.get("display_name"), 100)
        agent_version = _safe_str(node_state.get("agent_version"), 64)
        host_info = _safe_host_info(node_state.get("host"))
        last_action_val = node_state["last_action"]

    return NodeInfo(
        node_id=node_id_str,
        display_name=display_name,
        node_type=NodeType.DESKTOP,
        status=NodeStatus.ONLINE,
        last_seen_at=now,
        capabilities=_desktop_capabilities(settings),
        trust_level=TrustLevel.LOCAL_DESKTOP,
        metadata={
            "agent_version": agent_version,
            "host": host_info,
            "agent_state": agent_state_str,
            "last_action": last_action_val,
        },
    )


def get_desktop_runtime(
    settings_or_node_id: Settings | str,
    node_id: str | None = None,
) -> dict[str, Any] | None:
    """Get the runtime state dict for a node from the shared JSON state file."""
    if isinstance(settings_or_node_id, Settings):
        settings = settings_or_node_id
        target_node_id = node_id
    else:
        from config import load_settings
        settings = load_settings()
        target_node_id = settings_or_node_id

    target_node_id_str = _safe_str(target_node_id, 128)

    with _lock:
        state = load_desktop_state(settings)
        node_state = state.get(target_node_id_str)
        if not isinstance(node_state, dict):
            return None
        return dict(node_state)


def is_desktop_online(
    settings_or_node_id: Settings | str,
    *args,
    **kwargs
) -> bool:
    """True if the node has registered and its last seen time is within the TTL."""
    try:
        if isinstance(settings_or_node_id, Settings):
            settings = settings_or_node_id
            node_id = args[0] if len(args) > 0 else kwargs.pop("node_id")
            now = args[1] if len(args) > 1 else kwargs.get("now", None)
            ttl_seconds = args[2] if len(args) > 2 else kwargs.get("ttl_seconds", None)
        else:
            from config import load_settings
            settings = load_settings()
            node_id = settings_or_node_id
            now = args[0] if len(args) > 0 else kwargs.get("now", None)
            ttl_seconds = args[1] if len(args) > 1 else kwargs.get("ttl_seconds", None)

        if now is None:
            now = time.time()
        if ttl_seconds is None:
            ttl_seconds = float(settings.conveyor_desktop_heartbeat_ttl_seconds)

        node_id_str = _safe_str(node_id, 128)

        with _lock:
            state = load_desktop_state(settings)
            node_state = state.get(node_id_str)
            if not isinstance(node_state, dict):
                return False
            last_seen = node_state.get("last_seen_at")
            try:
                last_seen_val = float(last_seen)
            except (TypeError, ValueError):
                return False
            return (now - last_seen_val) <= ttl_seconds
    except Exception:
        return False


def list_runtime_nodes(settings: Settings, now: float | None = None) -> list[NodeInfo]:
    """List VPS and (optional) desktop node, integrating live heartbeat status."""
    if now is None:
        now = time.time()

    from nodes.registry import build_default_vps_node, build_stub_desktop_node

    nodes = [build_default_vps_node()]

    if not settings.conveyor_desktop_node_enabled:
        return nodes

    node_id = settings.conveyor_desktop_node_id or "macbook-payton"
    display_name = settings.conveyor_desktop_node_name or "Payton MacBook"
    computer_use_mode = settings.conveyor_computer_use_default_mode or "observe_only"
    ttl_seconds = settings.conveyor_desktop_heartbeat_ttl_seconds

    node_id_str = _safe_str(node_id, 128)
    state = get_desktop_runtime(settings, node_id_str)
    if not isinstance(state, dict):
        nodes.append(build_stub_desktop_node(
            node_id=node_id_str,
            display_name=display_name,
            computer_use_mode=computer_use_mode,
        ))
    else:
        online = is_desktop_online(settings, node_id_str, now=now, ttl_seconds=float(ttl_seconds))
        status = NodeStatus.ONLINE if online else NodeStatus.OFFLINE
        
        disp_name = _safe_str(state.get("display_name"), 100) or display_name
        
        last_seen = state.get("last_seen_at")
        try:
            last_seen_val = float(last_seen)
        except (TypeError, ValueError):
            last_seen_val = 0.0

        nodes.append(NodeInfo(
            node_id=node_id_str,
            display_name=disp_name,
            node_type=NodeType.DESKTOP,
            status=status,
            last_seen_at=last_seen_val if last_seen_val > 0 else None,
            capabilities=_desktop_capabilities(settings),
            trust_level=TrustLevel.LOCAL_DESKTOP,
            metadata={
                "agent_version": _safe_str(state.get("agent_version"), 64),
                "host": _safe_host_info(state.get("host")),
                "agent_state": _safe_str(state.get("agent_state"), 64),
                "last_action": _safe_str(state.get("last_action"), 128),
                "computer_use_mode": computer_use_mode,
            },
        ))

    return nodes
