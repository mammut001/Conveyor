"""Kill only same-UID Chromium with an exact orphaned G4 user-data directory.

Never kill on process-name or substring matches. A missing /proc means there
are no verified processes; cleanup still retries and fails closed on failure.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import stat
import time


def _matching_pids(profile: Path):
    target = ("--user-data-dir=" + str(profile)).encode()
    for folder in Path("/proc").iterdir():
        if not folder.name.isdigit():
            continue
        try:
            if folder.stat().st_uid != os.getuid():
                continue
            parts = (folder / "cmdline").read_bytes().split(b"\0")
            if target in parts and any(b"chrom" in x.lower() for x in parts[:2]):
                yield int(folder.name)
        except (OSError, ValueError):
            continue


def _signal_verified(pid, profile, sig):
    # Revalidate to reduce PID-reuse races; never signal another profile.
    if pid in _matching_pids(profile):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def cleanup(root):
    root = Path(root)
    for folder in root.glob("session-*"):
        if folder.is_symlink():
            folder.unlink()
            continue
        if not folder.is_dir():
            continue
        # Never recurse into unexpected owner / symlinked profile data.
        if folder.stat().st_uid != os.getuid():
            raise OSError("g4_orphan_ownership_denied")
        profile = folder / "profile"
        pids = list(_matching_pids(profile))
        for pid in pids:
            _signal_verified(pid, profile, signal.SIGTERM)
        for attempt in range(6):
            try:
                shutil.rmtree(folder)
                break
            except OSError:
                if attempt == 2:
                    for pid in pids:
                        _signal_verified(pid, profile, signal.SIGKILL)
                if attempt == 5:
                    raise
                time.sleep(0.1 * (attempt + 1))
