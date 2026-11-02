from pathlib import Path
import unittest


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


if __name__ == "__main__":
    unittest.main()
