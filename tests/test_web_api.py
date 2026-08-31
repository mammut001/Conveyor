from __future__ import annotations

import asyncio
import http.client
import json
import threading
import unittest
from unittest.mock import patch

from web_console import WebConsoleHandler, WebConsoleServer


TOKEN = "test-token-0123456789-abcdefghijklmnopqrstuvwxyz"


class FakeJob:
    id = "q2"


class FakeControl:
    runner = object()

    def system_status(self):
        return {"uptime_seconds": 1, "queue": {"depth": 0}}

    def list_sessions(self):
        return [{"id": "web-a", "title": "Session"}]

    def get_session(self, session_id):
        return {"id": session_id, "jobs": []} if session_id == "web-a" else None

    def list_jobs(self, _limit=100):
        return [{"id": "q1", "state": "running", "chat_id": "web-a"}]

    def get_job(self, job_id):
        return {"id": job_id, "state": "running"} if job_id == "q1" else None

    def events(self, job_id, after=0, limit=500):
        if job_id == "q1" and after < 1:
            return [{"schema_version": 1, "event_id": "event-1", "sequence": 1,
                     "timestamp": "2026-01-01T00:00:00Z", "kind": "task.started",
                     "job_id": "q1", "payload": {}}]
        return []

    async def diff(self, job_id):
        return {"job_id": job_id, "diff": "@@ test"} if job_id == "q1" else None

    def list_approvals(self):
        return [{"id": "approval-1", "job_id": "q1", "status": "pending"}]

    def request_approval(self, job_id, action):
        if job_id != "q1":
            raise KeyError(job_id)
        return {"id": "approval-1", "job_id": job_id, "action": action, "status": "pending"}

    async def decide_approval(self, approval_id, approve):
        return {"id": approval_id, "status": "accepted" if approve else "rejected"}

    async def cancel_job(self, job_id):
        return (job_id == "q1", "cancelled")

    async def emergency_stop(self):
        return "stopped"

    def nodes(self):
        return [{"id": "vps", "status": "online"}]

    def computer_status(self):
        return {"armed": False, "arm_remaining_seconds": 0, "active_task": None, "screenshots": []}

    def artifact_path(self, _artifact_id):
        return None


async def fake_submit(*_args, **_kwargs):
    return True, "queued", FakeJob()


class WebApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()
        cls.server = WebConsoleServer(
            ("127.0.0.1", 0), WebConsoleHandler,
            control=FakeControl(), loop=cls.loop, token=TOKEN,
        )
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.loop_thread.join(timeout=2); cls.server_thread.join(timeout=2)

    def request(self, method, path, body=None, authorized=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = f"Bearer {TOKEN}"
        data = json.dumps(body).encode() if body is not None else None
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read() or b"{}")
        connection.close()
        return response.status, payload

    def test_health_and_unauthorized_rejection(self):
        status, body = self.request("GET", "/api/health", authorized=False)
        self.assertEqual(status, 200); self.assertTrue(body["ok"])
        status, _ = self.request("GET", "/api/jobs", authorized=False)
        self.assertEqual(status, 401)

    def test_sessions_jobs_event_replay_and_diff(self):
        self.assertEqual(self.request("GET", "/api/sessions")[0], 200)
        status, jobs = self.request("GET", "/api/jobs")
        self.assertEqual(status, 200); self.assertEqual(jobs["jobs"][0]["id"], "q1")
        status, events = self.request("GET", "/api/jobs/q1/events?after=0")
        self.assertEqual(status, 200); self.assertEqual(events["events"][0]["sequence"], 1)
        status, replay = self.request("GET", "/api/jobs/q1/events?after=1")
        self.assertEqual(status, 200); self.assertEqual(replay["events"], [])
        self.assertEqual(self.request("GET", "/api/jobs/q1/diff")[0], 200)

    def test_realtime_connect_event_and_reconnect_cursor(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", "/api/events/stream?job_id=q1&after=0", headers={"Authorization": f"Bearer {TOKEN}"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        lines = [response.readline().decode("utf-8") for _ in range(4)]
        self.assertTrue(any("event-1" in line for line in lines))
        connection.close()
        status, replay = self.request("GET", "/api/jobs/q1/events?after=1")
        self.assertEqual(status, 200); self.assertEqual(replay["events"], [])

    def test_task_cancel_and_authorized_mutations(self):
        with patch("web_console.submit_codex_job", fake_submit):
            status, task = self.request("POST", "/api/tasks", {"prompt": "hello", "session_id": "web-a"})
        self.assertEqual(status, 202); self.assertEqual(task["job_id"], "q2")
        self.assertEqual(self.request("POST", "/api/jobs/q1/cancel", {})[0], 200)
        self.assertEqual(self.request("POST", "/api/jobs/q1/cancel", {}, authorized=False)[0], 401)

    def test_apply_discard_require_scoped_approval(self):
        status, result = self.request("POST", "/api/jobs/q1/apply", {})
        self.assertEqual(status, 202); self.assertEqual(result["approval"]["status"], "pending")
        status, result = self.request("POST", "/api/approvals/approval-1/approve", {})
        self.assertEqual(status, 200); self.assertEqual(result["status"], "accepted")
        status, result = self.request("POST", "/api/approvals/approval-1/reject", {})
        self.assertEqual(status, 200); self.assertEqual(result["status"], "rejected")

    def test_nodes_computer_and_emergency_stop(self):
        self.assertEqual(self.request("GET", "/api/nodes")[0], 200)
        self.assertEqual(self.request("GET", "/api/computer/status")[0], 200)
        self.assertEqual(self.request("POST", "/api/computer/stop", {})[0], 200)


if __name__ == "__main__":
    unittest.main()
