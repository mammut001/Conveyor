"""Private, atomic Web Console secret store. Never return values from listing."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_LOCK = threading.RLock()
MAX_VALUE = 8192


class SecretStoreError(ValueError):
    pass


def path_for(settings: Any) -> Path:
    configured = getattr(settings, "secrets_file", None) or os.environ.get("CONVEYOR_SECRETS_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path(getattr(settings, "codex_memory_root")) / "secrets" / "console_secrets.json"


def _check(path: Path) -> None:
    if path.is_symlink():
        raise SecretStoreError("unsafe secrets path")
    if path.exists():
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or (st.st_mode & 0o077):
            raise SecretStoreError("unsafe secrets file permissions")


def _load(path: Path) -> dict[str, dict[str, Any]]:
    _check(path)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or any(
        not isinstance(k, str) or not NAME.fullmatch(k) or not isinstance(v, dict)
        for k, v in data.items()
    ):
        raise SecretStoreError("invalid secrets file")
    return data


def _write(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise SecretStoreError("unsafe secrets directory")
    _check(path)
    fd, tmp = tempfile.mkstemp(prefix=".secrets-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(entries, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        # Persist the rename, not just the data. Best effort on unsupported OS.
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _valid_name(name: Any) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise SecretStoreError("invalid secret name")
    return name


def list_secrets(settings: Any) -> list[dict[str, Any]]:
    with _LOCK:
        entries = _load(path_for(settings))
    return [{"name": name, "set": isinstance(item.get("value"), str),
             "updated_at": item.get("updated_at")}
            for name, item in sorted(entries.items())]


def set_secret(settings: Any, name: Any, value: Any) -> dict[str, Any]:
    name = _valid_name(name)
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_VALUE or "\x00" in value:
        raise SecretStoreError("invalid secret value")
    with _LOCK:
        path = path_for(settings)
        entries = _load(path)
        now = datetime.now(timezone.utc).isoformat()
        entries[name] = {"value": value, "updated_at": now}
        _write(path, entries)
    return {"name": name, "set": True, "updated_at": now}


def delete_secret(settings: Any, name: Any) -> bool:
    name = _valid_name(name)
    with _LOCK:
        path = path_for(settings)
        entries = _load(path)
        if name not in entries or "value" not in entries[name]:
            return False
        # Tombstone keeps unset status visible without retaining the value.
        entries[name] = {"updated_at": datetime.now(timezone.utc).isoformat()}
        _write(path, entries)
    return True


def read_secret(path_or_settings: Any, name: str) -> str | None:
    """Used only for a selected, explicitly granted model provider env key."""
    _valid_name(name)
    path = path_or_settings if isinstance(path_or_settings, (str, Path)) else path_for(path_or_settings)
    with _LOCK:
        item = _load(Path(path)).get(name, {})
    value = item.get("value")
    return value if isinstance(value, str) else None
