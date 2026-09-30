"""Web-safe coordination for Secure Human Takeover.

The Web Console may create/activate a takeover lease and request that it close,
but it never starts or kills the remote-desktop transport itself. Transport
lifecycle belongs to ``handoff_sidecar.py`` running under systemd.

Only coordination metadata and a private-network URL are exposed. VNC
passwords, typed text, screenshots, clipboard contents, and other secret UI
material never enter this module's persisted state or API payloads.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from desktop_computer_requests import cancel_pending_computer_steps, has_claimed_computer_steps
from desktop_observe_requests import cancel_pending_observe_requests, has_claimed_observe_requests
from human_takeover import ALLOWED_REASONS, HumanTakeoverStore

CLOSE_ACTIONS = ("complete", "cancel")


def _state_root(settings: Any) -> Path:
    root = Path(settings.codex_memory_root) / "state"
    root.mkdir(parents=True, exist_ok=True)
    return root


def close_request_path(settings: Any) -> Path:
    return _state_root(settings) / "human_takeover_close.json"


def transport_gate_path(settings: Any) -> Path:
    return _state_root(settings) / "human_takeover_transport_ready.json"


def sidecar_status_path(settings: Any) -> Path:
    return _state_root(settings) / "human_takeover_sidecar.json"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    old_umask = os.umask(0o077)
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        os.umask(old_umask)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def read_close_request(settings: Any) -> dict[str, Any] | None:
    value = _read_json(close_request_path(settings))
    if not value:
        return None
    session_id = str(value.get("session_id") or "")
    action = str(value.get("action") or "")
    if not session_id or action not in CLOSE_ACTIONS:
        return None
    return {
        "session_id": session_id[:64],
        "action": action,
        "requested_at": float(value.get("requested_at") or 0),
    }


def clear_close_request(settings: Any, *, session_id: str | None = None) -> None:
    path = close_request_path(settings)
    if session_id:
        current = read_close_request(settings)
        if current and current.get("session_id") != session_id:
            return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def request_close(settings: Any, session_id: str, action: str) -> dict[str, Any]:
    session_id = str(session_id or "").strip()[:64]
    if not session_id:
        raise ValueError("takeover session_id is required")
    if action not in CLOSE_ACTIONS:
        raise ValueError("unsupported takeover close action")
    _atomic_json(
        close_request_path(settings),
        {
            "session_id": session_id,
            "action": action,
            "requested_at": time.time(),
        },
    )
    return read_close_request(settings) or {}


def read_transport_gate(settings: Any) -> dict[str, Any] | None:
    value = _read_json(transport_gate_path(settings))
    if not value:
        return None
    session_id = str(value.get("session_id") or "").strip()[:64]
    if not session_id:
        return None
    return {
        "session_id": session_id,
        "ready_at": float(value.get("ready_at") or 0),
    }


def allow_transport(settings: Any, session_id: str) -> dict[str, Any]:
    session_id = str(session_id or "").strip()[:64]
    if not session_id:
        raise ValueError("takeover session_id is required")
    _atomic_json(
        transport_gate_path(settings),
        {"session_id": session_id, "ready_at": time.time()},
    )
    return read_transport_gate(settings) or {}


def clear_transport_gate(settings: Any, *, session_id: str | None = None) -> None:
    path = transport_gate_path(settings)
    if session_id:
        current = read_transport_gate(settings)
        if current and current.get("session_id") != session_id:
            return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def read_sidecar_status(settings: Any) -> dict[str, Any]:
    value = _read_json(sidecar_status_path(settings)) or {}
    # Explicit allow-list. Never pass arbitrary sidecar output through to Web.
    return {
        "phase": str(value.get("phase") or "unknown")[:32],
        "running": bool(value.get("running")),
        "ready": bool(value.get("ready")),
        "url": str(value.get("url") or "")[:2048] or None,
        "local_url": str(value.get("local_url") or "")[:2048] or None,
        "error": str(value.get("error") or "")[:500] or None,
        "updated_at": float(value.get("updated_at") or 0),
    }


class WebTakeover:
    """Authenticated Web-facing takeover coordinator.

    ``start`` performs the same idle barrier as the operator CLI: pending
    computer/screenshot claims are cancelled, and an already-claimed operation
    must finish before the remote desktop may be opened. Only after that barrier
    succeeds is a session-scoped transport gate written for the systemd sidecar.
    """

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.store = HumanTakeoverStore(settings)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "conveyor_takeover_enabled", False))

    def status(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                "enabled": False,
                "takeover": None,
                "privacy_mode": False,
                "closing": None,
                "transport_allowed": False,
                "transport": None,
                "message": "Human takeover is disabled (set CONVEYOR_TAKEOVER_ENABLED=true)",
            }
        current = self.store.current()
        public = HumanTakeoverStore.public(current)
        close = read_close_request(self.settings)
        if close and (not current or close.get("session_id") != current.get("id")):
            clear_close_request(
                self.settings,
                session_id=str(close.get("session_id") or ""),
            )
            close = None
        gate = read_transport_gate(self.settings)
        if gate and (not current or gate.get("session_id") != current.get("id")):
            # The sidecar owns cleanup for a stale gate. Do not delete it here,
            # otherwise it could lose ownership of an old transport.
            gate = None
        transport = read_sidecar_status(self.settings)
        return {
            "enabled": True,
            "takeover": public,
            "privacy_mode": bool(public),
            "closing": close.get("action") if close else None,
            "transport_allowed": bool(
                current and gate and gate.get("session_id") == current.get("id")
            ),
            "transport": transport,
        }

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Human takeover is disabled (set CONVEYOR_TAKEOVER_ENABLED=true)")
        reason = str(payload.get("reason") or "operator_requested").strip().lower()
        if reason not in ALLOWED_REASONS:
            raise ValueError(f"unsupported takeover reason: {reason}")
        ttl = int(payload.get("ttl_seconds") or 300)
        task_id = str(payload.get("task_id") or "").strip() or None

        # A stale gate belongs to a previous Web-managed lease and must be
        # cleaned by the sidecar before a new lease can safely start.
        stale_gate = read_transport_gate(self.settings)
        if stale_gate:
            raise RuntimeError("a previous handoff transport still needs sidecar cleanup")

        result = self.store.start(
            reason=reason,
            task_id=task_id,
            requested_by="web-console",
            ttl_seconds=ttl,
        )
        clear_close_request(self.settings)

        # Establish exclusive GUI ownership before authorizing the sidecar to
        # open noVNC. This ordering prevents a human from entering while an
        # already-claimed Agent click/type/screenshot is still in flight.
        cancel_pending_computer_steps(self.settings)
        cancel_pending_observe_requests(self.settings)
        max_seconds = max(
            1,
            int(getattr(self.settings, "conveyor_computer_max_seconds", 120)),
        )
        deadline = time.monotonic() + max(30, max_seconds + 5)
        while has_claimed_computer_steps(self.settings) or has_claimed_observe_requests(
            self.settings
        ):
            current = self.store.current()
            if current is None or current.get("id") != result.get("id"):
                raise RuntimeError("takeover expired before the desktop became idle")
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "a computer-use action is still in flight; takeover remains open "
                    "and remote desktop stays closed"
                )
            time.sleep(0.1)
        current = self.store.current()
        if current is None or current.get("id") != result.get("id"):
            raise RuntimeError("takeover expired before the desktop became idle")

        allow_transport(self.settings, str(result["id"]))
        return self.status()

    def activate(self, session_id: str) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        gate = read_transport_gate(self.settings)
        if not gate or gate.get("session_id") != session_id:
            raise ValueError("takeover transport is not authorized for this session")
        result = self.store.activate(session_id)
        if result is None:
            current = self.store.current()
            if (
                not current
                or current.get("id") != session_id
                or current.get("state") != "human_active"
            ):
                raise ValueError("takeover session not found or invalid state")
        return self.status()

    def close(self, session_id: str, action: str) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        current = self.store.current()
        if not current or current.get("id") != session_id:
            raise ValueError("takeover session not found or invalid state")
        request_close(self.settings, session_id, action)
        # Lease deliberately remains open. Agent automation stays paused until
        # the sidecar verifies noVNC/Tailscale are gone and finalizes the lease.
        return self.status()
