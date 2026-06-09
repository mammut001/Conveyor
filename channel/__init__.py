"""channel/ — channel adapters and shared types.

Public surface:
  InboundMessage, OutboundPort       (types.py)
  is_allowed                         (auth.py)
  TelegramAdapter, TelegramOutbound  (telegram.py)
  FeishuAdapter                      (feishu.py)
"""
from channel.types import (
    ChannelName,
    ChatType,
    InboundMessage,
    OutboundPort,
)

__all__ = [
    "ChannelName",
    "ChatType",
    "InboundMessage",
    "OutboundPort",
]
