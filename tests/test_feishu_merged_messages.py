"""Feishu: quickly sent messages that the SDK merges must keep their text."""
from __future__ import annotations

import unittest

from lark_oapi.channel.safety.chat_pipeline import merge_batch
from lark_oapi.channel.types import Conversation, Identity, InboundMessage as SdkMessage, TextContent

from channel.feishu import inbound_from_event


def _sdk_message(mid: str, text: str) -> SdkMessage:
    return SdkMessage(
        id=mid, create_time=1, conversation=Conversation(chat_id="oc_1", chat_type="p2p"),
        sender=Identity(open_id="ou_owner"), content=TextContent(text=text), content_text=text,
    )


class FeishuMergedMessageTest(unittest.TestCase):
    def test_merged_batch_keeps_the_joined_text(self) -> None:
        merged = merge_batch([_sdk_message("m1", "帮我看一下日志"), _sdk_message("m2", "顺便查下磁盘")])
        self.assertEqual(merged.content_text, "")  # what the SDK hands over
        inbound = inbound_from_event(merged)
        self.assertEqual(inbound.operator_id, "ou_owner")
        self.assertIn("帮我看一下日志", inbound.text)
        self.assertIn("顺便查下磁盘", inbound.text)

    def test_single_message_unchanged(self) -> None:
        inbound = inbound_from_event(_sdk_message("m1", "/status"))
        self.assertEqual(inbound.text, "/status")


if __name__ == "__main__":
    unittest.main()
