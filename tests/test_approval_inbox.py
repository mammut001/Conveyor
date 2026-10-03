"""tests/test_approval_inbox.py — Unit and HTTP integration tests for Unified Approval Inbox."""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import approval_inbox
import handlers.tools.executors  # register builtin tools
import routines
from handlers.tools.audit import read_audit_tail
from handlers.tools.confirm import (
    clear_all_pending,
    create_pending,
    get_pending,
    replace_pending_arg,
)
from handlers.tools.registry import register_tool, ToolSpec, DangerLevel
from personal_tools.registry import register_personal_tools
from handlers.job_queue import JobQueue
from web_console import WebConsoleHandler, WebConsoleServer
from web_control import WebControl

TOKEN = "test-approval-inbox-token-12345"


class TestDraftParsingAndValidation(unittest.TestCase):
    """Test parse_draft and build_arg round-trips and validation rules."""

    def setUp(self):
        register_personal_tools()

    def test_email_send_roundtrip(self):
        arg = "alice@example.com | Project Update | Hello Alice, here is the report."
        draft = approval_inbox.parse_draft("email.send", arg)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["to"], "alice@example.com")
        self.assertEqual(draft["subject"], "Project Update")
        self.assertEqual(draft["body"], "Hello Alice, here is the report.")
        built = approval_inbox.build_arg("email.send", draft)
        self.assertEqual(built, arg)

    def test_email_send_body_with_pipes(self):
        arg = "alice@example.com, bob@example.com | Table | Col 1 | Col 2 | Col 3"
        draft = approval_inbox.parse_draft("email.send", arg)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["to"], "alice@example.com, bob@example.com")
        self.assertEqual(draft["subject"], "Table")
        self.assertEqual(draft["body"], "Col 1 | Col 2 | Col 3")
        built = approval_inbox.build_arg("email.send", draft)
        self.assertEqual(built, arg)

    def test_github_comment_roundtrip(self):
        arg = "42 | LGTM! Ship it."
        draft = approval_inbox.parse_draft("github.comment", arg)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["number"], "42")
        self.assertEqual(draft["body"], "LGTM! Ship it.")
        built = approval_inbox.build_arg("github.comment", draft)
        self.assertEqual(built, arg)

    def test_github_create_issue_roundtrip(self):
        arg = "Bug: memory leak | Found on worker node #2"
        draft = approval_inbox.parse_draft("github.create_issue", arg)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["title"], "Bug: memory leak")
        self.assertEqual(draft["body"], "Found on worker node #2")
        built = approval_inbox.build_arg("github.create_issue", draft)
        self.assertEqual(built, arg)

    def test_notes_add_roundtrip(self):
        arg = "Remember to buy coffee beans"
        draft = approval_inbox.parse_draft("notes.add", arg)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["text"], "Remember to buy coffee beans")
        built = approval_inbox.build_arg("notes.add", draft)
        self.assertEqual(built, arg)

    def test_memory_remember_roundtrip(self):
        arg = "The operator prefers dark mode theme."
        draft = approval_inbox.parse_draft("memory.remember", arg)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["text"], "The operator prefers dark mode theme.")
        built = approval_inbox.build_arg("memory.remember", draft)
        self.assertEqual(built, arg)

    def test_routine_create_roundtrip(self):
        arg = "0 9 * * 1-5 | Check system status | Daily Check"
        draft = approval_inbox.parse_draft("routine.create", arg)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["cron"], "0 9 * * 1-5")
        self.assertEqual(draft["prompt"], "Check system status")
        self.assertEqual(draft["name"], "Daily Check")
        built = approval_inbox.build_arg("routine.create", draft)
        self.assertEqual(built, arg)

    def test_skill_create_roundtrip(self):
        arg = "Deploy Web App | Steps to build and deploy | Step 1: build | Step 2: run"
        draft = approval_inbox.parse_draft("skill.create", arg)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["name"], "Deploy Web App")
        self.assertEqual(draft["description"], "Steps to build and deploy")
        self.assertEqual(draft["body"], "Step 1: build | Step 2: run")
        built = approval_inbox.build_arg("skill.create", draft)
        self.assertEqual(built, arg)

    def test_non_editable_tool_returns_none_and_raises(self):
        self.assertIsNone(approval_inbox.parse_draft("service_restart", "conveyor"))
        with self.assertRaises(ValueError) as ctx:
            approval_inbox.build_arg("service_restart", {"service": "conveyor"})
        self.assertIn("does not support editable drafts", str(ctx.exception))

    def test_pipes_in_single_line_fields_rejected(self):
        # email to & subject
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("email.send", {"to": "a@b.com | c@d.com", "subject": "hi", "body": "text"})
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("email.send", {"to": "a@b.com", "subject": "hi|there", "body": "text"})
        # github number & title
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("github.comment", {"number": "4|2", "body": "text"})
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("github.create_issue", {"title": "title|pipe", "body": "text"})
        # routine cron & name
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("routine.create", {"cron": "0 9 * * * | evil", "prompt": "check", "name": "ok"})
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("routine.create", {"cron": "0 9 * * *", "prompt": "check", "name": "name|pipe"})

    def test_newlines_in_single_line_fields_rejected(self):
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("email.send", {"to": "a@b.com\n", "subject": "hi", "body": "text"})
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("github.create_issue", {"title": "title\nnewline", "body": "text"})
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("routine.create", {"cron": "0 9 * * *\n", "prompt": "check", "name": "ok"})

    def test_prompt_cannot_contain_pipe_but_can_contain_newlines(self):
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("routine.create", {"cron": "0 9 * * *", "prompt": "check | evil", "name": "ok"})
        # prompt with newline is allowed
        built = approval_inbox.build_arg("routine.create", {"cron": "0 9 * * *", "prompt": "line 1\nline 2", "name": "ok"})
        self.assertIn("line 1\nline 2", built)

    def test_number_digits_only(self):
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("github.comment", {"number": "abc", "body": "text"})
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("github.comment", {"number": "12a", "body": "text"})

    def test_to_comma_separated_emails(self):
        # valid single and multiple
        self.assertTrue(approval_inbox.build_arg("email.send", {"to": "user@example.com", "subject": "hi", "body": "text"}))
        self.assertTrue(approval_inbox.build_arg("email.send", {"to": "a@b.com, c@d.org", "subject": "hi", "body": "text"}))
        # invalid
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("email.send", {"to": "not-an-email", "subject": "hi", "body": "text"})
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("email.send", {"to": "valid@b.com, not-an-email", "subject": "hi", "body": "text"})
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("email.send", {"to": "valid@b.com, ", "subject": "hi", "body": "text"})

    def test_length_caps(self):
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("email.send", {"to": "a" * 321 + "@b.com", "subject": "hi", "body": "text"})
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("email.send", {"to": "a@b.com", "subject": "x" * 201, "body": "text"})
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("github.create_issue", {"title": "x" * 201, "body": "text"})
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("routine.create", {"cron": "0 9 * * *", "prompt": "check", "name": "x" * 81})
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("github.comment", {"number": "1", "body": "x" * 20001})
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("notes.add", {"text": "x" * 4001})
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("routine.create", {"cron": "0 9 * * *", "prompt": "x" * 4001, "name": "ok"})

    def test_secrets_rejected_with_exact_message(self):
        secret_values = [
            "ghp_123456789012345678901234567890123456",
            "AKIAIOSFODNN7EXAMPLE",
            "ya29.a0AfH6SMDfake-token-value-here-1234567890",
        ]
        for secret in secret_values:
            with self.assertRaises(ValueError) as ctx:
                approval_inbox.build_arg("notes.add", {"text": f"Note with token: {secret}"})
            self.assertEqual(str(ctx.exception), "drafts cannot contain secrets or tokens")

    def test_memory_screening(self):
        # Multiple sentences rejected
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("memory.remember", {"text": "First sentence. Second sentence."})
        # Credentials rejected by screen_write_arg
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("memory.remember", {"text": "我的密码是 SecretPassword123"})

    def test_routine_cron_validation(self):
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("routine.create", {"cron": "every day at 9am", "prompt": "check", "name": "ok"})


class TestEditPendingAndAudit(unittest.TestCase):
    """Test edit_pending, concurrency atomicity, DB updates, and audit logging."""

    def setUp(self):
        clear_all_pending()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(
            codex_memory_root=Path(self.temp_dir.name),
            codex_task_root=Path(self.temp_dir.name),
            routines_enabled=True,
        )
        routines.init_db(self.settings)

    def tearDown(self):
        clear_all_pending()
        self.temp_dir.cleanup()

    def test_edit_pending_success_and_preserves_metadata(self):
        action = create_pending(
            tool_name="notes.add",
            arg="original note",
            operator_id="op1",
            chat_id="chat1",
            channel="web",
        )
        token = action.token
        created_at = action.created_at
        ttl = action.ttl_seconds

        time.sleep(0.01)
        updated = approval_inbox.edit_pending(self.settings, token, {"text": "updated note"})

        self.assertEqual(updated.token, token)
        self.assertEqual(updated.tool_name, "notes.add")
        self.assertEqual(updated.arg, "updated note")
        self.assertEqual(updated.operator_id, "op1")
        self.assertEqual(updated.chat_id, "chat1")
        self.assertEqual(updated.channel, "web")
        self.assertEqual(updated.created_at, created_at)
        self.assertEqual(updated.ttl_seconds, ttl)
        self.assertEqual(get_pending(token).arg, "updated note")

        # Check audit event
        tail = read_audit_tail(self.settings, n=5)
        self.assertTrue(len(tail) >= 1)
        audit_rec = tail[-1]
        self.assertEqual(audit_rec["action"], "edited")
        self.assertEqual(audit_rec["tool_name"], "notes.add")
        self.assertEqual(audit_rec["arg"], "updated note")
        self.assertEqual(audit_rec["old_arg"], "original note")

    def test_edit_pending_persisted_routine_approval_updated(self):
        routine = routines.create_routine(
            self.settings,
            name="Test Routine",
            schedule="0 9 * * *",
            prompt="check notes",
            deliver=["web"],
        )
        routine_id = routine["id"]
        action = create_pending(
            tool_name="notes.add",
            arg="initial note",
            operator_id="web-console",
            chat_id=f"routine-{routine_id}",
            channel="web",
        )
        routines.persist_routine_approval(self.settings, action.token, routine_id)

        # Confirm DB has initial arg
        conn = routines._connect(self.settings)
        try:
            row = conn.execute("SELECT arg FROM routine_approvals WHERE token = ?", (action.token,)).fetchone()
            self.assertEqual(row["arg"], "initial note")
        finally:
            conn.close()

        # Edit draft
        approval_inbox.edit_pending(self.settings, action.token, {"text": "edited note"})

        # Confirm DB row was updated with edited arg
        conn = routines._connect(self.settings)
        try:
            row = conn.execute("SELECT arg FROM routine_approvals WHERE token = ?", (action.token,)).fetchone()
            self.assertEqual(row["arg"], "edited note")
        finally:
            conn.close()

    def test_edit_pending_errors(self):
        # Non-pending raises KeyError
        with self.assertRaises(KeyError):
            approval_inbox.edit_pending(self.settings, "nonexistent", {"text": "hello"})

        # Non-web channel raises KeyError
        tg_action = create_pending("notes.add", "arg", "op", "chat", "telegram")
        with self.assertRaises(KeyError):
            approval_inbox.edit_pending(self.settings, tg_action.token, {"text": "hello"})

        # Non-editable tool raises PermissionError
        srv_action = create_pending("service_restart", "conveyor", "op", "chat", "web")
        with self.assertRaises(PermissionError):
            approval_inbox.edit_pending(self.settings, srv_action.token, {"service": "new"})

        # Redacted item raises PermissionError
        secret_arg = "Note with secret: ghp_123456789012345678901234567890123456"
        redacted_action = create_pending("notes.add", secret_arg, "op", "chat", "web")
        with self.assertRaises(PermissionError) as ctx:
            approval_inbox.edit_pending(self.settings, redacted_action.token, {"text": "safe note"})
        self.assertIn("redacted content", str(ctx.exception))


class TestListItems(unittest.TestCase):
    """Test approval_inbox.list_items sources, redaction, and sorting."""

    def setUp(self):
        clear_all_pending()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(
            codex_memory_root=Path(self.temp_dir.name),
            codex_task_root=Path(self.temp_dir.name),
            routines_enabled=True,
        )
        routines.init_db(self.settings)
        self.queue = JobQueue()
        self.queue.configure(self.settings, runner=None, recover=False)
        self.control = WebControl(self.settings, runner=None, queue=self.queue)

    def tearDown(self):
        clear_all_pending()
        self.temp_dir.cleanup()

    def test_list_items_aggregates_sources(self):
        # 1. Chat tool action
        chat_action = create_pending("notes.add", "chat note", "web-console", "web-chat-1", "web")

        # 2. Routine tool action
        r = routines.create_routine(self.settings, name="Backup Check", schedule="0 10 * * *", prompt="run check", deliver=["web"])
        routine_action = create_pending("notes.add", "routine note", "web-console", f"routine-{r['id']}", "web")
        routines.persist_routine_approval(self.settings, routine_action.token, r["id"])

        # 3. Webhook routine tool action
        r_wh = routines.create_routine(self.settings, name="Webhook Routine", schedule="0 10 * * *", prompt="wh", deliver=["web"])
        wh_action = create_pending("notes.add", "webhook note", "web-console", f"routine-{r_wh['id']}", "web")
        routines.persist_routine_approval(self.settings, wh_action.token, r_wh["id"])
        # record run with trigger='webhook'
        routines.record_run(
            self.settings,
            routine_id=r_wh["id"],
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            status="approval_pending",
            output="waiting",
            approval_id=wh_action.token,
            approval_status="pending",
            trigger="webhook",
        )

        # 4. Job approval
        conn = self.control._connect()
        try:
            with conn:
                conn.execute(
                    """INSERT INTO web_approvals (id, job_id, action, status, created_at, expires_at)
                       VALUES ('job-appr-1', 'job-123', 'apply', 'pending', '2026-01-01T00:00:00Z', ?)""",
                    (time.time() + 300,),
                )
        finally:
            conn.close()
        job_appr_id = 'job-appr-1'

        items = approval_inbox.list_items(self.settings, self.control)
        self.assertEqual(len(items), 4)

        sources = {it["id"]: it["source"] for it in items}
        self.assertEqual(sources[chat_action.token], "chat")
        self.assertEqual(sources[routine_action.token], "routine")
        self.assertEqual(sources[wh_action.token], "webhook")
        self.assertEqual(sources[job_appr_id], "job")

        # Verify routine details
        r_item = next(it for it in items if it["id"] == routine_action.token)
        self.assertEqual(r_item["routine_id"], r["id"])
        self.assertEqual(r_item["routine_name"], "Backup Check")

        # Verify job item structure
        j_item = next(it for it in items if it["id"] == job_appr_id)
        self.assertEqual(j_item["kind"], "job")
        self.assertEqual(j_item["action"], "apply")
        self.assertEqual(j_item["job_id"], "job-123")
        self.assertFalse(j_item["editable"])
        self.assertIsNone(j_item["draft"])

    def test_redacted_items_not_editable(self):
        secret = "ghp_123456789012345678901234567890123456"
        action = create_pending("notes.add", f"Note with {secret}", "op", "chat", "web")
        items = approval_inbox.list_items(self.settings, self.control)
        item = next(it for it in items if it["id"] == action.token)
        self.assertTrue(item.get("redacted"))
        self.assertFalse(item["editable"])
        self.assertIsNone(item["draft"])
        self.assertNotIn(secret, item["arg"])


class TestApprovalInboxHttpApi(unittest.TestCase):
    """Test HTTP API with real WebConsoleServer on 127.0.0.1:0."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        td = Path(cls.temp_dir.name)
        cls.settings = SimpleNamespace(
            codex_memory_root=td,
            codex_task_root=td,
            user_timezone="America/Toronto",
            routines_enabled=True,
            webhooks_enabled=True,
            approval_inbox_enabled=True,
            conveyor_web_enabled=True,
            conveyor_web_host="127.0.0.1",
            conveyor_web_port=0,
            conveyor_web_token=TOKEN,
            chat_mode="auto",
            chat_tools_enabled=True,
            conveyor_event_retention_per_job=2000,
            telegram_bot_token=None,
            lark_app_id=None,
            lark_app_secret=None,
            conveyor_desktop_node_enabled=False,
        )
        cls.queue = JobQueue()
        cls.queue.configure(cls.settings, runner=None, recover=False)
        cls.control = WebControl(cls.settings, runner=None, queue=cls.queue)

        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()

        cls.server = WebConsoleServer(
            ("127.0.0.1", 0), WebConsoleHandler,
            control=cls.control, loop=cls.loop, token=TOKEN,
        )
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.loop_thread.join(timeout=2)
        cls.server_thread.join(timeout=2)
        cls.temp_dir.cleanup()

    def setUp(self):
        clear_all_pending()
        self.settings.approval_inbox_enabled = True
        conn = self.control._connect()
        try:
            with conn:
                conn.execute("DELETE FROM web_approvals")
        finally:
            conn.close()

    def raw_request(
        self,
        method: str,
        path: str,
        body: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        authorized: bool = True,
    ):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        req_headers = dict(headers or {})
        if authorized:
            req_headers["Authorization"] = f"Bearer {TOKEN}"
        if isinstance(body, str):
            body = body.encode("utf-8")
        conn.request(method, path, body=body, headers=req_headers)
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = raw.decode("utf-8", errors="replace")
        return res.status, parsed

    def test_flag_off_returns_409(self):
        self.settings.approval_inbox_enabled = False
        status, body = self.raw_request("GET", "/api/approval-inbox")
        self.assertEqual(status, 409)
        self.assertIn("approval inbox is disabled", body.get("error", ""))

        status, body = self.raw_request("POST", "/api/approval-inbox/any-id/approve", body="{}")
        self.assertEqual(status, 409)

        status, body = self.raw_request("POST", "/api/approval-inbox/any-id/reject", body="{}")
        self.assertEqual(status, 409)

    def test_unauthenticated_returns_401(self):
        status, _ = self.raw_request("GET", "/api/approval-inbox", authorized=False)
        self.assertEqual(status, 401)

    def test_get_approval_inbox_items_and_counts(self):
        create_pending("notes.add", "note 1", "op", "chat1", "web")
        status, data = self.raw_request("GET", "/api/approval-inbox")
        self.assertEqual(status, 200)
        self.assertIn("items", data)
        self.assertIn("counts", data)
        self.assertEqual(data["counts"]["total"], 1)
        self.assertEqual(data["counts"]["chat"], 1)

    def test_approve_as_is_executes_original_arg(self):
        action = create_pending("notes.add", "buy milk", "web-console", "web-chat-1", "web")
        token = action.token

        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as mock_tool:
            mock_tool.return_value = "Note saved successfully"
            status, res = self.raw_request("POST", f"/api/approval-inbox/{token}/approve", body="{}")

        self.assertEqual(status, 200)
        self.assertEqual(res["status"], "accepted")
        mock_tool.assert_called_once()
        self.assertEqual(mock_tool.call_args[0][2], "buy milk")
        self.assertIsNone(get_pending(token))

    def test_approve_redacted_item_as_shown_executes_real_arg(self):
        # The list shows a redacted arg; approving with that as expected_arg must
        # still execute the real (unredacted) arg, and a draft stays forbidden.
        secret = "ghp_" + "1" * 36
        action = create_pending("notes.add", f"Note with {secret}", "web-console", "web-chat-1", "web")
        status, listing = self.raw_request("GET", "/api/approval-inbox")
        shown = next(it for it in listing["items"] if it["id"] == action.token)
        self.assertNotIn(secret, shown["arg"])
        status, res = self.raw_request(
            "POST", f"/api/approval-inbox/{action.token}/approve",
            body=json.dumps({"draft": {"text": "x"}, "expected_arg": shown["arg"]}),
        )
        self.assertEqual(status, 400)
        self.assertIsNotNone(get_pending(action.token))
        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as mock_tool:
            mock_tool.return_value = "ok"
            status, res = self.raw_request(
                "POST", f"/api/approval-inbox/{action.token}/approve",
                body=json.dumps({"expected_arg": shown["arg"]}),
            )
        self.assertEqual(status, 200)
        mock_tool.assert_called_once()
        self.assertEqual(mock_tool.call_args[0][2], f"Note with {secret}")

    def test_edit_then_approve_executes_edited_arg_exactly_once(self):
        action = create_pending("notes.add", "original note", "web-console", "web-chat-1", "web")
        token = action.token

        payload = json.dumps({
            "draft": {"text": "edited note content"},
            "expected_arg": "original note",
        })

        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as mock_tool:
            mock_tool.return_value = "Note edited and saved"
            status, res = self.raw_request("POST", f"/api/approval-inbox/{token}/approve", body=payload)

        self.assertEqual(status, 200)
        self.assertEqual(res["status"], "accepted")
        mock_tool.assert_called_once()
        self.assertEqual(mock_tool.call_args[0][2], "edited note content")
        self.assertIsNone(get_pending(token))

    def test_expected_arg_mismatch_returns_409_and_does_not_execute(self):
        action = create_pending("notes.add", "current arg", "web-console", "web-chat-1", "web")
        token = action.token

        payload = json.dumps({
            "draft": {"text": "new note"},
            "expected_arg": "stale arg that operator saw",
        })

        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as mock_tool:
            status, res = self.raw_request("POST", f"/api/approval-inbox/{token}/approve", body=payload)

        self.assertEqual(status, 409)
        self.assertIn("draft changed, reload", res.get("error", ""))
        mock_tool.assert_not_called()
        self.assertIsNotNone(get_pending(token))
        self.assertEqual(get_pending(token).arg, "current arg")

    def test_invalid_draft_returns_400_and_does_not_execute(self):
        action = create_pending("routine.create", "0 9 * * * | prompt | name", "web-console", "web-chat-1", "web")
        token = action.token

        payload = json.dumps({
            "draft": {"cron": "invalid cron syntax", "prompt": "prompt", "name": "name"},
            "expected_arg": "0 9 * * * | prompt | name",
        })

        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as mock_tool:
            status, res = self.raw_request("POST", f"/api/approval-inbox/{token}/approve", body=payload)

        self.assertEqual(status, 400)
        self.assertIn("Invalid cron", res.get("error", ""))
        mock_tool.assert_not_called()
        self.assertIsNotNone(get_pending(token))
        self.assertEqual(get_pending(token).arg, "0 9 * * * | prompt | name")

    def test_draft_on_non_editable_tool_returns_400(self):
        action = create_pending("service_restart", "conveyor", "web-console", "web-chat-1", "web")
        token = action.token

        payload = json.dumps({
            "draft": {"service": "malicious"},
            "expected_arg": "conveyor",
        })

        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as mock_tool:
            status, res = self.raw_request("POST", f"/api/approval-inbox/{token}/approve", body=payload)

        self.assertEqual(status, 400)
        self.assertIn("does not support editable drafts", res.get("error", ""))
        mock_tool.assert_not_called()
        self.assertIsNotNone(get_pending(token))

    def test_draft_on_job_item_returns_400(self):
        conn = self.control._connect()
        try:
            with conn:
                conn.execute(
                    """INSERT INTO web_approvals (id, job_id, action, status, created_at, expires_at)
                       VALUES ('job-appr-test', 'job-123', 'apply', 'pending', '2026-01-01T00:00:00Z', ?)""",
                    (time.time() + 300,),
                )
        finally:
            conn.close()
        appr_id = 'job-appr-test'

        payload = json.dumps({
            "draft": {"patch": "something"},
        })
        status, res = self.raw_request("POST", f"/api/approval-inbox/{appr_id}/approve", body=payload)
        self.assertEqual(status, 400)
        self.assertIn("job approvals do not support drafts", res.get("error", ""))

    def test_reject_tool_action(self):
        action = create_pending("notes.add", "to be rejected", "web-console", "web-chat-1", "web")
        token = action.token

        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as mock_tool:
            status, res = self.raw_request("POST", f"/api/approval-inbox/{token}/reject", body="{}")

        self.assertEqual(status, 200)
        self.assertEqual(res["status"], "rejected")
        mock_tool.assert_not_called()
        self.assertIsNone(get_pending(token))

    def test_reject_job_approval(self):
        conn = self.control._connect()
        try:
            with conn:
                conn.execute(
                    """INSERT INTO web_approvals (id, job_id, action, status, created_at, expires_at)
                       VALUES ('job-appr-rej', 'job-123', 'discard', 'pending', '2026-01-01T00:00:00Z', ?)""",
                    (time.time() + 300,),
                )
        finally:
            conn.close()
        appr_id = 'job-appr-rej'

        status, res = self.raw_request("POST", f"/api/approval-inbox/{appr_id}/reject", body="{}")
        self.assertEqual(status, 200)
        self.assertEqual(res["status"], "rejected")

    def test_double_approve_returns_404(self):
        action = create_pending("notes.add", "double test", "web-console", "web-chat-1", "web")
        token = action.token

        with patch("handlers.tools.runner.run_tool", new_callable=AsyncMock) as mock_tool:
            mock_tool.return_value = "executed ok"
            status1, _ = self.raw_request("POST", f"/api/approval-inbox/{token}/approve", body="{}")
            self.assertEqual(status1, 200)

            status2, res2 = self.raw_request("POST", f"/api/approval-inbox/{token}/approve", body="{}")
            self.assertEqual(status2, 404)
            self.assertEqual(res2.get("error"), "not found")

    def test_cannot_change_tool_name_or_channel_via_draft(self):
        action = create_pending("notes.add", "plain note", "web-console", "web-chat-1", "web")
        token = action.token

        # Payload trying to inject unexpected keys
        payload = json.dumps({
            "draft": {"text": "new note", "tool_name": "service_restart", "channel": "telegram"},
        })
        status, res = self.raw_request("POST", f"/api/approval-inbox/{token}/approve", body=payload)
        self.assertEqual(status, 400)
        self.assertIn("Unknown field", res.get("error", ""))

        # Action is untouched
        current = get_pending(token)
        self.assertEqual(current.tool_name, "notes.add")
        self.assertEqual(current.channel, "web")
