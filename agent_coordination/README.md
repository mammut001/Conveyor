# Agent coordination

This package groups the existing agent delegation, channel-routing and result-delivery modules. It keeps runtime behavior and persistent data formats unchanged.

- `agent_delegation*`: task submission, schema, queue, worker and execution turn.
- `agent_channel_*`: routing agent messages and returning results through channels.
- `agent_result_*`: result outbox and poller lifecycle.
- `agent_telegram_presentation.py`: Telegram presentation for delegated tasks.

Import these modules through `agent_coordination`, for example:

```python
from agent_coordination import agent_delegation
from agent_coordination.agent_delegation_worker import start
```

The previous top-level module import paths are no longer available. Deployments must copy the whole package, not just individual Python files.
