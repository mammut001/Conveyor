"""Offline Feishu audio tests: fake Feishu message-resource API and fake STT."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from channel.feishu import FeishuOutbound
from channel.feishu_audio import handle_audio, is_audio_message
from channel.types import InboundMessage


class FakeFeishu:
    def __init__(self, body: bytes = b"opus payload") -> None:
        self.body = body
        self.downloads: list[tuple] = []
        self.sent: list[tuple] = []

    async def download_resource(self, key, kind, message_id=None):
        self.downloads.append((key, kind, message_id))
        return self.body

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
        self.assertEqual(self.api.downloads, [("file_123", "file", "om_1")])
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
        self.assertEqual(self.api.downloads, [])
        self.dispatch.assert_not_awaited()
        self.assertIn("size limit", self.api.sent[0][1]["elements"][0]["content"])

    async def test_oversize_response_rejected_without_stt(self):
        self.api.body = b"x" * 1025
        with patch("channel.feishu_audio.transcribe_async", new_callable=AsyncMock) as stt:
            self.assertTrue(await self.send())
        stt.assert_not_awaited()
        self.dispatch.assert_not_awaited()

    async def test_duration_limit_rejected_before_download(self):
        self.event.content.duration_ms = 121000
        self.assertTrue(await self.send())
        self.assertEqual(self.api.downloads, [])
        self.dispatch.assert_not_awaited()

    async def test_unauthorized_sender_ignored(self):
        self.inbound = InboundMessage(
            channel="feishu", operator_id="ou_stranger", chat_id="oc_1",
            message_id="om_1", text="", chat_type="p2p",
        )
        self.assertTrue(await self.send())
        self.assertEqual(self.api.downloads, [])
        self.assertEqual(self.api.sent, [])
        self.dispatch.assert_not_awaited()

    async def test_group_without_mention_ignored(self):
        self.inbound = InboundMessage(
            channel="feishu", operator_id="ou_operator", chat_id="oc_group",
            message_id="om_1", text="", chat_type="group",
        )
        self.assertTrue(await self.send())
        self.assertEqual(self.api.downloads, [])
        self.assertEqual(self.api.sent, [])

    async def test_flag_off_preserves_previous_dispatch(self):
        os.environ["CONVEYOR_FEISHU_AUDIO"] = "0"
        self.assertFalse(await self.send())
        self.assertEqual(self.api.downloads, [])
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

    async def test_requires_explicit_audio_type(self):
        self.event.raw_content_type = "text"
        self.event.content.kind = "text"
        self.assertFalse(is_audio_message(self.event))
        self.assertFalse(await self.send())
        self.api.downloads.clear()
        self.assertEqual(self.api.sent, [])


if __name__ == "__main__":
    unittest.main()
