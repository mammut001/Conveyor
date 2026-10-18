"""Use a loopback provider to verify deadlines and physical connection cleanup."""
from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from runner.chat_client import ChatConfig, ChatError, complete_chat, stream_chat


class Provider(BaseHTTPRequestHandler):
    started = threading.Event()
    disconnected = threading.Event()
    stop = threading.Event()

    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        mode = payload['messages'][0]['content']
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream' if payload['stream'] else 'application/json')
        if not payload['stream']:
            self.send_header('Content-Length', '1000000')
        self.end_headers()
        Provider.started.set()
        try:
            if mode == 'provider-error':
                obj = {'base_resp': {'status_code': 1, 'message': 'opaque-provider-credential'}}
                self.wfile.write(('data: ' + json.dumps(obj) + '\n\n').encode())
                self.wfile.flush()
                return
            if mode == 'partial':
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"partial answer"}}]}\n\n')
                self.wfile.flush()
            while not Provider.stop.wait(0.02):
                self.wfile.write(b': heartbeat\n\n' if payload['stream'] else b' ')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            Provider.disconnected.set()


class ChatClientLifecycleTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        Provider.stop.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        Provider.started.clear()
        Provider.disconnected.clear()

    def config(self, timeout=5):
        return ChatConfig(base_url=f'http://127.0.0.1:{self.server.server_port}',
                          api_key='opaque-provider-credential', model='test', timeout=timeout)

    async def assert_disconnected(self):
        self.assertTrue(await asyncio.to_thread(Provider.disconnected.wait, 2),
                        'provider socket must close when the caller stops waiting')

    async def test_heartbeats_do_not_extend_stream_deadline(self):
        started = time.monotonic()
        with self.assertRaisesRegex(ChatError, 'timed out'):
            async for _ in stream_chat(self.config(timeout=0.5), [{'role':'user','content':'heartbeat'}]):
                pass
        self.assertLess(time.monotonic() - started, 2)
        await self.assert_disconnected()

    async def test_trickled_body_does_not_extend_completion_deadline(self):
        started = time.monotonic()
        with self.assertRaisesRegex(ChatError, 'timed out'):
            await complete_chat(self.config(timeout=0.5), [{'role':'user','content':'trickle'}])
        self.assertLess(time.monotonic() - started, 2)
        await self.assert_disconnected()

    async def test_closing_partial_stream_disconnects_provider(self):
        reply = stream_chat(self.config(), [{'role':'user','content':'partial'}])
        self.assertEqual(await reply.__anext__(), 'partial answer')
        await reply.aclose()
        await self.assert_disconnected()

    async def test_cancelling_a_waiting_stream_disconnects_provider(self):
        async def collect():
            async for _ in stream_chat(self.config(), [{'role':'user','content':'heartbeat'}]):
                pass
        task = asyncio.create_task(collect())
        self.assertTrue(await asyncio.to_thread(Provider.started.wait, 2))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.assert_disconnected()

    async def test_cancelling_completion_disconnects_provider(self):
        task = asyncio.create_task(complete_chat(self.config(), [{'role':'user','content':'trickle'}]))
        self.assertTrue(await asyncio.to_thread(Provider.started.wait, 2))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.assert_disconnected()

    async def test_stream_provider_error_redacts_opaque_key(self):
        with self.assertRaises(ChatError) as caught:
            async for _ in stream_chat(self.config(), [{'role':'user','content':'provider-error'}]):
                pass
        self.assertNotIn('opaque-provider-credential', str(caught.exception))
        self.assertIn('[REDACTED]', str(caught.exception))
