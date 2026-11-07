"""/web_fetch, /web_text, /web_headers and /web_search used to raise NameError."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers import commands


class WebCommandsTest(unittest.TestCase):
    def test_each_web_command_invokes_its_tool(self) -> None:
        cases = {
            commands._web_fetch: "web.fetch", commands._web_text: "web.text",
            commands._web_headers: "web.headers", commands._web_search: "web.search",
        }
        for handler, tool in cases.items():
            with self.subTest(tool=tool):
                invoke = AsyncMock()
                with patch("handlers.tools.runner._invoke_tool", invoke):
                    asyncio.run(handler(SimpleNamespace(), SimpleNamespace(), None, SimpleNamespace(), "x"))
                self.assertEqual(invoke.await_args.args[3], tool)


if __name__ == "__main__":
    unittest.main()
