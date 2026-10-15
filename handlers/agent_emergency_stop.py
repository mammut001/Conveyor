"""Allow authenticated emergency Computer Use stop before agent resolution.

This only handles exact existing stop phrases in strict AgentContext mode.
No other tool/command or model action bypasses canonical agent identity.
"""
from __future__ import annotations

from functools import wraps


def stop_before_identity(func):
    @wraps(func)
    async def wrapped(msg, port, settings, *args, **kwargs):
        import agent_context
        if agent_context.enabled(settings):
            from handlers.intent import COMPUTER_STOP_TEXTS
            if (isinstance(msg.text, str)
                    and msg.text.strip().lower() in COMPUTER_STOP_TEXTS):
                from channel.auth import is_allowed
                if not is_allowed(msg, settings):
                    await port.reply(msg, "Unauthorized.")
                    return None
                # Bypass ONLY the agent-scoped tool dispatcher. This is a
                # safety cancellation, not an authority to start or edit tasks.
                from handlers.tools.executors import exec_computer_stop
                result = await exec_computer_stop(settings, "")
                await port.reply(msg, result)
                return None
        return await func(msg, port, settings, *args, **kwargs)

    return wrapped
