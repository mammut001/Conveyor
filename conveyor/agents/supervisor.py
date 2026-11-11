"""Compatibility shim for the renamed generic module."""
from conveyor.agents.agent_supervisor import *  # noqa: F401,F403
from conveyor.agents import agent_supervisor as _impl
__all__ = getattr(_impl, "__all__", [n for n in dir(_impl) if not n.startswith("_")])
