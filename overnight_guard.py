"""Propagate lease fencing into synchronous adapters and background threads."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading

_CURRENT=ContextVar('nightly_lease_guard',default=None)


class LeaseLost(RuntimeError):
    pass


class LeaseGuard:
    def __init__(self,store,task):
        self.store=store;self.task=task;self.stopped=threading.Event()

    def check(self):
        if self.stopped.is_set() or not self.store.is_current(self.task['id'],self.task['lease']):
            self.stopped.set()
            raise LeaseLost('nightly task is no longer authorized')


def check_guard():
    guard=_CURRENT.get()
    if guard is not None:
        guard.check()


@contextmanager
def bind_guard(guard):
    token=_CURRENT.set(guard)
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def cleanup_scope():
    # Only coordinator-owned cleanup/rollback may proceed after cancellation.
    with bind_guard(None):
        yield
