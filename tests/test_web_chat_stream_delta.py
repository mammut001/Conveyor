from __future__ import annotations

import asyncio
import queue
import unittest

from channel.types import InboundMessage
from web_chat import WebChatPort


class WebChatStreamDeltaWhitespaceTests(unittest.TestCase):
    def test_edit_progress_preserves_whitespace_before_streaming_cursor(self):
        events: queue.Queue = queue.Queue()
        port = WebChatPort(events, settings=None, durable_session_id="web:web-console:test")
        msg = InboundMessage(
            channel="web",
            operator_id="web-console",
            chat_id="test",
            message_id=None,
            text="hello",
        )
        content = '    print("hello")\n\n'

        delivered = asyncio.run(port.edit_progress(msg, "placeholder", content + " ▍"))

        self.assertTrue(delivered)
        self.assertEqual(events.get_nowait(), ("delta", {"text": content}))

    def test_edit_progress_removes_only_the_cursor_suffix(self):
        events: queue.Queue = queue.Queue()
        port = WebChatPort(events, settings=None, durable_session_id="web:web-console:test")
        msg = InboundMessage(
            channel="web",
            operator_id="web-console",
            chat_id="test",
            message_id=None,
            text="hello",
        )
        content = "  indented Markdown\n    code block\n\n"

        asyncio.run(port.edit_progress(msg, "placeholder", content + " ▍"))

        self.assertEqual(events.get_nowait(), ("delta", {"text": content}))


if __name__ == "__main__":
    unittest.main()
