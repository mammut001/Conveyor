"""PR6 Agent screens: identity, operator scoping, XSS and lease cancellation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import agent_context as ac
import agent_mailbox as mailbox
import web_agent_screens as screens
from agents import AgentStore, WEB_OPERATOR, chat_id_for
from config import Settings


def test_settings(root):
    return Settings(
        telegram_bot_token="test-token", telegram_allowed_user_id=1,
        codex_workspace_root=root/"workspace", codex_bin="codex",
        codex_task_root=root/"tasks", codex_model=None,
        codex_timeout_seconds=30, codex_retry_429_delays_seconds=(),
        telegram_progress_seconds=1, codex_memory_root=root,
        user_timezone="UTC", agents_enabled=True)


class AgentScreenTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = test_settings(Path(tmp.name))
        flags = patch.dict(os.environ, {
            "CONVEYOR_NAMED_AGENTS": "1", "CONVEYOR_AGENT_CONTEXT": "1",
            "CONVEYOR_AGENT_MAILBOX": "1", "CONVEYOR_AGENT_DELEGATION": "1"})
        flags.start()
        self.addCleanup(flags.stop)
        self.store = AgentStore(self.settings)
        self.a = self.store.create({"name": "Atlas", "tools_allowlist": ["notes.search"]})
        self.b = self.store.create({"name": "Brava", "tools_allowlist": ["notes.search"]})

    def ctx(self, item):
        return ac.resolve(self.settings, "web", chat_id_for(item["id"]), WEB_OPERATOR)

    def send(self, to, *, kind="message", body="<img src=x onerror=alert(1)>"):
        with ac.bind(self.ctx(self.a)):
            return mailbox.send(
                self.settings, sender=self.ctx(self.a), to_agent=to["id"],
                kind=kind, body=body,
                idempotency_key="screen-" + str(time.time_ns()),
                deadline=time.time()+120)

    def test_flag_off_hides_all_new_routes(self):
        with patch.dict(os.environ, {"CONVEYOR_AGENT_CONTEXT": "0"}):
            self.assertFalse(screens.features(self.settings)["enabled"])
            with self.assertRaises(ValueError):
                screens.list_items(self.settings, self.a["id"], "inbox")
        with patch.dict(os.environ, {"CONVEYOR_AGENT_MAILBOX": "0"}):
            self.assertFalse(screens.features(self.settings)["inbox"])
            with self.assertRaises(ValueError):
                screens.list_items(self.settings, self.a["id"], "inbox")
        with patch.dict(os.environ, {"CONVEYOR_AGENT_DELEGATION": "0"}):
            self.assertFalse(screens.features(self.settings)["tasks"])

    def test_inbox_is_scoped_untrusted_and_never_contains_other_operators(self):
        row = self.send(self.b)
        records = screens.list_items(self.settings, self.b["id"], "inbox")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], row["id"])
        self.assertEqual(records[0]["body"], "<img src=x onerror=alert(1)>")
        self.assertTrue(records[0]["untrusted"])
        self.assertFalse(records[0]["approval_authority"])
        self.assertEqual(screens.list_items(self.settings, self.a["id"], "inbox"), [])
        with self.assertRaises(PermissionError):
            screens.list_items(self.settings, self.b["id"], "inbox", operator="ou_other")
        with ac.bind(self.ctx(self.a)), self.assertRaises(PermissionError):
            screens.list_items(self.settings, self.b["id"], "inbox")
        # An injected row that belongs to a different operator is invisible.
        with sqlite3.connect(str(self.store.path)) as db:
            db.execute("UPDATE agent_mailbox SET operator='ou_other' WHERE id=?", (row["id"],))
        self.assertEqual(screens.list_items(self.settings, self.b["id"], "inbox"), [])

    def test_archived_and_unknown_agent_ids_denied(self):
        self.store.archive(self.b["id"])
        for name in (self.b["id"], "missingagent", "../default", ""):
            with self.subTest(name=name), self.assertRaises((ValueError, PermissionError, ac.AgentContextDenied)):
                screens.list_items(self.settings, name, "inbox")

    def test_tasks_scoped_with_statuses_and_targeted_cancel(self):
        first = self.send(self.b, kind="task", body=json.dumps({
            "v":1, "task":"hello", "tools":[]}))
        second = self.send(self.b, kind="task", body=json.dumps({
            "v":1, "task":"bye", "tools":[]}))
        tasks = screens.list_items(self.settings, self.b["id"], "tasks")
        self.assertEqual({x["id"] for x in tasks}, {first["id"], second["id"]})
        self.assertEqual({x["status"] for x in tasks}, {"queued"})
        self.assertNotIn("body", tasks[0])
        self.assertTrue(screens.cancel_task(self.settings, self.b["id"], first["id"]))
        import agent_security
        count_before = len([row for row in agent_security.audit_for_agent(
            self.settings, self.b["id"], WEB_OPERATOR)
            if row["reason"] == "task_cancelled"])
        self.assertFalse(screens.cancel_task(self.settings, self.b["id"], first["id"]))
        count_after = len([row for row in agent_security.audit_for_agent(
            self.settings, self.b["id"], WEB_OPERATOR)
            if row["reason"] == "task_cancelled"])
        self.assertEqual(count_before, 1)
        self.assertEqual(count_after, count_before)
        status = {x["id"]:x["status"] for x in screens.list_items(
            self.settings, self.a["id"], "tasks")}
        self.assertEqual(status[first["id"]], "dead")
        self.assertEqual(status[second["id"]], "queued")
        with self.assertRaises(PermissionError):
            screens.cancel_task(self.settings, self.b["id"], second["id"], operator="other")
        with self.assertRaises(LookupError):
            screens.cancel_task(self.settings, self.store.get("default")["id"], "f"*32)
        self.assertEqual({x["id"] for x in screens.list_items(
            self.settings, self.b["id"], "tasks")}, {first["id"], second["id"]})

    def test_same_operator_cannot_cancel_unrelated_agent_task(self):
        c = self.store.create({"name":"Charlie","tools_allowlist":[]})
        first = self.send(self.b, kind="task", body='{"v":1,"task":"hello","tools":[]}')
        with self.assertRaises(LookupError):
            screens.cancel_task(self.settings, c["id"], first["id"])
        self.assertEqual(screens.list_items(self.settings, c["id"], "tasks"), [])


if __name__ == "__main__":
    unittest.main()
