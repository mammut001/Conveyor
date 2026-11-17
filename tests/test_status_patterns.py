"""Unit tests for :func:`conveyor.agents.chat_intents.detect_status_intent`.

``detect_status_intent`` is a pure function: no settings, DB, or network
access is involved, so these tests only exercise the pattern/guard matrix.
"""
from __future__ import annotations

import unittest

from conveyor.agents import chat_intents


class DetectStatusIntentProgressTests(unittest.TestCase):
    """Short, present-tense Chinese/English status questions -> "progress"."""

    def test_progress_positives(self) -> None:
        cases = [
            "进度",
            "进度？",
            "进度?",
            "qa-todo-cli 进度怎么样了",
            "进度怎么样了",
            "进度怎么样了？",
            "做完了吗",
            "做完了吗？",
            "跑完了吗",
            "做到哪了",
            "做到哪了呢",
            "现在什么情况",
            "现在什么情况？",
            "还剩多久",
            "还剩多久？",
            "结果怎么样",
            "做得怎么样",
            "跑得怎么样",
            "现在怎么样了",
            "status",
            "STATUS",
            "Status",
            "status?",
            "qa-todo-cli status",
            "progress",
            "Progress",
            "PROGRESS",
            "how is it going",
            "How is it going?",
            "how is the build going",
            "how's progress?",
            "what is the status",
        ]
        self.assertGreaterEqual(len(cases), 20)
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(chat_intents.detect_status_intent(text), "progress")

    def test_progress_negatives(self) -> None:
        long_progress = (
            "我想问问这个项目从现在开始整体的进度情况到底怎么样了呢，麻烦一次性给我讲清楚所有的细节和原因"
        )
        self.assertGreater(len(long_progress), 40)
        cases = [
            "",
            "   ",
            "我昨天停了",
            "不要停",
            "停止和暂停有什么区别",
            "怎么看进度",
            "为什么进度不更新",
            "进度是什么意思",
            "什么是进度",
            "/进度",
            "上次进度很好",
            "如何查看进度",
            "怎么实现进度条",
            "进度和状态有什么区别",
            "我不想问进度",
            "hello",
            "帮我建一个项目叫 x",
            long_progress,
            None,
        ]
        self.assertGreaterEqual(len(cases), 15)
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(chat_intents.detect_status_intent(text))


class DetectStatusIntentStopTests(unittest.TestCase):
    """Short, imperative stop/pause requests -> "stop"."""

    def test_stop_positives(self) -> None:
        cases = [
            "先停一下",
            "停一下",
            "停掉",
            "停止",
            "暂停",
            "暂停 qa-todo-cli",
            "暂停 qa-todo-cli？",
            "先停一下好吗",
            "先停一下！",
            "暂停一下",
            "先暂停",
            "停止。",
            "stop",
            "STOP",
            "Stop",
            "stop.",
            "stop it",
            "stop the goal",
            "pause",
            "Pause",
            "PAUSE",
            "Pause, please",
            "停止 qa-todo-cli",
            "  stop",
        ]
        self.assertGreaterEqual(len(cases), 20)
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(chat_intents.detect_status_intent(text), "stop")

    def test_stop_negatives(self) -> None:
        long_stop = (
            "我在想这个任务跑得不太对劲的时候是不是应该直接停止它，还是先看看日志再决定要怎么处理比较好"
        )
        self.assertGreater(len(long_stop), 40)
        cases = [
            "",
            "   ",
            "我昨天停了",
            "不要停",
            "停止和暂停有什么区别",
            "怎么看进度",
            "暂停是什么意思",
            "什么是暂停",
            "/暂停",
            "don't stop",
            "我不想停下来",
            "如何停止",
            "上次停止过一次",
            "为什么要停止",
            "停",
            "不停",
            "hello",
            "帮我看一下这个项目",
            long_stop,
            None,
        ]
        self.assertGreaterEqual(len(cases), 15)
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(chat_intents.detect_status_intent(text))


class DetectStatusIntentGuardTests(unittest.TestCase):
    """Cross-cutting guards: length cap, how-to/definition, past time, slash."""

    def test_length_cap_rejects_long_text_even_with_keywords(self) -> None:
        # Boundary: exactly 40 characters after strip() is still a short status query.
        exactly_40 = "进度" + "a" * 38
        self.assertEqual(len(exactly_40), 40)
        self.assertEqual(chat_intents.detect_status_intent(exactly_40), "progress")

        # One character over the cap -> None even though 进度 is right there.
        one_over = "进度" + "a" * 39
        self.assertEqual(len(one_over), 41)
        self.assertIsNone(chat_intents.detect_status_intent(one_over))

        # Clearly long sentence that only mentions 进度 in passing -> None.
        clearly_long = "这个项目整体进度到底怎么样了我现在想确认一下顺便问清楚所有细节和原因再决定下一步以及优先级"
        self.assertEqual(len(clearly_long), 45)
        self.assertGreater(len(clearly_long), 40)
        self.assertIsNone(chat_intents.detect_status_intent(clearly_long))

    def test_progress_negation_is_not_a_progress_request(self) -> None:
        cases = [
            "我不想问进度",
            "不用问进度",
            "不要告诉我进度",
            "别问进度",
            "没问进度",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(chat_intents.detect_status_intent(text))

    def test_stop_phrases_survive_negation_looking_words(self) -> None:
        # 别跑了 / 不用跑了 contain 别 / 不用 but are still stop requests.
        for text in ("别跑了", "不用跑了"):
            with self.subTest(text=text):
                self.assertEqual(chat_intents.detect_status_intent(text), "stop")

    def test_slash_command_is_never_a_status_intent(self) -> None:
        for text in ("/status", "/progress", "/stop", "/暂停"):
            with self.subTest(text=text):
                self.assertIsNone(chat_intents.detect_status_intent(text))

    def test_question_mark_ending_is_allowed_for_progress(self) -> None:
        self.assertEqual(chat_intents.detect_status_intent("做完了吗？"), "progress")
        self.assertEqual(chat_intents.detect_status_intent("进度怎么样了?"), "progress")

    def test_empty_or_none_text(self) -> None:
        self.assertIsNone(chat_intents.detect_status_intent(None))
        self.assertIsNone(chat_intents.detect_status_intent(""))


if __name__ == "__main__":
    unittest.main()
