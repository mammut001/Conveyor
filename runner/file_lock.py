from __future__ import annotations

import os
import time
import logging
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:
    fcntl = None

@contextmanager
def file_lock(path: Path, *, timeout_seconds: float | None = None):
    """A cross-process file-based lock.
    
    If fcntl is available, uses flock. Otherwise, falls back to O_EXCL file creation.
    Blocks by default unless timeout_seconds is set.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    start_time = time.time()
    fd = None
    lock_acquired = False
    fallback_path = path.with_suffix(".lock_fallback")

    try:
        if fcntl is not None:
            fd = os.open(str(path), os.O_CREAT | os.O_WRONLY)
            while True:
                try:
                    if timeout_seconds is not None:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_EX)
                    lock_acquired = True
                    break
                except (OSError, IOError):
                    if timeout_seconds is not None and (time.time() - start_time) >= timeout_seconds:
                        raise TimeoutError(f"Could not acquire lock on {path} within {timeout_seconds} seconds.")
                    time.sleep(0.05)
        else:
            # Fallback approach
            while True:
                try:
                    fd = os.open(str(fallback_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, str(os.getpid()).encode("utf-8"))
                    lock_acquired = True
                    break
                except FileExistsError:
                    if timeout_seconds is not None and (time.time() - start_time) >= timeout_seconds:
                        raise TimeoutError(f"Could not acquire fallback lock on {path} within {timeout_seconds} seconds.")
                    time.sleep(0.05)
        
        yield

    finally:
        if fcntl is not None:
            if fd is not None:
                if lock_acquired:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except (OSError, IOError):
                        pass
                os.close(fd)
        else:
            if fd is not None:
                os.close(fd)
            if lock_acquired:
                try:
                    fallback_path.unlink()
                except OSError:
                    pass
