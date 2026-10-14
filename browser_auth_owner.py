"""G4 session-directory ownership: advisory flock plus PID/starttime identity.

The lock is acquired before launching Chromium and held until bounded cleanup.
The /proc start tick distinguishes PID reuse after a crash. Both checks are
conservative: an ambiguous owner is never treated as permission to kill.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import stat


def process_starttime(pid):
    """Linux /proc/<pid>/stat field 22 (index 19 after the comm field)."""
    try:
        raw = (Path("/proc") / str(int(pid)) / "stat").read_text()
        return int(raw.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def owner_alive(folder):
    try:
        path = Path(folder) / "owner.json"
        if path.is_symlink():
            return True  # fail closed on untrusted ownership metadata
        identity = json.loads(path.read_text())
        pid = identity["pid"]
        start = identity["starttime"]
        if (type(pid) is not int or pid < 1 or
                type(start) is not int or start < 0):
            return True
        proc = Path("/proc") / str(pid)
        return proc.stat().st_uid == os.getuid() and process_starttime(pid) == start
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return True  # malformed metadata is not evidence of orphanhood


def acquire(folder):
    """Exclusive per-session owner FD, returned open to the session."""
    folder = Path(folder)
    path = folder / "lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise OSError("g4_lock_permissions")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        tick = process_starttime(os.getpid())
        if tick is None:
            raise OSError("g4_owner_identity_unavailable")
        payload = json.dumps({"pid": os.getpid(), "starttime": tick}).encode()
        tmp = folder / "owner.json.tmp"
        metadata_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(metadata_fd, "wb") as dst:
                dst.write(payload)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(tmp, folder / "owner.json")
        finally:
            if tmp.exists():
                tmp.unlink()
        return fd
    except BaseException:
        os.close(fd)
        raise


def try_lock_for_cleanup(folder):
    """Return the exclusive lock FD or None if another owner holds it."""
    path = Path(folder) / "lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except (OSError, ValueError):
        return None  # never delete on lock inspection failure
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            os.close(fd)
            return None
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, BlockingIOError):
        os.close(fd)
        return None


def release(fd):
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
