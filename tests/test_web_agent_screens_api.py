"""PR6 authenticated HTTP API and CSRF regression checks."""
from __future__ import annotations

import asyncio
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import agent_context as ac
import agent_mailbox as mailbox
from agents import AgentStore, WEB_OPERATOR, chat_id_for
from handlers.job_queue import JobQueue
from tests.test_web_agent_screens import test_settings
from web_console import WebConsoleHandler, WebConsoleServer
from web_control import WebControl


class AgentScreensHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.settings = test_settings(Path(cls.temp.name))
        cls.flags = patch.dict(os.environ, {
            "CONVEYOR_NAMED_AGENTS":"1", "CONVEYOR_AGENT_CONTEXT":"1",
            "CONVEYOR_AGENT_MAILBOX":"1", "CONVEYOR_AGENT_DELEGATION":"1"})
        cls.flags.start()
        cls.store = AgentStore(cls.settings)
        cls.a = cls.store.create({"name":"Alpha","tools_allowlist":[]})
        cls.b = cls.store.create({"name":"Beta","tools_allowlist":[]})
        cls.queue = JobQueue()
        cls.queue.configure(cls.settings, runner=None, recover=False)
        cls.control = WebControl(cls.settings, runner=None, queue=cls.queue)
        cls.loop = asyncio.new_event_loop()
        cls.thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.thread.start()
        cls.server = WebConsoleServer(("127.0.0.1", 0), WebConsoleHandler,
                                      control=cls.control, loop=cls.loop,
                                      token="private-screens-token")
        cls.web_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.web_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.thread.join(timeout=2)
        cls.web_thread.join(timeout=2)
        cls.flags.stop()
        cls.temp.cleanup()

    def req(self, method, url, body=None, *, auth=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=6)
        hdr = {"Content-Type":"application/json"}
        if auth:
            hdr["Authorization"] = "Bearer private-screens-token"
        hdr.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, url, body=data, headers=hdr)
        response = conn.getresponse()
        raw = response.read()
        status = response.status
        conn.close()
        return status, json.loads(raw or b"{}")

    def test_scoped_get_requires_auth_and_id(self):
        self.assertEqual(self.req("GET", "/api/agent-screens/config", auth=False)[0], 401)
        status, flags = self.req("GET", "/api/agent-screens/config")
        self.assertEqual(status, 200)
        self.assertTrue(flags["enabled"])
        self.assertTrue(flags["inbox"])
        self.assertTrue(flags["tasks"])
        self.assertEqual(self.req("GET", "/api/agent-screens?view=inbox")[0], 400)
        self.assertEqual(self.req("GET",
            "/api/agent-screens?agent_id=not-real&view=inbox")[0], 404)

    def test_http_xss_payload_is_data_and_operator_scoped(self):
        a = ac.resolve(self.settings, "web", chat_id_for(self.a["id"]), WEB_OPERATOR)
        malicious = '<svg onload="alert(1)"><script>alert(2)</script></svg>'
        with ac.bind(a):
            row = mailbox.send(self.settings, sender=a, to_agent=self.b["id"],
                               kind="message", body=malicious, idempotency_key="http-xss")
        url = f"/api/agent-screens?agent_id={self.b['id']}&view=inbox"
        status, body = self.req("GET", url)
        self.assertEqual(status, 200)
        item = next(x for x in body["items"] if x["id"] == row["id"])
        self.assertEqual(item["body"], malicious)
        self.assertTrue(item["untrusted"])
        self.assertFalse(item["approval_authority"])
        with patch.dict(os.environ, {"CONVEYOR_AGENT_CONTEXT":"0"}):
            self.assertEqual(self.req("GET", url)[0], 404)

    def test_cancel_requires_same_origin_ajax_and_correct_agent(self):
        a = ac.resolve(self.settings, "web", chat_id_for(self.a["id"]), WEB_OPERATOR)
        with ac.bind(a):
            task = mailbox.send(self.settings, sender=a, to_agent=self.b["id"],
                                kind="task", body='{"v":1,"task":"test","tools":[]}',
                                idempotency_key="http-cancel")
        url = "/api/agent-screens/cancel"
        payload = {"agent_id":self.b["id"], "task_id":task["id"]}
        ajax = {"X-Requested-With":"XMLHttpRequest"}
        self.assertEqual(self.req("POST", url, payload, auth=False, headers=ajax)[0], 401)
        self.assertEqual(self.req("POST", url, payload)[0], 403)
        self.assertEqual(self.req("POST", url, payload, headers={
            **ajax, "Origin":"https://evil.invalid"})[0], 403)
        self.assertEqual(self.req("POST", url, {
            **payload, "agent_id":"unknown"}, headers=ajax)[0], 404)
        code, result = self.req("POST", url, payload, headers=ajax)
        self.assertEqual(code, 200)
        self.assertTrue(result["cancelled"])
        code, result = self.req("POST", url, payload, headers=ajax)
        self.assertEqual(code, 200)
        self.assertFalse(result["cancelled"])


if __name__ == "__main__":
    unittest.main()
