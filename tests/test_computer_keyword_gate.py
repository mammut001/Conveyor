"""Hard-blocked computer-use keywords must not be evadable by spelling."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from desktop_computer_requests import contains_blocked_keyword

_S = SimpleNamespace(conveyor_computer_blocked_keywords=(), conveyor_computer_allow_login_passwords=False)


class KeywordGateTest(unittest.TestCase):
    def test_spelling_tricks_are_caught(self) -> None:
        for text in ("ｂａｎｋ", "b​ank", "b a n k", "银​行", "汇款给他", "打开系统设置",
                     "System Preferences", "wire transfer", "delete my account"):
            with self.subTest(text=text):
                self.assertIsNotNone(contains_blocked_keyword(_S, text))

    def test_ordinary_text_is_not_blocked(self) -> None:
        for text in ("web ankle", "打开计算器并点 1", "git checkout main", "Calculator", "a b c"):
            with self.subTest(text=text):
                self.assertIsNone(contains_blocked_keyword(_S, text))

    def test_chinese_keyword_in_a_direct_action_is_refused(self) -> None:
        # json.dumps escaped Chinese to \\uXXXX, so 转账/交易密码 never matched here.
        from handlers.tools.executors import exec_computer_action
        from tests.test_computer_dispatch import _settings
        with tempfile.TemporaryDirectory() as raw:
            settings = replace(_settings(Path(raw), always_direct=True))
            with patch("desktop_computer_requests.is_direct_mode_active", return_value=True):
                out = asyncio.run(exec_computer_action(
                    settings, '{"action":"type","text":"转账 交易密码 123456"}'))
        self.assertIn("受限关键词", out)


if __name__ == "__main__":
    unittest.main()
