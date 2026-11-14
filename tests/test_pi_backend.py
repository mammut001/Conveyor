from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import queue
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from channel.types import InboundMessage
from conveyor.core.config import Settings
from handlers.chat import is_destructive_request, routes_to_agent
from runner.pi_agent import (
    PI_PROVIDER_KEY_ENV,
    PiBackend,
    normalize_pi_model,
    pi_api_key_env,
)
from runner.operators import run as run_ops
from runner.providers import create_agent_backend, create_runtime_runner
from runner.types import Job, JobMode, JobState
from conveyor.web.web_chat import WebChatPort, run_web_chat


def _base_env(**extra: str) -> dict[str, str]:
    env = {
        "HOME": "/tmp",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "TMPDIR": "/tmp",
    }
    env.update(extra)
    return env


def _write_fake_pi(tmp: Path, name: str, dump_path: Path) -> Path:
    """Fake pi CLI that records the environment it was handed."""
    fake_pi = tmp / name
    fake_pi.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "sys.stdin.read()\n"
        f"with open({str(dump_path)!r}, 'w', encoding='utf-8') as fh:\n"
        "    json.dump(dict(os.environ), fh)\n"
        "print(json.dumps({'type': 'agent_end', 'messages': [{'role': 'assistant', 'content': [{'type': 'text', 'text': 'ok'}]}]}), flush=True)\n",
        encoding="utf-8",
    )
    fake_pi.chmod(0o755)
    return fake_pi


def _write_fake_pi_events(tmp: Path, name: str, events: list[dict]) -> Path:
    """Fake pi CLI that prints the given events as JSONL on stdout."""
    lines = [
        "#!/usr/bin/env python3",
        "import json, sys",
        "sys.stdin.read()",
    ]
    for event in events:
        lines.append(f"print(json.dumps({event!r}), flush=True)")
    fake_pi = tmp / name
    fake_pi.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fake_pi.chmod(0o755)
    return fake_pi


def _write_fake_pi_timeline(tmp: Path, name: str, timeline: list) -> Path:
    """Fake pi CLI replaying a timeline of events and silent gaps.

    Each timeline item is either a dict (printed as one JSONL event) or a
    float (a silent sleep). stdout is flushed after every line so the parent
    stream reader observes the real timing.
    """
    lines = [
        "#!/usr/bin/env python3",
        "import json, sys, time",
        "sys.stdin.read()",
        "def emit(obj):",
        "    print(json.dumps(obj), flush=True)",
    ]
    for item in timeline:
        if isinstance(item, dict):
            lines.append(f"emit({item!r})")
        else:
            lines.append(f"time.sleep({item!r})")
    fake_pi = tmp / name
    fake_pi.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fake_pi.chmod(0o755)
    return fake_pi


def _run_pi(backend: PiBackend, job: Job) -> None:
    async def on_progress(text: str) -> None:
        pass

    asyncio.run(backend._run_codex_attempt(job, on_progress))


def _make_settings(tmp: Path) -> Settings:
    root = (tmp / "workspace").resolve()
    root.mkdir(parents=True, exist_ok=True)
    state_root = tmp / "state"
    (state_root / "tasks").mkdir(parents=True, exist_ok=True)
    return Settings(
        telegram_bot_token="unused",
        telegram_allowed_user_id=1,
        codex_workspace_root=root,
        codex_bin="codex",
        codex_task_root=state_root / "tasks",
        codex_model=None,
        codex_timeout_seconds=10,
        telegram_progress_seconds=1,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=state_root / "memory",
        user_timezone="UTC",
        chat_mode="auto",
        chat_api_key="sk-test-deepseek",
        chat_model="deepseek-flash",
        chat_tools_enabled=True,
    )


def _drain_queue(q: queue.Queue) -> list[tuple[str, dict]]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


class PiBackendTests(unittest.TestCase):
    def test_provider_factory_selects_pi(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            settings = _make_settings(Path(raw))
            backend = create_agent_backend(settings, provider="pi")
            runner = create_runtime_runner(settings, provider="pi")
            self.assertEqual(backend.name, "pi")
            self.assertIsInstance(runner, PiBackend)
            self.assertEqual(runner.pi_model, "deepseek-v4-flash")

    def test_destructive_detection_and_routing(self) -> None:
        self.assertTrue(routes_to_agent("你可以帮我下载一个这个吗"))
        self.assertFalse(is_destructive_request("你可以帮我下载一个这个吗"))
        self.assertFalse(is_destructive_request("帮我查一下 GitHub 有没有登录"))
        self.assertTrue(is_destructive_request("帮我把 /tmp/test.txt 删除掉"))
        self.assertTrue(is_destructive_request("please rm -rf /tmp/build"))
        self.assertTrue(is_destructive_request("delete the old backup file"))

    def test_run_codex_attempt_parses_events_and_passes_env(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            backend = PiBackend(settings)
            fake_pi = tmp / "fake-pi"
            fake_pi.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "prompt = sys.stdin.read()\n"
                "assert os.environ.get(\"DEEPSEEK_API_KEY\") == \"sk-test-deepseek\"\n"
                "assert os.environ.get(\"PI_OFFLINE\") == \"1\"\n"
                "print(json.dumps({\"type\": \"tool_execution_start\", \"toolName\": \"bash\", \"args\": {\"command\": \"gh auth status\"}}), flush=True)\n"
                "print(json.dumps({\"type\": \"message_update\", \"assistantMessageEvent\": {\"type\": \"text_delta\", \"delta\": \"GitHub 已登录: \"}}), flush=True)\n"
                "print(json.dumps({\"type\": \"message_end\", \"message\": {\"role\": \"assistant\", \"content\": [{\"type\": \"text\", \"text\": \"GitHub 已登录: mammut001\"}]}}), flush=True)\n"
                "print(json.dumps({\"type\": \"agent_end\", \"messages\": [{\"role\": \"assistant\", \"content\": [{\"type\": \"text\", \"text\": \"GitHub 已登录: mammut001\"}]}]}), flush=True)\n",
                encoding="utf-8",
            )
            fake_pi.chmod(0o755)
            backend.pi_bin = str(fake_pi)

            job = Job(
                "job-test-1",
                JobMode.RUN,
                "帮我查一下 GitHub 有没有登录",
                "unused",
                state=JobState.RUNNING,
                worktree_path=settings.codex_workspace_root,
                log_path=settings.codex_task_root / "job.log",
                final_message_path=settings.codex_task_root / "final.txt",
            )
            updates: list[str] = []

            async def on_progress(text: str) -> None:
                updates.append(text)

            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test-deepseek"}):
                asyncio.run(backend._run_codex_attempt(job, on_progress))
            self.assertEqual(job.return_code, 0, job.error)
            self.assertEqual(job.final_message_path.read_text(encoding="utf-8"), "GitHub 已登录: mammut001")
            self.assertTrue(any("🔧 bash" in u for u in updates))

    def test_run_codex_attempt_sums_usage_across_assistant_turns(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            backend = PiBackend(settings)
            fake_pi = tmp / "fake-pi-usage"
            fake_pi.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "sys.stdin.read()\n"
                "def emit(obj):\n"
                "    print(json.dumps(obj), flush=True)\n"
                "emit({\"type\": \"message_end\", \"message\": {\"role\": \"user\", \"usage\": {\"input\": 50, \"output\": 5, \"cacheRead\": 20}, \"content\": [{\"type\": \"text\", \"text\": \"hi\"}]}})\n"
                "emit({\"type\": \"message_end\", \"message\": {\"role\": \"assistant\", \"provider\": \"stepfun\", \"model\": \"step-5-preview\", \"usage\": {\"input\": 100, \"output\": 61, \"cacheRead\": 1024, \"cacheWrite\": 0, \"totalTokens\": 1185}, \"stopReason\": \"stop\", \"content\": [{\"type\": \"text\", \"text\": \"first turn\"}]}})\n"
                "emit({\"type\": \"message_end\", \"message\": {\"role\": \"toolResult\", \"usage\": {\"input\": 10, \"output\": 2, \"cacheRead\": 7}, \"content\": [{\"type\": \"text\", \"text\": \"ok\"}]}})\n"
                "emit({\"type\": \"message_end\", \"message\": {\"role\": \"assistant\", \"usage\": {\"input\": 203, \"output\": 30, \"cacheRead\": 512, \"cacheWrite\": 0, \"totalTokens\": 745}, \"stopReason\": \"stop\", \"content\": [{\"type\": \"text\", \"text\": \"second turn\"}]}})\n"
                "emit({\"type\": \"agent_end\", \"messages\": [{\"role\": \"assistant\", \"content\": [{\"type\": \"text\", \"text\": \"second turn\"}]}]})\n",
                encoding="utf-8",
            )
            fake_pi.chmod(0o755)
            backend.pi_bin = str(fake_pi)

            job = Job(
                "job-usage-1",
                JobMode.RUN,
                "帮我总结一下",
                "unused",
                state=JobState.RUNNING,
                worktree_path=settings.codex_workspace_root,
                log_path=settings.codex_task_root / "job.log",
                final_message_path=settings.codex_task_root / "final.txt",
            )

            async def on_progress(text: str) -> None:
                pass

            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test-deepseek"}):
                asyncio.run(backend._run_codex_attempt(job, on_progress))
            self.assertEqual(job.return_code, 0, job.error)
            # Only the two assistant turns count: (100+1024) + (203+512) input,
            # 1024+512 cacheRead, 61+30 output. The user/toolResult message_end
            # events carry usage too but must be ignored.
            self.assertEqual(
                job.usage,
                {
                    "input_tokens": 1124 + 715,
                    "cached_input_tokens": 1024 + 512,
                    "output_tokens": 61 + 30,
                },
            )

    def test_usage_from_event_requires_assistant_message_and_int_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backend = PiBackend(_make_settings(Path(raw)))
            self.assertIsNone(backend._usage_from_event({"type": "message_end"}))
            self.assertIsNone(
                backend._usage_from_event(
                    {"type": "message_end", "message": {"role": "user", "usage": {"input": 5, "output": 1}}}
                )
            )
            self.assertIsNone(
                backend._usage_from_event({"type": "message_end", "message": {"role": "assistant"}})
            )
            self.assertIsNone(
                backend._usage_from_event(
                    {"type": "message_end", "message": {"role": "assistant", "usage": "nope"}}
                )
            )
            self.assertIsNone(
                backend._usage_from_event({"type": "message_update", "message": {"role": "assistant", "usage": {"input": 5}}})
            )
            # bool / float / string / missing values are all ignored.
            self.assertEqual(
                backend._usage_from_event(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "usage": {
                                "input": "100",
                                "cacheRead": True,
                                "output": 61.5,
                                "cacheWrite": None,
                            },
                        },
                    }
                ),
                {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
            )

    def test_state_from_attempt_file_pi_and_codex_records(self) -> None:
        from runner import metadata as metadata_module

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)

            def attempt(name: str, lines: list[str]) -> Path:
                path = tmp / name
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return path

            state_of = metadata_module._state_from_attempt_file

            # Pi success log: only the ``kind`` envelope exists.
            self.assertEqual(
                state_of(
                    None,
                    attempt(
                        "pi-ok.jsonl",
                        [
                            json.dumps({"kind": "tool.started", "payload": {"name": "bash", "status": "running"}}),
                            json.dumps({"kind": "assistant.completed", "payload": {"text": "done"}}),
                        ],
                    ),
                ),
                "completed",
            )
            # Pi failure log.
            self.assertEqual(
                state_of(
                    None,
                    attempt(
                        "pi-fail.jsonl",
                        [
                            json.dumps({"kind": "assistant.completed", "payload": {"text": "partial"}}),
                            json.dumps({"kind": "agent.error", "payload": {"text": "boom"}}),
                        ],
                    ),
                ),
                "failed",
            )
            # Pi log still in flight.
            self.assertEqual(
                state_of(
                    None,
                    attempt(
                        "pi-running.jsonl",
                        [json.dumps({"kind": "tool.completed", "payload": {"status": "completed"}})],
                    ),
                ),
                "running",
            )
            # Codex logs keep working, and junk lines are skipped.
            self.assertEqual(
                state_of(
                    None,
                    attempt(
                        "codex.jsonl",
                        [
                            "not json at all",
                            json.dumps({"type": "item.completed", "item": {"type": "agent_message"}}),
                            json.dumps([1, 2, 3]),
                            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
                        ],
                    ),
                ),
                "completed",
            )
            self.assertEqual(
                state_of(None, attempt("codex-failed.jsonl", [json.dumps({"type": "turn.failed"})])),
                "failed",
            )
            self.assertEqual(state_of(None, attempt("empty.jsonl", [])), "running")

    def test_web_chat_auto_executes_non_destructive_and_gates_destructive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            runner = PiBackend(settings)
            executed_prompts: list[str] = []

            async def fake_handle_codex_job(msg, port, _runner, *, mode, prompt, wait=False):
                executed_prompts.append(prompt)
                await port.reply(msg, f"已自动执行: {prompt}")

            q1: queue.Queue = queue.Queue()
            msg1 = InboundMessage(
                channel="web",
                operator_id="web-console",
                chat_id="web-auto",
                message_id="m1",
                text="你可以帮我下载 https://example.com/file.txt 吗",
            )
            port1 = WebChatPort(q1, settings, "web:web-console:web-auto", prompt=msg1.text)
            with patch("handlers.jobs.handle_codex_job", new=fake_handle_codex_job):
                asyncio.run(run_web_chat(msg1, port1, settings, runner, msg1.text))
            events_non_delete = _drain_queue(q1)
            self.assertEqual(len(executed_prompts), 1)
            self.assertTrue(any(e == "message" and "已自动执行" in d.get("text", "") for e, d in events_non_delete))
            self.assertFalse(any(e == "approval" for e, _ in events_non_delete))

            q2: queue.Queue = queue.Queue()
            msg2 = InboundMessage(
                channel="web",
                operator_id="web-console",
                chat_id="web-del",
                message_id="m2",
                text="帮我删除 /tmp/file.txt",
            )
            port2 = WebChatPort(q2, settings, "web:web-console:web-del", prompt=msg2.text)
            with patch("handlers.jobs.handle_codex_job", new=fake_handle_codex_job):
                asyncio.run(run_web_chat(msg2, port2, settings, runner, msg2.text))
            events_delete = _drain_queue(q2)
            # Must NOT have executed automatically without confirmation
            self.assertEqual(len(executed_prompts), 1)
            approvals = [d for e, d in events_delete if e == "approval"]
            self.assertEqual(len(approvals), 1)
            self.assertEqual(approvals[0]["tool_name"], "pi.destructive_task")


    def test_normalize_pi_model(self) -> None:
        self.assertEqual(normalize_pi_model("deepseek", "deepseek-flash"), "deepseek-v4-flash")
        self.assertEqual(normalize_pi_model("deepseek", "deepseek-chat"), "deepseek-v4-flash")
        self.assertEqual(normalize_pi_model("deepseek", "deepseek-reasoner"), "deepseek-v4-pro")
        self.assertEqual(normalize_pi_model("openai", "gpt-4o"), "gpt-4o")
        # Non-DeepSeek providers keep their own model names, including "".
        self.assertEqual(normalize_pi_model("stepfun", "step-5-preview"), "step-5-preview")
        self.assertEqual(normalize_pi_model("stepfun", ""), "")
        self.assertEqual(normalize_pi_model("anthropic", "  "), "")
        self.assertEqual(normalize_pi_model("stepfun", "  step-5-preview "), "step-5-preview")

    def test_pi_api_key_env_mapping_fallback_and_override(self) -> None:
        for provider, expected in PI_PROVIDER_KEY_ENV.items():
            self.assertEqual(pi_api_key_env(provider), expected)
        # Unknown provider falls back to a derived, valid env var name.
        self.assertEqual(pi_api_key_env("my-llm"), "MY_LLM_API_KEY")
        self.assertEqual(pi_api_key_env("my.llm.v2"), "MY_LLM_V2_API_KEY")
        self.assertEqual(pi_api_key_env(""), "_API_KEY")
        # A valid override wins for every provider.
        with patch.dict(os.environ, {"CONVEYOR_PI_API_KEY_ENV": "MY_CUSTOM_KEY"}):
            self.assertEqual(pi_api_key_env("stepfun"), "MY_CUSTOM_KEY")
            self.assertEqual(pi_api_key_env("deepseek"), "MY_CUSTOM_KEY")
            self.assertEqual(pi_api_key_env("whatever"), "MY_CUSTOM_KEY")
        # An invalid override is ignored.
        with patch.dict(os.environ, {"CONVEYOR_PI_API_KEY_ENV": "lower_case key"}):
            self.assertEqual(pi_api_key_env("stepfun"), "STEPFUN_API_KEY")
        with patch.dict(os.environ, {"CONVEYOR_PI_API_KEY_ENV": "1BAD"}):
            self.assertEqual(pi_api_key_env("stepfun"), "STEPFUN_API_KEY")
        with patch.dict(os.environ, {"CONVEYOR_PI_API_KEY_ENV": ""}):
            self.assertEqual(pi_api_key_env("deepseek"), "DEEPSEEK_API_KEY")

    def test_pi_command_omits_empty_model(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            settings = _make_settings(Path(raw))
            with patch.dict(os.environ, _base_env(CONVEYOR_PI_PROVIDER="stepfun")):
                backend = PiBackend(settings)
            self.assertEqual(backend.pi_provider, "stepfun")
            self.assertEqual(backend.pi_api_key_env, "STEPFUN_API_KEY")
            self.assertIn(backend.pi_api_key_env, backend._provider_credential_keys())
            job = Job("job-cmd", JobMode.RUN, "hi", "unused")
            command = backend._pi_command(job)
            self.assertIn("--provider", command)
            self.assertIn("stepfun", command)
            if backend.pi_model:
                self.assertIn("--model", command)
            # An empty model must not be passed to the CLI at all; pi 1.5.0
            # requires --model whenever --provider is given, so both are omitted.
            backend.pi_model = ""
            command = backend._pi_command(job)
            self.assertNotIn("--model", command)
            self.assertNotIn("--provider", command)

    def test_run_codex_attempt_stepfun_key_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            dump = tmp / "env.json"
            fake_pi = _write_fake_pi(tmp, "fake-pi-stepfun", dump)
            with patch.dict(
                os.environ,
                _base_env(
                    CONVEYOR_PI_PROVIDER="stepfun",
                    CONVEYOR_PI_MODEL="step-5-preview",
                    CONVEYOR_PI_BASE_URL="https://api.stepfun.com/step_plan/v1",
                    STEPFUN_API_KEY="sk-stepfun-test",
                ),
                clear=True,
            ):
                backend = PiBackend(settings)
                backend.pi_bin = str(fake_pi)
                self.assertEqual(backend.pi_model, "step-5-preview")
                job = Job(
                    "job-stepfun",
                    JobMode.RUN,
                    "hello",
                    "unused",
                    state=JobState.RUNNING,
                    worktree_path=settings.codex_workspace_root,
                    log_path=settings.codex_task_root / "job.log",
                    final_message_path=settings.codex_task_root / "final.txt",
                )
                _run_pi(backend, job)
            self.assertEqual(job.return_code, 0, job.error)
            child_env = json.loads(dump.read_text(encoding="utf-8"))
            # The StepFun key reaches pi...
            self.assertEqual(child_env.get("STEPFUN_API_KEY"), "sk-stepfun-test")
            # ...and the DeepSeek key from settings.chat_api_key is NOT leaked
            # into a non-DeepSeek provider run.
            self.assertNotIn("DEEPSEEK_API_KEY", child_env)

    def test_run_codex_attempt_stepfun_key_with_env_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = dataclasses.replace(_make_settings(tmp), env_isolation=True)
            dump = tmp / "env-isolated.json"
            fake_pi = _write_fake_pi(tmp, "fake-pi-isolated", dump)
            with patch.dict(
                os.environ,
                _base_env(
                    CONVEYOR_PI_PROVIDER="stepfun",
                    CONVEYOR_PI_BASE_URL="https://api.stepfun.com/step_plan/v1",
                    STEPFUN_API_KEY="sk-stepfun-isolated",
                ),
                clear=True,
            ):
                backend = PiBackend(settings)
                backend.pi_bin = str(fake_pi)
                job = Job(
                    "job-stepfun-isolated",
                    JobMode.RUN,
                    "hello",
                    "unused",
                    state=JobState.RUNNING,
                    worktree_path=settings.codex_workspace_root,
                    log_path=settings.codex_task_root / "job.log",
                    final_message_path=settings.codex_task_root / "final.txt",
                )
                _run_pi(backend, job)
            self.assertEqual(job.return_code, 0, job.error)
            child_env = json.loads(dump.read_text(encoding="utf-8"))
            self.assertEqual(child_env.get("STEPFUN_API_KEY"), "sk-stepfun-isolated")
            self.assertNotIn("DEEPSEEK_API_KEY", child_env)

    def test_run_codex_attempt_deepseek_uses_chat_api_key_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            dump = tmp / "env-deepseek.json"
            fake_pi = _write_fake_pi(tmp, "fake-pi-deepseek", dump)
            with patch.dict(os.environ, _base_env(), clear=True):
                backend = PiBackend(settings)
                backend.pi_bin = str(fake_pi)
                job = Job(
                    "job-deepseek",
                    JobMode.RUN,
                    "hello",
                    "unused",
                    state=JobState.RUNNING,
                    worktree_path=settings.codex_workspace_root,
                    log_path=settings.codex_task_root / "job.log",
                    final_message_path=settings.codex_task_root / "final.txt",
                )
                _run_pi(backend, job)
            self.assertEqual(job.return_code, 0, job.error)
            child_env = json.loads(dump.read_text(encoding="utf-8"))
            self.assertEqual(child_env.get("DEEPSEEK_API_KEY"), "sk-test-deepseek")

    def test_run_mode_uses_direct_workspace_without_worktree_churn(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            backend = PiBackend(settings)
            job = Job("run-direct", JobMode.RUN, "check", "unused")
            wt = asyncio.run(backend._create_worktree(job))
            self.assertEqual(wt, settings.codex_workspace_root.resolve())
            self.assertFalse(job.worktree_created)

    def _thinking_job(self, tmp: Path, settings: Settings) -> Job:
        return Job(
            "job-thinking",
            JobMode.RUN,
            "帮我分析一下",
            "unused",
            state=JobState.RUNNING,
            worktree_path=settings.codex_workspace_root,
            log_path=settings.codex_task_root / "job.log",
            final_message_path=settings.codex_task_root / "final.txt",
        )

    def test_run_codex_attempt_emits_thinking_indicator_before_text(self) -> None:
        secret = "SECRET_REASONING"
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            backend = PiBackend(settings)
            events = [
                {"type": "message_start", "message": {"role": "assistant"}},
                {"type": "message_update", "assistantMessageEvent": {"type": "thinking_start"}},
                {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": secret}},
                {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": f"{secret} 继续"}},
                {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": f"{secret} 结束"}},
                {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "已想好"}},
                {
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "content": [{"type": "text", "text": "已想好"}]}],
                },
            ]
            fake_pi = _write_fake_pi_events(tmp, "fake-pi-thinking", events)
            backend.pi_bin = str(fake_pi)
            job = self._thinking_job(tmp, settings)
            updates: list[str] = []

            async def on_progress(text: str) -> None:
                updates.append(text)

            with patch("runner.operators.run.THINKING_THRESHOLD_SECONDS", 0.0):
                asyncio.run(backend._run_codex_attempt(job, on_progress))
            self.assertEqual(job.return_code, 0, job.error)
            # Exactly one indicator for the whole reasoning chain...
            self.assertEqual(updates.count(run_ops.THINKING_INDICATOR), 1)
            # ...and it lands before the streamed prose.
            indicator_at = updates.index(run_ops.THINKING_INDICATOR)
            text_at = next(i for i, u in enumerate(updates) if "已想好" in u)
            self.assertLess(indicator_at, text_at)
            # Reasoning content is never shown or logged.
            for update in updates:
                self.assertNotIn(secret, update)
            self.assertNotIn(secret, job.log_path.read_text(encoding="utf-8"))
            self.assertNotIn(secret, job.final_message_path.read_text(encoding="utf-8"))
            self.assertNotIn(secret, job.last_event)

    def test_run_codex_attempt_thinking_indicator_once_per_chain(self) -> None:
        """Noise events interleaved with reasoning must not break the chain."""
        secret = "SECRET_REASONING"
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            backend = PiBackend(settings)
            events = [
                {"type": "message_update", "assistantMessageEvent": {"type": "thinking_start"}},
                {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": secret}},
                {"type": "turn_start"},
                {"type": "message_start", "message": {"role": "assistant"}},
                {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": secret}},
                {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "完成"}},
                {
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "content": [{"type": "text", "text": "完成"}]}],
                },
            ]
            fake_pi = _write_fake_pi_events(tmp, "fake-pi-thinking-noise", events)
            backend.pi_bin = str(fake_pi)
            job = self._thinking_job(tmp, settings)
            updates: list[str] = []

            async def on_progress(text: str) -> None:
                updates.append(text)

            with patch("runner.operators.run.THINKING_THRESHOLD_SECONDS", 0.0):
                asyncio.run(backend._run_codex_attempt(job, on_progress))
            self.assertEqual(job.return_code, 0, job.error)
            self.assertEqual(updates.count(run_ops.THINKING_INDICATOR), 1)

    def test_run_codex_attempt_thinking_indicator_sent_per_chain(self) -> None:
        """A tool call between two reasoning bursts re-arms the indicator."""
        secret = "SECRET_REASONING"
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            backend = PiBackend(settings)
            events = [
                {"type": "message_update", "assistantMessageEvent": {"type": "thinking_start"}},
                {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": secret}},
                {
                    "type": "tool_execution_start",
                    "toolName": "bash",
                    "toolCallId": "call-1",
                    "args": {"command": "ls"},
                },
                {"type": "tool_execution_end", "toolCallId": "call-1"},
                {"type": "message_update", "assistantMessageEvent": {"type": "thinking_start"}},
                {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": secret}},
                {"type": "message_update", "assistantMessageEvent": {"type": "thinking_end"}},
                {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "搞定"}},
                {
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "content": [{"type": "text", "text": "搞定"}]}],
                },
            ]
            fake_pi = _write_fake_pi_events(tmp, "fake-pi-thinking-chains", events)
            backend.pi_bin = str(fake_pi)
            job = self._thinking_job(tmp, settings)
            updates: list[str] = []

            async def on_progress(text: str) -> None:
                updates.append(text)

            with patch("runner.operators.run.THINKING_THRESHOLD_SECONDS", 0.0):
                asyncio.run(backend._run_codex_attempt(job, on_progress))
            self.assertEqual(job.return_code, 0, job.error)
            self.assertEqual(updates.count(run_ops.THINKING_INDICATOR), 2)
            for update in updates:
                self.assertNotIn(secret, update)
            self.assertNotIn(secret, job.log_path.read_text(encoding="utf-8"))

    def _tool_pulse_job(self, tmp: Path, settings: Settings) -> Job:
        return Job(
            "job-tool-pulse",
            JobMode.RUN,
            "跑一个长命令",
            "unused",
            state=JobState.RUNNING,
            worktree_path=settings.codex_workspace_root,
            log_path=settings.codex_task_root / "job.log",
            final_message_path=settings.codex_task_root / "final.txt",
        )

    _TOOL_START = {
        "type": "tool_execution_start",
        "toolName": "bash",
        "toolCallId": "call-1",
        "args": {"command": "sleep 30"},
    }
    _TOOL_END = {"type": "tool_execution_end", "toolCallId": "call-1"}
    _AGENT_END = {
        "type": "agent_end",
        "messages": [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}],
    }

    def test_run_codex_attempt_pulses_during_silent_tool_call(self) -> None:
        """A silent tool call (pi emits nothing) must pulse on a timer."""
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            backend = PiBackend(settings)
            fake_pi = _write_fake_pi_timeline(
                tmp,
                "fake-pi-tool-pulse",
                [
                    self._TOOL_START,
                    0.6,
                    self._TOOL_END,
                    # Silent gap after the tool finished: no pulse may land
                    # here, the arm is disarmed by tool_execution_end.
                    0.6,
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {"type": "text_delta", "delta": "done"},
                    },
                    self._AGENT_END,
                ],
            )
            backend.pi_bin = str(fake_pi)
            job = self._tool_pulse_job(tmp, settings)
            updates: list[str] = []

            async def on_progress(text: str) -> None:
                updates.append(text)

            with patch("runner.operators.run.TOOL_PULSE_THRESHOLD_SECONDS", 0.1), patch(
                "runner.operators.run.TOOL_PULSE_INTERVAL_SECONDS", 0.1
            ):
                asyncio.run(backend._run_codex_attempt(job, on_progress))
            self.assertEqual(job.return_code, 0, job.error)
            pulses = [
                u for u in updates if re.fullmatch(r"\U0001f527 bash: sleep .* \(\d+s\)\.\.\.", u)
            ]
            # The tool runs 0.6s with a 0.1s threshold / 0.1s interval, so a
            # handful of pulses must reach the chat...
            self.assertGreaterEqual(len(pulses), 2)
            # ...and they stop at tool_execution_end. Had the disarm failed
            # the extra 0.6s of silence would double the count (~12).
            self.assertLessEqual(len(pulses), 8)
            start_at = updates.index("\U0001f527 bash: sleep 30")
            for pulse in pulses:
                self.assertGreater(updates.index(pulse), start_at)
            # Nothing pulses after the tool finished and the prose resumed.
            self.assertEqual(updates[-1], "done \u258d")
            # Final message / result unaffected by the pulse.
            self.assertEqual(job.final_message_path.read_text(encoding="utf-8"), "done")

    def test_run_codex_attempt_no_pulse_for_fast_tool_call(self) -> None:
        """A tool call that completes immediately must not pulse."""
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            backend = PiBackend(settings)
            fake_pi = _write_fake_pi_timeline(
                tmp,
                "fake-pi-tool-fast",
                [self._TOOL_START, self._TOOL_END, self._AGENT_END],
            )
            backend.pi_bin = str(fake_pi)
            job = self._tool_pulse_job(tmp, settings)
            updates: list[str] = []

            async def on_progress(text: str) -> None:
                updates.append(text)

            with patch("runner.operators.run.TOOL_PULSE_THRESHOLD_SECONDS", 0.1), patch(
                "runner.operators.run.TOOL_PULSE_INTERVAL_SECONDS", 0.1
            ):
                asyncio.run(backend._run_codex_attempt(job, on_progress))
            self.assertEqual(job.return_code, 0, job.error)
            self.assertEqual(updates, ["\U0001f527 bash: sleep 30"])
            self.assertEqual(job.final_message_path.read_text(encoding="utf-8"), "done")
            self.assertEqual(job.error, "")

    def test_parse_pi_event_thinking_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backend = PiBackend(_make_settings(Path(raw)))
            for ame_type in ("thinking_start", "thinking_delta"):
                parsed = backend._parse_pi_event(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": ame_type,
                            "delta": "SECRET_REASONING",
                        },
                    }
                )
                self.assertTrue(parsed.thinking)
                self.assertEqual(parsed.progress, "")
                self.assertEqual(parsed.result, "")
                self.assertEqual(parsed.error, "")
                self.assertEqual(parsed.kind, "")
                self.assertIsNone(parsed.payload)
                self.assertEqual(backend._pi_event({"type": "message_update", "assistantMessageEvent": {"type": ame_type}}), ("", ""))
            # thinking_end carries no reasoning either and breaks the chain.
            end = backend._parse_pi_event(
                {"type": "message_update", "assistantMessageEvent": {"type": "thinking_end"}}
            )
            self.assertFalse(end.thinking)
            self.assertEqual(end.progress, "")
            self.assertEqual(end.kind, "")

class PiSystemPromptTests(unittest.TestCase):
    def test_system_prompt_names_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            settings = _make_settings(Path(raw))
            backend = PiBackend(settings)
            job = Job(id="j1", mode=JobMode.RUN, prompt="hi", sandbox="workspace-write")
            self.assertNotIn("project workspace", backend._pi_system_prompt(job))
            job.worktree_path = Path(raw) / "workspace"
            prompt = backend._pi_system_prompt(job)
            self.assertIn(f"The project workspace is {job.worktree_path}", prompt)
            self.assertIn("SAFETY POLICY", prompt)

class PiProviderErrorTests(unittest.TestCase):
    _thinking_job = PiBackendTests._thinking_job

    def test_provider_error_with_exit_zero_writes_no_placeholder_final(self) -> None:
        # Real pi 0.73 exits 0 when the provider answers 404 for an unknown model.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = _make_settings(tmp)
            backend = PiBackend(settings)
            err = '404 The model "step-nonexistent" does not exist'
            events = [
                {"type": "message_end", "message": {"role": "assistant", "content": [],
                                                    "stopReason": "error", "errorMessage": err}},
                {"type": "agent_end", "messages": [{"role": "assistant", "content": [],
                                                    "stopReason": "error", "errorMessage": err}]},
            ]
            backend.pi_bin = str(_write_fake_pi_events(tmp, "fake-pi-404", events))
            job = self._thinking_job(tmp, settings)

            async def on_progress(text: str) -> None:
                pass

            asyncio.run(backend._run_codex_attempt(job, on_progress))
            self.assertEqual(job.return_code, 0)
            self.assertIn("does not exist", job.error)
            self.assertFalse(job.final_message_path.exists())

class PiTimeoutStopTests(unittest.TestCase):
    _thinking_job = PiBackendTests._thinking_job

    """On timeout pi must get SIGTERM first so it can kill its tools' process groups."""

    def _run_with_script(self, tmp: Path, body: str):
        import dataclasses
        settings = dataclasses.replace(_make_settings(tmp), codex_timeout_seconds=1)
        backend = PiBackend(settings)
        script = tmp / "fake-pi-hang"
        script.write_text("#!/bin/bash\n" + body, encoding="utf-8")
        script.chmod(0o755)
        backend.pi_bin = str(script)
        job = self._thinking_job(tmp, settings)

        async def on_progress(text: str) -> None:
            pass

        asyncio.run(backend._run_codex_attempt(job, on_progress))
        return job

    def test_timeout_sends_sigterm_before_kill(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            marker = tmp / "got-term"
            job = self._run_with_script(
                tmp, f"trap 'touch {marker}; exit 143' TERM\ncat >/dev/null\nwhile true; do sleep 0.1; done\n")
            self.assertIn("Timed out", job.error)
            self.assertTrue(marker.exists(), "pi was killed without a chance to clean up")
            self.assertEqual(job.return_code, 143)

    def test_timeout_falls_back_to_sigkill(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch("runner.pi_agent.PI_TERMINATE_GRACE_SECONDS", 0.3):
                job = self._run_with_script(
                    Path(raw), "trap '' TERM\ncat >/dev/null\nwhile true; do sleep 0.1; done\n")
            self.assertIn("Timed out", job.error)
            self.assertEqual(job.return_code, -9)

class PiProviderErrorTextTests(unittest.TestCase):
    def test_common_provider_errors_get_a_chinese_hint_and_keep_the_raw_text(self) -> None:
        from runner.operators.run import _is_rate_limited
        with tempfile.TemporaryDirectory() as raw:
            with patch.dict(os.environ, {"CONVEYOR_PI_PROVIDER": "stepfun", "CONVEYOR_PI_MODEL": "step-5-preview"}):
                backend = PiBackend(_make_settings(Path(raw)))
            quota = backend._explain_provider_error("402 You exceeded your current quota")
            self.assertTrue(quota.startswith("模型服务额度已用完（stepfun）"))
            self.assertIn("402 You exceeded your current quota", quota)
            self.assertIn("STEPFUN_API_KEY", backend._explain_provider_error("401 Invalid API key"))
            self.assertIn("step-5-preview", backend._explain_provider_error('404 The model "x" does not exist'))
            limited = backend._explain_provider_error("429 Too Many Requests")
            job = Job("j", JobMode.RUN, "p", "x", error=limited)
            self.assertTrue(_is_rate_limited(backend, job))  # retry detection still sees 429
            self.assertEqual(backend._explain_provider_error("socket hang up"), "socket hang up")


if __name__ == "__main__":
    unittest.main()
