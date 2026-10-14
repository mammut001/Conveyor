"""Offline Feishu audio tests: fake Feishu message-resource API and fake STT."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from channel.feishu import FeishuOutbound
from channel.feishu_audio import handle_audio, is_audio_message, temporary_audio
from channel.types import InboundMessage


class FakeResponse:
    """Fake potentially huge network body with observable limited reads."""
    def __init__(self, total: int, headers=None, body=None):
        self.size = total
        self.body = body
        self.headers = headers or {}
        self.status = 200
        self.position = 0
        self.read_calls = []
        self.closed = False

    def read(self, n):
        assert n > 0
        self.read_calls.append(n)
        taken = min(n, self.size - self.position)
        self.position += taken
        if self.body is None:
            return b"x" * taken
        return self.body[self.position - taken:self.position]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


class FakeFeishu:
    def __init__(self) -> None:
        self._client = SimpleNamespace(config=SimpleNamespace(
            domain="https://open.feishu.cn", timeout=5,
        ))
        self.sent: list[tuple] = []

    async def send(self, chat_id, payload, options=None):
        self.sent.append((chat_id, payload, options))
        return SimpleNamespace(ok=True, message_id="om_reply")


class FeishuAudioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "CONVEYOR_FEISHU_AUDIO": "1", "CONVEYOR_STT_ENABLED": "1",
            "CONVEYOR_STT_MAX_BYTES": "1024",
            "CONVEYOR_STT_MAX_DURATION_SECONDS": "120",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = FakeFeishu()
        self.port = FeishuOutbound(self.api)
        self.response = FakeResponse(12, body=b"opus payload")
        self.requests = []
        def open_fake(request, *, timeout):
            self.requests.append(request)
            return self.response
        self.open_patch = patch("channel.feishu_audio._open_resource", side_effect=open_fake)
        self.open_patch.start()
        self.addCleanup(self.open_patch.stop)
        self.token_patch = patch("channel.feishu_audio._tenant_token", return_value="fake-tenant")
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)
        self.temp_paths = []
        original_temporary_audio = temporary_audio
        @contextmanager
        def tracked_temporary_audio(suffix):
            with original_temporary_audio(suffix) as path:
                self.temp_paths.append(path)
                yield path
        self.tmp_patch = patch("channel.feishu_audio.temporary_audio", tracked_temporary_audio)
        self.tmp_patch.start()
        self.addCleanup(self.tmp_patch.stop)
        self.settings = SimpleNamespace(lark_allowed_open_id="ou_operator")
        self.inbound = InboundMessage(
            channel="feishu", operator_id="ou_operator", chat_id="oc_1",
            message_id="om_1", text='<audio key="file_123"/>', chat_type="p2p",
        )
        content = SimpleNamespace(kind="audio", file_key="file_123",
                                  duration_ms=2000, raw={})
        self.event = SimpleNamespace(raw_content_type="audio", content=content,
                                     resources=[])
        self.dispatch = AsyncMock()

    async def send(self):
        return await handle_audio(
            self.event, self.inbound, port=self.port, settings=self.settings,
            dispatch_text=self.dispatch,
        )

    async def test_happy_path_resource_type_preview_dispatch_and_cleanup(self):
        observed: list[Path] = []
        def fake_convert(source: Path, destination: Path) -> None:
            self.assertEqual(source.read_bytes(), b"opus payload")
            observed.extend((source, destination))
            destination.write_bytes(b"ogg data")
        async def fake_stt(path: Path):
            self.assertEqual(path.suffix, ".ogg")
            self.assertEqual(path.read_bytes(), b"ogg data")
            return "/run safe-command"
        with patch("channel.feishu_audio._convert_opus", side_effect=fake_convert), \
             patch("channel.feishu_audio.transcribe_async", side_effect=fake_stt):
            self.assertTrue(await self.send())
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].full_url,
            "https://open.feishu.cn/open-apis/im/v1/messages/om_1/resources/file_123?type=file")
        self.assertEqual(self.requests[0].get_header("Authorization"), "Bearer fake-tenant")
        self.assertTrue(self.response.closed)
        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.api.sent[0][2], {"reply_to": "om_1"})
        self.assertIn("🎤 heard: /run safe-command",
                      self.api.sent[0][1]["elements"][0]["content"])
        self.dispatch.assert_awaited_once_with("/run safe-command")
        self.assertTrue(observed)
        for path in observed:
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())

    async def test_oversize_metadata_rejected_before_download(self):
        self.event.content.raw["file_size"] = 1025
        self.assertTrue(await self.send())
        self.assertEqual(self.requests, [])
        self.dispatch.assert_not_awaited()
        self.assertIn("size limit", self.api.sent[0][1]["elements"][0]["content"])

    async def test_oversize_response_rejected_without_stt(self):
        self.response = FakeResponse(1025)
        with patch("channel.feishu_audio.transcribe_async", new_callable=AsyncMock) as stt:
            self.assertTrue(await self.send())
        stt.assert_not_awaited()
        self.dispatch.assert_not_awaited()

    async def test_duration_limit_rejected_before_download(self):
        self.event.content.duration_ms = 121000
        self.assertTrue(await self.send())
        self.assertEqual(self.requests, [])
        self.dispatch.assert_not_awaited()

    async def test_unauthorized_sender_ignored(self):
        self.inbound = InboundMessage(
            channel="feishu", operator_id="ou_stranger", chat_id="oc_1",
            message_id="om_1", text="", chat_type="p2p",
        )
        self.assertTrue(await self.send())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.api.sent, [])
        self.dispatch.assert_not_awaited()

    async def test_group_without_mention_ignored(self):
        self.inbound = InboundMessage(
            channel="feishu", operator_id="ou_operator", chat_id="oc_group",
            message_id="om_1", text="", chat_type="group",
        )
        self.assertTrue(await self.send())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.api.sent, [])

    async def test_flag_off_preserves_previous_dispatch(self):
        os.environ["CONVEYOR_FEISHU_AUDIO"] = "0"
        self.assertFalse(await self.send())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.api.sent, [])
        self.dispatch.assert_not_awaited()
        os.environ["CONVEYOR_FEISHU_AUDIO"] = "1"
        os.environ["CONVEYOR_STT_ENABLED"] = "0"
        self.assertFalse(await self.send())

    async def test_stt_failure_removes_both_files(self):
        paths: list[Path] = []
        def fake_convert(source, destination):
            paths.extend((source, destination))
            destination.write_bytes(b"ogg")
        with patch("channel.feishu_audio._convert_opus", side_effect=fake_convert), \
             patch("channel.feishu_audio.transcribe_async", new=AsyncMock(
                 side_effect=RuntimeError("fake provider private failure"))):
            self.assertTrue(await self.send())
        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("Could not process", self.api.sent[0][1]["elements"][0]["content"])
        for path in paths:
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())
        self.dispatch.assert_not_awaited()

    async def _assert_rejected_stream(self, headers, expected_read):
        # A 50 MiB response must not be buffered (the fake allocates nothing
        # beyond the requested chunk).
        self.response = FakeResponse(50 * 1024 * 1024, headers=headers)
        with patch("channel.feishu_audio._convert_opus") as convert, \
             patch("channel.feishu_audio.transcribe_async", new_callable=AsyncMock) as stt:
            self.assertTrue(await self.send())
        self.assertEqual(len(self.requests), 1)
        self.assertLessEqual(self.response.position, 1025)  # max_bytes()+1
        self.assertEqual(self.response.position, expected_read)
        self.assertTrue(self.response.closed)
        self.assertEqual(len(self.temp_paths), 1)
        for path in self.temp_paths:
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())
        convert.assert_not_called()
        stt.assert_not_awaited()
        self.dispatch.assert_not_awaited()
        self.assertIn("size limit", self.api.sent[0][1]["elements"][0]["content"])

    async def test_stream_without_content_length_stops_at_limit_plus_one(self):
        await self._assert_rejected_stream({}, 1025)

    async def test_stream_lying_small_content_length_stops_at_limit_plus_one(self):
        await self._assert_rejected_stream({"Content-Length": "100"}, 1025)

    async def test_stream_oversize_content_length_reads_zero_bytes(self):
        await self._assert_rejected_stream({"Content-Length": "52428800"}, 0)

    async def test_invalid_file_key_rejected_before_api(self):
        self.event.content.file_key = "../../leak"
        self.assertTrue(await self.send())
        self.assertEqual(self.requests, [])
        self.dispatch.assert_not_awaited()

    async def test_requires_explicit_audio_type(self):
        self.event.raw_content_type = "text"
        self.event.content.kind = "text"
        self.assertFalse(is_audio_message(self.event))
        self.assertFalse(await self.send())
        self.requests.clear()
        self.assertEqual(self.api.sent, [])


if __name__ == "__main__":
    unittest.main()
