from __future__ import annotations
import unittest
from unittest.mock import MagicMock
import http.client
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import threading
import asyncio
from conveyor.entrypoints.web_console import WebConsoleServer, WebConsoleHandler
from conveyor.web.web_control import WebControl
from handlers.job_queue import JobQueue

TOKEN = "test-token-0123456789-abcdefghijklmnopqrstuvwxyz"

class TestRoutinesTimezoneAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        td = Path(cls.temp_dir.name)
        cls.settings = SimpleNamespace(
            codex_memory_root=td,
            codex_task_root=td,
            user_timezone="Asia/Shanghai",
            routines_enabled=True,
            conveyor_web_enabled=True,
            conveyor_web_host="127.0.0.1",
            conveyor_web_port=0,
            conveyor_web_token=TOKEN,
            chat_mode="auto",
            chat_tools_enabled=True,
            conveyor_event_retention_per_job=2000,
        )
        cls.queue = JobQueue()
        cls.queue.configure(cls.settings, runner=None, recover=False)
        cls.control = WebControl(cls.settings, runner=None, queue=cls.queue)

        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()

        # We need to mock routines.list_routines because we don't want to test routines functionality, just the API
        with unittest.mock.patch("conveyor.automation.routines.list_routines", return_value=[]):
            cls.server = WebConsoleServer(
                ("127.0.0.1", 0), WebConsoleHandler,
                control=cls.control, loop=cls.loop, token=TOKEN,
            )
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server_thread.join()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.loop_thread.join()
        cls.temp_dir.cleanup()

    def test_timezone_in_api(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"}
        conn.request("GET", "/api/routines", headers=headers)
        res = conn.getresponse()
        data = json.loads(res.read().decode("utf-8"))
        conn.close()
        
        self.assertEqual(res.status, 200)
        self.assertEqual(data["timezone"], "Asia/Shanghai")
