from pathlib import Path
import re
import unittest

from overnight_chat import route


ROOT = Path(__file__).resolve().parents[1]


class NightlyWelcomeTests(unittest.TestCase):
    def test_fresh_chat_welcome_explains_nightly_commands(self):
        source = (ROOT / "web/src/components/ChatPanel.tsx").read_text(encoding="utf-8")
        start = source.index('<div className="welcome-state">')
        end = source.index("{messages.map", start)
        welcome = source[start:end]

        for command in (
            "/nightly_start <objective>",
            "/nightly_status <run ID>",
            "/nightly_cancel <run ID>",
        ):
            with self.subTest(command=command):
                self.assertIn(command, welcome)

    def test_fresh_chat_welcome_shows_routable_chinese_start_example(self):
        source = (ROOT / "web/src/components/ChatPanel.tsx").read_text(encoding="utf-8")
        start = source.index('<div className="welcome-state">')
        end = source.index("{messages.map", start)
        welcome = source[start:end]

        examples = re.findall(
            r"<code>\{'((?:/nightly_start|启动夜间任务：)[^']*)'\}</code>",
            welcome,
        )
        concrete = [
            command for command in examples
            if re.search(r"[\u4e00-\u9fff]", command) and "<objective>" not in command
        ]
        self.assertTrue(concrete, "welcome should include a concrete Chinese nightly start example")
        for command in concrete:
            with self.subTest(command=command):
                routed = route(command)
                self.assertIsNotNone(routed)
                self.assertEqual(routed.tools, ("nightly.start",))

    def test_fresh_chat_welcome_shows_nightly_scope_and_defaults(self):
        source = (ROOT / "web/src/components/ChatPanel.tsx").read_text(encoding="utf-8")
        start = source.index('<div className="welcome-state">')
        end = source.index("{messages.map", start)
        welcome = source[start:end]

        for term in ("预览", "生产", "1 小时", "12"):
            with self.subTest(term=term):
                self.assertIn(term, welcome)


if __name__ == "__main__":
    unittest.main()
