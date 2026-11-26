"""Compatibility shim for the renamed generic module."""
from agent_supervisor import *  # noqa: F401,F403
import agent_supervisor as _impl
__all__ = getattr(_impl, "__all__", [n for n in dir(_impl) if not n.startswith("_")])
