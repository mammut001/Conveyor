from __future__ import annotations

import logging
import sys
import threading
import traceback
from typing import Any

from redaction import SecretRedactingFilter, redact_text

DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "googleapiclient", "google_auth_httplib2", "lark_oapi")

_original_sys_excepthook = None
_original_threading_excepthook = None


def _redacting_sys_excepthook(exc_type: type[BaseException], exc_value: BaseException, exc_traceback: Any) -> None:
    if exc_type is not None and issubclass(exc_type, KeyboardInterrupt):
        if _original_sys_excepthook is not None and _original_sys_excepthook is not _redacting_sys_excepthook:
            _original_sys_excepthook(exc_type, exc_value, exc_traceback)
            return
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    formatted = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    sys.stderr.write(redact_text(formatted))
    sys.stderr.flush()


_redacting_sys_excepthook._is_redacting_hook = True  # type: ignore[attr-defined]


def _redacting_threading_excepthook(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is not None and issubclass(args.exc_type, KeyboardInterrupt):
        if _original_threading_excepthook is not None and _original_threading_excepthook is not _redacting_threading_excepthook:
            _original_threading_excepthook(args)
            return
        if hasattr(threading, "__excepthook__"):
            threading.__excepthook__(args)
            return
        return
    thread_name = args.thread.name if args.thread is not None else ""
    header = f"Exception in thread {thread_name}:\n" if thread_name else "Exception in thread:\n"
    formatted = header + "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    sys.stderr.write(redact_text(formatted))
    sys.stderr.flush()


_redacting_threading_excepthook._is_redacting_hook = True  # type: ignore[attr-defined]


def install_redacting_excepthook() -> None:
    """Install redacting exception hooks on sys and threading."""
    global _original_sys_excepthook, _original_threading_excepthook
    if not getattr(sys.excepthook, "_is_redacting_hook", False):
        _original_sys_excepthook = sys.excepthook
        sys.excepthook = _redacting_sys_excepthook
    if not getattr(threading.excepthook, "_is_redacting_hook", False):
        _original_threading_excepthook = threading.excepthook
        threading.excepthook = _redacting_threading_excepthook


def configure_logging(
    service_name: str | None = None,
    level: int | str = logging.INFO,
    fmt: str | None = None,
) -> logging.Logger:
    """Configure basic logging, attach redacting filters, and quiet noisy loggers."""
    if fmt is None:
        fmt = DEFAULT_LOG_FORMAT

    logging.basicConfig(format=fmt, level=level)

    root = logging.getLogger()
    root.setLevel(level)

    if not any(isinstance(f, SecretRedactingFilter) for f in root.filters):
        root.addFilter(SecretRedactingFilter())

    for handler in root.handlers:
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(SecretRedactingFilter())

    for name in NOISY_LOGGERS:
        noisy = logging.getLogger(name)
        if noisy.getEffectiveLevel() < logging.WARNING:
            noisy.setLevel(logging.WARNING)

    install_redacting_excepthook()

    if service_name:
        return logging.getLogger(service_name)
    return root
