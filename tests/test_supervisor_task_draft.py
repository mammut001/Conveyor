"""Structured discussion drafts, plus one supervisor chat handoff over HTTP."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlencode

from conveyor.agents.supervisor_task_draft import (
    build_supervisor_task_suggestion,
    parse_task_draft,
    strip_task_draft_fence,
    suggestion_metadata,
    task_suggestion_from_metadata,
)
from tests.test_supervisor_api import _HttpHarness, _init_git_repo, _make_settings


def _fence(goal: str, criteria: str, extra: dict | None = None) -> str:
    payload = {"goal": goal, "acceptance_criteria": criteria}
    if extra:
        payload.update(extra)
    return "```conveyor-task\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


class TaskDraftParserTests(unittest.TestCase):
    def test_last_fence_wins_and_extra_keys_are_ignored(self) -> None:
        text = (
            _fence("旧的目标", "旧标准")
            + "\n中间说明\n"
            + _fence("保留门户转发的原始 Host", "Host 与 Origin 主机名一致", {"note": "ignore"})
        )
        self.assertEqual(
            parse_task_draft(text),
            {
                "goal": "保留门户转发的原始 Host",
                "acceptance_criteria": "Host 与 Origin 主机名一致",
            },
        )

    def test_rejects_non_object_prohibition_and_partial_labels(self) -> None:
        self.assertIsNone(parse_task_draft('```conveyor-task\n["goal"]\n```'))
        self.assertIsNone(parse_task_draft("```conveyor-task\n{not json}\n```"))
        self.assertIsNone(parse_task_draft(_fence("不要执行", "无")))
        self.assertIsNone(parse_task_draft("目标：保留 Host\n"))
        labeled = "说明\n任务目标：保留 Host\n验收标准：Origin 一致\n"
        self.assertEqual(
            parse_task_draft(labeled),
            {"goal": "保留 Host", "acceptance_criteria": "Origin 一致"},
        )
        english = "Goal: keep the host\nAcceptance criteria: origin matches\n"
        self.assertEqual(
            parse_task_draft(english),
            {"goal": "keep the host", "acceptance_criteria": "origin matches"},
        )

    def test_strip_removes_the_fence_and_keeps_the_plan(self) -> None:
        visible = strip_task_draft_fence("维护计划正文\n\n" + _fence("保留 Host", "Origin 一致"))
        self.assertIn("维护计划正文", visible)
        self.assertNotIn("conveyor-task", visible)
        self.assertNotIn("保留 Host", visible)

    def test_read_only_structured_draft_is_not_the_user_prohibition(self) -> None:
        prompt = "先不要执行、不要改代码，请给出维护计划"
        assistant = "维护计划：保留浏览器原来的 Host。\n" + _fence(
            "保留门户转发的原始 Host",
            "新会话请求的 Host 与 Origin 主机名一致",
        )
        suggestion = build_supervisor_task_suggestion(prompt, assistant, "sid-1")
        assert suggestion is not None
        self.assertEqual(suggestion["goal"], "保留门户转发的原始 Host")
        self.assertEqual(suggestion["acceptance_criteria"], "新会话请求的 Host 与 Origin 主机名一致")
        self.assertEqual(suggestion["prompt"], suggestion["goal"])
        self.assertNotIn("先不要执行", suggestion["prompt"])
        self.assertEqual(suggestion["source"], "structured_draft")
        self.assertIs(suggestion["execution_authorized"], False)
        self.assertIn("维护计划", suggestion["context"])
        self.assertNotIn("```", suggestion["context"])

    def test_read_only_without_draft_does_not_authorize_the_request(self) -> None:
        suggestion = build_supervisor_task_suggestion(
            "只讨论，不要改代码",
            "这是讨论正文。",
            "sid-2",
        )
        assert suggestion is not None
        self.assertEqual(suggestion["prompt"], "")
        self.assertEqual(suggestion["goal"], "")
        self.assertEqual(suggestion["acceptance_criteria"], "")
        self.assertEqual(suggestion["source"], "read_only_fallback")
        self.assertIs(suggestion["execution_authorized"], False)
        self.assertEqual(suggestion["context"], "这是讨论正文。")

    def test_edit_imperative_without_a_draft_keeps_the_operator_prompt(self) -> None:
        prompt = "帮我修改 README.md 并运行测试"
        suggestion = build_supervisor_task_suggestion(prompt, "建议先改 README。", "sid-3")
        assert suggestion is not None
        self.assertEqual(suggestion["prompt"], prompt)
        self.assertEqual(suggestion["goal"], prompt)
        self.assertEqual(suggestion["acceptance_criteria"], "")
        self.assertEqual(suggestion["source"], "operator_request")
        self.assertIs(suggestion["execution_authorized"], True)
        self.assertIsNone(build_supervisor_task_suggestion("今天天气怎么样", "晴。", "sid-4"))

    def test_incomplete_fence_hides_partial_json_and_keeps_markdown(self) -> None:
        pieces = [
            "维护计划：先看范围。\n",
            "```python\nprint('ok')\n",
            "```\n",
            "```conveyor-task\n",
            '{"goal": "保留',
            ' Host"}\n```\n尾注',
        ]
        buf = ""
        shown: list[str] = []
        for piece in pieces:
            buf += piece
            shown.append(strip_task_draft_fence(buf))
        self.assertEqual(shown[0], "维护计划：先看范围。")
        self.assertIn("```python", shown[1])
        self.assertIn("print('ok')", shown[1])
        self.assertIn("```python", shown[2])
        self.assertNotIn("conveyor-task", shown[3])
        self.assertNotIn("保留", shown[3])
        self.assertNotIn("goal", shown[4])
        self.assertNotIn("保留", shown[4])
        self.assertIn("维护计划", shown[5])
        self.assertIn("```python", shown[5])
        self.assertIn("print('ok')", shown[5])
        self.assertIn("尾注", shown[5])
        self.assertNotIn("conveyor-task", shown[5])
        self.assertNotIn("保留", shown[5])

    def test_long_goal_is_kept_and_context_stays_bounded(self) -> None:
        goal = "保" * 1900
        preface = "计" * 2000
        suggestion = build_supervisor_task_suggestion(
            "先不要执行，请规划",
            preface + "\n" + _fence(goal, "标准"),
            "sid-5",
        )
        assert suggestion is not None
        self.assertEqual(len(suggestion["goal"]), 1900)
        self.assertEqual(suggestion["acceptance_criteria"], "标准")
        self.assertEqual(suggestion["prompt"], suggestion["goal"])
        self.assertEqual(len(suggestion["context"]), 1800)
        self.assertNotIn("conveyor-task", suggestion["context"])
        kept = build_supervisor_task_suggestion("先不要执行，请规划", _fence("保" * 8000, "通过"), "sid-5b")
        assert kept is not None
        self.assertEqual(len(kept["goal"]), 8000)
        self.assertIs(kept["execution_authorized"], False)
        self.assertIsNone(parse_task_draft(_fence("保" * 8001, "通过")))
        restored = task_suggestion_from_metadata(suggestion_metadata(suggestion), "sid-5")
        assert restored is not None
        self.assertEqual(restored["goal"], suggestion["goal"])
        self.assertIs(restored["execution_authorized"], False)
        legacy = task_suggestion_from_metadata({"prompt": "帮我修改 README.md 并运行测试"}, "sid-6")
        assert legacy is not None
        self.assertEqual(legacy["prompt"], "帮我修改 README.md 并运行测试")
        self.assertNotIn("goal", legacy)


class SupervisorDraftHandoffHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-sup-draft-http-")
        self.root = Path(self._tmp.name)
        self._harnesses: list[_HttpHarness] = []

    def tearDown(self) -> None:
        for harness in reversed(self._harnesses):
            harness.close()
        self._tmp.cleanup()

    def test_read_only_discussion_persists_plan_without_dispatch(self) -> None:
        settings = _make_settings(self.root / "chat-env")
        harness = _HttpHarness(settings)
        self._harnesses.append(harness)
        repo_dir = self.root / "chat-repo"
        _init_git_repo(repo_dir)
        _, repo = harness.request_json("POST", "/api/supervisor/repositories", {"path": str(repo_dir)})
        _, worktree = harness.request_json(
            "POST",
            f"/api/supervisor/repositories/{repo['id']}/worktrees",
            {"branch": "feat/draft-handoff"},
        )
        bound = worktree["chat_session_id"]
        prompt = "先不要执行、不要改代码，请给出维护计划"
        assistant = (
            "维护计划：保留浏览器原来的 Host，这样新会话的 Origin 才能对上。\n"
            + _fence("保留门户转发的原始 Host", "新会话请求的 Host 与 Origin 主机名一致")
            + "\n[[CONFIDENCE: high]]"
        )
        codex_calls: list[str] = []

        async def _fake_stream_chat(_cfg: Any, _messages: list[dict[str, Any]]):
            yield assistant

        async def _spy_handle_codex_job(msg: Any, port: Any, _runner: Any, *, mode: Any, prompt: str, wait: bool = True):
            codex_calls.append(prompt)
            await port.reply(msg, f"legacy-codex-ran:{prompt}")

        with (
            patch("runner.chat_client.stream_chat", _fake_stream_chat),
            patch("handlers.jobs.handle_codex_job", _spy_handle_codex_job),
        ):
            status, events = harness.request_sse(
                "/api/chat",
                {"message": prompt, "session_id": bound},
            )
        self.assertEqual(status, 200)
        assert isinstance(events, list)
        self.assertEqual(codex_calls, [])
        messages = [payload for name, payload in events if name == "message"]
        self.assertEqual(len(messages), 1)
        body = messages[0]
        self.assertIn("维护计划", body["text"])
        self.assertNotIn("conveyor-task", body["text"])
        self.assertNotIn("```", body["text"])
        suggestion = body["task_suggestion"]
        self.assertEqual(suggestion["goal"], "保留门户转发的原始 Host")
        self.assertEqual(suggestion["acceptance_criteria"], "新会话请求的 Host 与 Origin 主机名一致")
        self.assertEqual(suggestion["prompt"], suggestion["goal"])
        self.assertNotIn("先不要执行", suggestion["prompt"])
        self.assertIs(suggestion["execution_authorized"], False)
        self.assertEqual(suggestion["source"], "structured_draft")

        hist_status, history = harness.request_json(
            "GET",
            "/api/chat/history?" + urlencode({"session_id": bound}),
        )
        self.assertEqual(hist_status, 200)
        self.assertEqual(history["messages"][0]["text"], prompt)
        stored = history["messages"][1]
        self.assertEqual(stored["text"], body["text"])
        self.assertEqual(stored["task_suggestion"]["goal"], suggestion["goal"])
        self.assertEqual(stored["task_suggestion"]["acceptance_criteria"], suggestion["acceptance_criteria"])
        self.assertEqual(stored["task_suggestion"]["prompt"], suggestion["prompt"])
        self.assertIs(stored["task_suggestion"]["execution_authorized"], False)
        task_status, tasks = harness.request_json("GET", "/api/supervisor/tasks")
        self.assertEqual(task_status, 200)
        self.assertEqual(tasks.get("tasks"), [])


if __name__ == "__main__":
    unittest.main()
