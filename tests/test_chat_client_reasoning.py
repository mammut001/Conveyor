"""Reasoning models must not spend the whole budget on hidden reasoning.

StepFun step-3.7-flash answers ``finish_reason="length"`` with an empty
``content`` and the whole budget consumed by ``reasoning`` /
``reasoning_content``, so the operator would get a blank reply. The client
therefore (a) sends an optional ``reasoning_effort`` hint and (b) replays the
request once with a wider ``max_tokens`` when a reply comes back empty.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from conveyor.core.config import Settings, _reasoning_effort_env, load_runtime_settings
from runner.chat_client import (
    ChatConfig,
    _expanded_max_tokens,
    _reasoning_exhausted,
    clean_reasoning_effort,
    complete_chat,
    config_from_settings,
    stream_chat,
)

# Capture the real class before patching; MockTransport stands in for the wire.
REAL_ASYNC_CLIENT = httpx.AsyncClient

REASONING = "thinking about whether 2027 has divisors " * 60
MESSAGES = [{"role": "user", "content": "Is 2027 prime? Answer in one short sentence."}]


# ---- provider doubles ------------------------------------------------------


def _json(obj) -> httpx.Response:
    return httpx.Response(200, json=obj)


def _sse(chunks) -> httpx.Response:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(), headers={"Content-Type": "text/event-stream"})


def _exhausted_completion() -> dict:
    """What the live StepFun API returns when reasoning eats the budget."""
    return {
        "choices": [{
            "finish_reason": "length",
            "message": {"role": "assistant", "content": "", "reasoning": REASONING,
                        "reasoning_content": REASONING},
        }],
        "usage": {"completion_tokens": 1500},
    }


def _answer_completion(text: str = "2027 is prime.") -> dict:
    return {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        "usage": {"completion_tokens": 12},
    }


def _reasoning_deltas() -> list[dict]:
    """SSE deltas that carry reasoning only, then finish_reason=length."""
    out = [{"choices": [{"delta": {"role": "assistant", "reasoning": REASONING[:900]}}]}]
    out += [{"choices": [{"delta": {"reasoning_content": REASONING[:900]}}]}]
    out += [{"choices": [{"delta": {}, "finish_reason": "length"}]}]
    return out


def _content_deltas(text: str = "2027 is prime.") -> list[dict]:
    out = [{"choices": [{"delta": {"role": "assistant", "content": chunk}}]}
           for chunk in (text[i:i + 5] for i in range(0, len(text), 5))]
    out += [{"choices": [{"delta": {}, "finish_reason": "stop"}]}]
    return out


class ScriptedProvider:
    """Serves queued responses and records every request body."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        return self.responses.pop(0) if self.responses else _answer_completion()

    @contextlib.contextmanager
    def patched(self):
        def factory(**kw):
            kw.setdefault("transport", httpx.MockTransport(self))
            return REAL_ASYNC_CLIENT(**kw)

        with mock.patch("runner.chat_client.httpx.AsyncClient", factory):
            yield


def _config(**kw) -> ChatConfig:
    base = dict(base_url="https://chat.example/v1", api_key="k", model="step-3.7-flash", timeout=10)
    base.update(kw)
    return ChatConfig(**base)


def _collect(config: ChatConfig) -> list[str]:
    async def run():
        return [chunk async for chunk in stream_chat(config, MESSAGES)]

    return asyncio.run(run())


class ReasoningExhaustionTests(unittest.TestCase):
    def test_complete_chat_retries_with_wider_budget(self):
        provider = ScriptedProvider(_json(_exhausted_completion()), _json(_answer_completion()))
        with provider.patched():
            out = asyncio.run(complete_chat(_config(max_tokens=1500), MESSAGES))
        self.assertEqual(out["content"], "2027 is prime.")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(provider.requests[0]["max_tokens"], 1500)
        self.assertEqual(provider.requests[1]["max_tokens"], 6000)

    def test_complete_chat_retries_at_most_once(self):
        provider = ScriptedProvider(_json(_exhausted_completion()), _json(_exhausted_completion()))
        with provider.patched():
            out = asyncio.run(complete_chat(_config(max_tokens=1500), MESSAGES))
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(out["content"], "")  # second result returned as-is
        self.assertEqual(out["finish_reason"], "length")

    def test_complete_chat_normal_response_sends_one_request(self):
        provider = ScriptedProvider(_json(_answer_completion()))
        with provider.patched():
            out = asyncio.run(complete_chat(_config(), MESSAGES))
        self.assertEqual(out["content"], "2027 is prime.")
        self.assertEqual(len(provider.requests), 1)

    def test_complete_chat_length_with_content_is_not_retried(self):
        body = _answer_completion()
        body["choices"][0]["finish_reason"] = "length"
        body["choices"][0]["message"]["reasoning"] = REASONING
        provider = ScriptedProvider(_json(body))
        with provider.patched():
            out = asyncio.run(complete_chat(_config(), MESSAGES))
        self.assertEqual(out["content"], "2027 is prime.")
        self.assertEqual(len(provider.requests), 1)

    def test_complete_chat_tool_calls_are_not_retried(self):
        body = {
            "choices": [{
                "finish_reason": "length",
                "message": {"role": "assistant", "content": "", "reasoning": REASONING,
                            "tool_calls": [{"id": "c1", "type": "function",
                                            "function": {"name": "f", "arguments": "{}"}}]},
            }],
        }
        provider = ScriptedProvider(_json(body))
        with provider.patched():
            out = asyncio.run(complete_chat(_config(), MESSAGES, tools=[{"type": "function"}]))
        self.assertEqual(len(provider.requests), 1)
        self.assertTrue(out["tool_calls"])

    def test_stream_chat_retries_reasoning_only_stream(self):
        provider = ScriptedProvider(_sse(_reasoning_deltas()), _sse(_content_deltas()))
        with provider.patched():
            chunks = _collect(_config(max_tokens=1500))
        self.assertEqual("".join(chunks), "2027 is prime.")
        self.assertNotIn(REASONING, "".join(chunks))
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(provider.requests[0]["max_tokens"], 1500)
        self.assertEqual(provider.requests[1]["max_tokens"], 6000)

    def test_stream_chat_retries_at_most_once(self):
        provider = ScriptedProvider(_sse(_reasoning_deltas()), _sse(_reasoning_deltas()))
        with provider.patched():
            chunks = _collect(_config(max_tokens=1500))
        self.assertEqual(chunks, [])
        self.assertEqual(len(provider.requests), 2)

    def test_stream_chat_content_stream_is_not_retried(self):
        deltas = _content_deltas()
        deltas[-1]["choices"][0]["finish_reason"] = "length"
        deltas[0]["choices"][0]["delta"]["reasoning"] = REASONING
        provider = ScriptedProvider(_sse(deltas))
        with provider.patched():
            chunks = _collect(_config())
        self.assertEqual("".join(chunks), "2027 is prime.")
        self.assertEqual(len(provider.requests), 1)

    def test_stream_chat_json_body_is_retried_too(self):
        provider = ScriptedProvider(_json(_exhausted_completion()), _json(_answer_completion("ok")))
        with provider.patched():
            chunks = _collect(_config(max_tokens=1500))
        self.assertEqual("".join(chunks), "ok")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(provider.requests[1]["max_tokens"], 6000)


class ReasoningEffortTests(unittest.TestCase):
    def test_payload_includes_effort_only_when_configured(self):
        provider = ScriptedProvider(_json(_answer_completion()))
        with provider.patched():
            asyncio.run(complete_chat(_config(reasoning_effort="low"), MESSAGES))
        self.assertEqual(provider.requests[0]["reasoning_effort"], "low")

        provider = ScriptedProvider(_json(_answer_completion()))
        with provider.patched():
            asyncio.run(complete_chat(_config(), MESSAGES))
        self.assertNotIn("reasoning_effort", provider.requests[0])

    def test_stream_payload_includes_effort(self):
        provider = ScriptedProvider(_sse(_content_deltas()))
        with provider.patched():
            _collect(_config(reasoning_effort="medium"))
        self.assertEqual(provider.requests[0]["reasoning_effort"], "medium")

    def test_unknown_effort_is_dropped(self):
        self.assertIsNone(clean_reasoning_effort("turbo"))
        self.assertIsNone(clean_reasoning_effort(None))
        self.assertEqual(clean_reasoning_effort(" LOW "), "low")

    def test_config_from_settings_reads_effort(self):
        base = SimpleNamespace(
            chat_mode="auto", chat_base_url=None, chat_api_key="k", chat_model="m",
            chat_timeout_seconds=30, chat_max_tokens=1500, chat_reasoning_effort="low",
        )
        self.assertEqual(config_from_settings(base).reasoning_effort, "low")
        without = SimpleNamespace(**{**base.__dict__, "chat_reasoning_effort": None})
        self.assertIsNone(config_from_settings(without).reasoning_effort)
        bogus = SimpleNamespace(**{**base.__dict__, "chat_reasoning_effort": "turbo"})
        self.assertIsNone(config_from_settings(bogus).reasoning_effort)
        missing = SimpleNamespace(
            chat_mode="auto", chat_api_key="k", chat_model="m",
            chat_timeout_seconds=30, chat_max_tokens=1500,
        )
        self.assertIsNone(config_from_settings(missing).reasoning_effort)
        self.assertIsNone(config_from_settings(SimpleNamespace(chat_mode="off")))


class ReasoningEffortEnvTests(unittest.TestCase):
    def test_env_parsing(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONVEYOR_CHAT_REASONING_EFFORT", None)
            self.assertIsNone(_reasoning_effort_env())
            for raw, expected in (("LOW", "low"), (" High ", "high"), ("turbo", None), ("", None)):
                os.environ["CONVEYOR_CHAT_REASONING_EFFORT"] = raw
                self.assertEqual(_reasoning_effort_env(), expected, raw)
            os.environ.pop("CONVEYOR_CHAT_REASONING_EFFORT", None)

    def test_settings_load_effort_from_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / "settings.env"
            env.write_text(
                "TELEGRAM_BOT_TOKEN=t\nTELEGRAM_ALLOWED_USER_ID=1\n"
                f"CODEX_WORKSPACE_ROOT={root / 'ws'}\nCODEX_TASK_ROOT={root / 'tasks'}\n"
                f"CODEX_MEMORY_ROOT={root / 'mem'}\n"
            )
            # One patch.dict block so load_dotenv cannot leak into other tests.
            with mock.patch.dict(os.environ, {}, clear=False):
                for raw, expected in (("LOW", "low"), ("turbo", None)):
                    os.environ["CONVEYOR_CHAT_REASONING_EFFORT"] = raw
                    self.assertEqual(load_runtime_settings(env_file=env).chat_reasoning_effort, expected)
                os.environ.pop("CONVEYOR_CHAT_REASONING_EFFORT", None)
                self.assertIsNone(load_runtime_settings(env_file=env).chat_reasoning_effort)


class ReasoningExhaustionHelperTests(unittest.TestCase):
    def test_expanded_budget(self):
        self.assertEqual(_expanded_max_tokens(1500), 6000)
        self.assertEqual(_expanded_max_tokens(160), 4096)
        self.assertEqual(_expanded_max_tokens(16384), 16384)

    def test_exhaustion_predicate(self):
        self.assertTrue(_reasoning_exhausted(
            {"content": "", "reasoning": REASONING}, "length"))
        self.assertTrue(_reasoning_exhausted(
            {"content": None, "reasoning_content": "hmm"}, "length"))
        self.assertTrue(_reasoning_exhausted({"content": "   ", "reasoning": "hmm"}, "length"))
        self.assertFalse(_reasoning_exhausted(
            {"content": "answer", "reasoning": REASONING}, "length"))
        self.assertFalse(_reasoning_exhausted({"content": "", "reasoning": "hmm"}, "stop"))
        self.assertFalse(_reasoning_exhausted(
            {"content": "", "reasoning": "hmm", "tool_calls": [{"id": "1"}]}, "length"))
        self.assertFalse(_reasoning_exhausted({"content": ""}, "length"))
        self.assertFalse(_reasoning_exhausted(None, "length"))

    def test_settings_field_default(self):
        self.assertIsNone(Settings.__dataclass_fields__["chat_reasoning_effort"].default)


if __name__ == "__main__":
    unittest.main()
