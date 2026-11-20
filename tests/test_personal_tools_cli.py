"""personal_tools.cli runs only tools that need no confirmation."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from handlers.tools.registry import DangerLevel
from personal_tools import cli, registry


class PersonalToolsCliTests(unittest.TestCase):
    def test_list_contains_only_read_and_write_safe_tools(self) -> None:
        rows = cli.allowed_tools()
        names = {name for name, _ in rows}
        self.assertEqual(names, {"reminders.create", "reminders.list", "reminders.due"})
        for name in names:
            self.assertIn(registry.get_personal_tool(name).danger, (DangerLevel.READ, DangerLevel.WRITE_SAFE))

    def test_confirmation_tools_are_refused_before_running(self) -> None:
        with mock.patch.object(registry, "execute_personal_tool") as execute:
            code, text = cli.run("reminders.cancel", "1", settings=object())
        self.assertEqual(code, 3)
        execute.assert_not_called()
        # Registry calls these READ / WRITE_SAFE, but they act outside Conveyor: refused.
        for tool in ("github.create_issue", "nightly.start", "browser.prepare_submit", "notes.add"):
            with mock.patch.object(registry, "execute_personal_tool") as execute:
                self.assertEqual(cli.run(tool, "x", settings=object())[0], 3, tool)
            execute.assert_not_called()

    def test_unknown_tool_and_missing_owner(self) -> None:
        self.assertEqual(cli.run("nope.nothing", "", settings=object())[0], 2)
        settings = mock.Mock(telegram_allowed_user_id=None)
        self.assertEqual(cli.run("reminders.list", "", settings=settings)[0], 4)

    def test_reminder_is_created_for_the_owner(self) -> None:
        from personal_tools.store import PersonalToolsStore
        from tests.test_web_reminders_api import _settings

        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            code, text = cli.run("reminders.create", "明天早上8点 开会", settings=settings)
            self.assertEqual(code, 0, text)
            rows = PersonalToolsStore(settings).list_reminders(str(settings.telegram_allowed_user_id), limit=10)
            self.assertEqual([r.text for r in rows], ["开会"])


if __name__ == "__main__":
    unittest.main()
