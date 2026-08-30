from __future__ import annotations

import unittest

from runner.claude_code import ClaudeCodeBackend


class ClaudeCodeBackendTests(unittest.TestCase):
    def test_text_delta_is_visible(self) -> None:
        progress, result = ClaudeCodeBackend._claude_event({
            "type": "stream_event",
            "event": {"delta": {"type": "text_delta", "text": "hello"}},
        })
        self.assertEqual(progress, "hello")
        self.assertEqual(result, "")

    def test_thinking_delta_is_not_exposed(self) -> None:
        progress, result = ClaudeCodeBackend._claude_event({
            "type": "stream_event",
            "event": {"delta": {"type": "thinking_delta", "thinking": "private"}},
        })
        self.assertEqual((progress, result), ("", ""))

    def test_tool_use_becomes_compact_indicator(self) -> None:
        progress, _ = ClaudeCodeBackend._claude_event({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "secret"}}]},
        })
        self.assertEqual(progress, "🔧 Bash...")
        self.assertNotIn("secret", progress)

    def test_result_becomes_final_text(self) -> None:
        progress, result = ClaudeCodeBackend._claude_event({"type": "result", "result": "done"})
        self.assertEqual(progress, "")
        self.assertEqual(result, "done")


if __name__ == "__main__":
    unittest.main()
