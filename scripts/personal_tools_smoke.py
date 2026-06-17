#!/usr/bin/env python3
"""personal_tools_smoke.py — P3.1+P3.2 local notes/reminders + delivery + audit.

Run: .venv/bin/python scripts/personal_tools_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake-token")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", "12345")
os.environ.setdefault("LARK_APP_ID", "cli_fake")
os.environ.setdefault("LARK_APP_SECRET", "fake")
os.environ.setdefault("CODEX_WORKSPACE_ROOT", "/tmp/codex-pt-ws")
os.environ.setdefault("CODEX_TASK_ROOT", "/tmp/codex-pt-task")
os.environ.setdefault("CODEX_BIN", "codex")
os.environ.setdefault("USER_TIMEZONE", "UTC")

import personal_tools  # noqa: F401 — register tools
from channel import InboundMessage
from config import Settings, load_settings
from handlers.commands import run_command
from handlers.tools.audit import audit_log_path
from handlers.tools.confirm import clear_all_pending, get_pending_for_context
from handlers.tools.registry import DangerLevel
from handlers.tools.runner import _invoke_tool, execute_confirmed
from personal_tools.registry import PERSONAL_TOOL_REGISTRY, execute_personal_tool
from personal_tools.reminder_parse import parse_reminder_text
from personal_tools.store import PersonalToolsStore, db_path
from scripts.harness_common import CheckResult, print_results


@dataclass
class FakeOutbound:
    replies: list[str] = field(default_factory=list)
    supports_inline_buttons: bool = False

    async def reply(self, msg, text):
        self.replies.append(text)
        return None

    async def send_new(self, msg, text):
        self.replies.append(text)
        return None

    async def edit_progress(self, msg, placeholder_id, text):
        return False

    async def reply_with_buttons(self, msg, text, buttons):
        self.replies.append(text)
        return None


class FakeRunner:
    settings: Settings | None = None


def _msg(text: str = "", operator_id: str = "12345") -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        operator_id=operator_id,
        chat_id="chat-1",
        message_id="m-1",
        text=text,
    )


def _settings(tmp: Path) -> Settings:
    base = load_settings()
    return replace(base, codex_memory_root=tmp, telegram_allowed_user_id=12345, user_timezone="UTC")


# ---- registry / store basics -----------------------------------------------

def _test_registry() -> CheckResult:
    name = "registry: 45+ tools, correct danger levels"
    try:
        expected = {
            "notes.add",
            "notes.search",
            "notes.list_recent",
            "notes.delete",
            "reminders.create",
            "reminders.list",
            "reminders.cancel",
            "reminders.due",
            "gmail.status",
            "gmail.recent",
            "gmail.search",
            "gmail.read",
            "email.send",
            "google.status",
            "google.auth",
            "google.revoke",
            "calendar.status",
            "calendar.today",
            "calendar.tomorrow",
            "calendar.week",
            "calendar.search",
            "calendar.freebusy",
            "calendar.create",
            "contacts.search",
            "briefing.status",
            "briefing.today",
            "briefing.tomorrow",
            "briefing.enable",
            "briefing.disable",
            "briefing.probe",
            "github.status",
            "github.issues",
            "github.issue",
            "github.prs",
            "github.pr",
            "github.ci",
            "github.create_issue",
            "github.comment",
            "planner.list",
            "planner.today",
            "planner.dev",
            "planner.health",
            "planner.triage",
            "planner.schedule",
            # P3.9 Project Profiles
            "projects.list",
            "projects.add",
            "projects.use",
            "projects.show",
            "projects.remove",
            "project.status",
            "project.health",
            "project.roadmap",
            "project.next",
            "project.release_checklist",
            "project.brief",
            # P3.10 Setup Wizard
            "setup.status",
            "setup.check",
            "setup.project",
            "setup.gmail",
            "setup.google",
            "setup.github",
            # P3.11 Project IO
            "project.export",
            "project.export_all",
            "project.import",
            "project.template",
        }
        ok_names = expected == set(PERSONAL_TOOL_REGISTRY)
        add_level = PERSONAL_TOOL_REGISTRY["notes.add"].danger
        create_level = PERSONAL_TOOL_REGISTRY["reminders.create"].danger
        delete_level = PERSONAL_TOOL_REGISTRY["notes.delete"].danger
        cancel_level = PERSONAL_TOOL_REGISTRY["reminders.cancel"].danger
        gmail_status_level = PERSONAL_TOOL_REGISTRY["gmail.status"].danger
        email_send_level = PERSONAL_TOOL_REGISTRY["email.send"].danger
        google_auth_level = PERSONAL_TOOL_REGISTRY["google.auth"].danger
        calendar_create_level = PERSONAL_TOOL_REGISTRY["calendar.create"].danger
        contacts_search_level = PERSONAL_TOOL_REGISTRY["contacts.search"].danger
        briefing_status_level = PERSONAL_TOOL_REGISTRY["briefing.status"].danger
        briefing_enable_level = PERSONAL_TOOL_REGISTRY["briefing.enable"].danger
        briefing_disable_level = PERSONAL_TOOL_REGISTRY["briefing.disable"].danger
        github_status_level = PERSONAL_TOOL_REGISTRY["github.status"].danger
        github_create_issue_level = PERSONAL_TOOL_REGISTRY["github.create_issue"].danger
        github_comment_level = PERSONAL_TOOL_REGISTRY["github.comment"].danger
        planner_list_level = PERSONAL_TOOL_REGISTRY["planner.list"].danger
        planner_today_level = PERSONAL_TOOL_REGISTRY["planner.today"].danger
        planner_dev_level = PERSONAL_TOOL_REGISTRY["planner.dev"].danger
        planner_health_level = PERSONAL_TOOL_REGISTRY["planner.health"].danger
        planner_triage_level = PERSONAL_TOOL_REGISTRY["planner.triage"].danger
        planner_schedule_level = PERSONAL_TOOL_REGISTRY["planner.schedule"].danger
        # P3.9 Project tools
        projects_list_level = PERSONAL_TOOL_REGISTRY["projects.list"].danger
        projects_add_level = PERSONAL_TOOL_REGISTRY["projects.add"].danger
        projects_use_level = PERSONAL_TOOL_REGISTRY["projects.use"].danger
        projects_show_level = PERSONAL_TOOL_REGISTRY["projects.show"].danger
        projects_remove_level = PERSONAL_TOOL_REGISTRY["projects.remove"].danger
        project_status_level = PERSONAL_TOOL_REGISTRY["project.status"].danger
        project_health_level = PERSONAL_TOOL_REGISTRY["project.health"].danger
        project_roadmap_level = PERSONAL_TOOL_REGISTRY["project.roadmap"].danger
        project_next_level = PERSONAL_TOOL_REGISTRY["project.next"].danger
        project_release_checklist_level = PERSONAL_TOOL_REGISTRY["project.release_checklist"].danger
        project_brief_level = PERSONAL_TOOL_REGISTRY["project.brief"].danger
        # P3.10 Setup tools
        setup_status_level = PERSONAL_TOOL_REGISTRY["setup.status"].danger
        setup_check_level = PERSONAL_TOOL_REGISTRY["setup.check"].danger
        setup_project_level = PERSONAL_TOOL_REGISTRY["setup.project"].danger
        setup_gmail_level = PERSONAL_TOOL_REGISTRY["setup.gmail"].danger
        setup_google_level = PERSONAL_TOOL_REGISTRY["setup.google"].danger
        setup_github_level = PERSONAL_TOOL_REGISTRY["setup.github"].danger
        # P3.11 Project IO tools
        project_export_level = PERSONAL_TOOL_REGISTRY["project.export"].danger
        project_export_all_level = PERSONAL_TOOL_REGISTRY["project.export_all"].danger
        project_import_level = PERSONAL_TOOL_REGISTRY["project.import"].danger
        project_template_level = PERSONAL_TOOL_REGISTRY["project.template"].danger
        ok_levels = (
            add_level == DangerLevel.WRITE_SAFE
            and create_level == DangerLevel.WRITE_SAFE
            and delete_level == DangerLevel.DESTRUCTIVE
            and cancel_level == DangerLevel.WRITE
            and gmail_status_level == DangerLevel.READ
            and email_send_level == DangerLevel.WRITE
            and google_auth_level == DangerLevel.WRITE
            and calendar_create_level == DangerLevel.WRITE
            and contacts_search_level == DangerLevel.READ
            and briefing_status_level == DangerLevel.READ
            and briefing_enable_level == DangerLevel.WRITE_SAFE
            and briefing_disable_level == DangerLevel.WRITE
            and github_status_level == DangerLevel.READ
            and github_create_issue_level == DangerLevel.WRITE_SAFE
            and github_comment_level == DangerLevel.WRITE
            and planner_list_level == DangerLevel.READ
            and planner_today_level == DangerLevel.READ
            and planner_dev_level == DangerLevel.READ
            and planner_health_level == DangerLevel.READ
            and planner_triage_level == DangerLevel.READ
            and planner_schedule_level == DangerLevel.READ
            # P3.9 Project tools levels
            and projects_list_level == DangerLevel.READ
            and projects_add_level == DangerLevel.WRITE_SAFE
            and projects_use_level == DangerLevel.WRITE_SAFE
            and projects_show_level == DangerLevel.READ
            and projects_remove_level == DangerLevel.DESTRUCTIVE
            and project_status_level == DangerLevel.READ
            and project_health_level == DangerLevel.READ
            and project_roadmap_level == DangerLevel.READ
            and project_next_level == DangerLevel.READ
            and project_release_checklist_level == DangerLevel.READ
            and project_brief_level == DangerLevel.READ
            # P3.10 Setup tools levels
            and setup_status_level == DangerLevel.READ
            and setup_check_level == DangerLevel.READ
            and setup_project_level == DangerLevel.READ
            and setup_gmail_level == DangerLevel.READ
            and setup_google_level == DangerLevel.READ
            and setup_github_level == DangerLevel.READ
            # P3.11 Project IO tools levels
            and project_export_level == DangerLevel.READ
            and project_export_all_level == DangerLevel.READ
            and project_import_level == DangerLevel.WRITE_SAFE
            and project_template_level == DangerLevel.READ
        )
        return CheckResult(name, ok_names and ok_levels, f"names={ok_names} levels={ok_levels}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


def _test_db_path() -> CheckResult:
    name = "store: db at codex_memory_root/personal_tools.db"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            PersonalToolsStore(settings)
            path = db_path(settings)
            ok = path.name == "personal_tools.db" and path.is_file()
            return CheckResult(name, ok, str(path))
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- notes CRUD --------------------------------------------------------------

async def _test_notes_crud() -> CheckResult:
    name = "notes: add/search/list/delete"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            op = "12345"
            add = await execute_personal_tool(settings, "notes.add", "buy milk", operator_id=op)
            if "笔记已保存" not in add:
                return CheckResult(name, False, f"add={add}")
            store = PersonalToolsStore(settings)
            rows = store.list_recent_notes(op)
            if len(rows) != 1:
                return CheckResult(name, False, f"count={len(rows)}")
            note_id = rows[0].id
            search = await execute_personal_tool(settings, "notes.search", "milk", operator_id=op)
            if "#" + str(note_id) not in search:
                return CheckResult(name, False, f"search={search}")
            listed = await execute_personal_tool(settings, "notes.list_recent", "", operator_id=op)
            if "buy milk" not in listed:
                return CheckResult(name, False, f"list={listed}")
            deleted = await execute_personal_tool(settings, "notes.delete", str(note_id), operator_id=op)
            if "已删除" not in deleted:
                return CheckResult(name, False, f"delete={deleted}")
            after = store.list_recent_notes(op)
            ok = len(after) == 0
            return CheckResult(name, ok, f"remaining={len(after)}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_notes_empty() -> CheckResult:
    name = "notes: empty list and empty search"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            op = "12345"
            empty_list = await execute_personal_tool(settings, "notes.list_recent", "", operator_id=op)
            if "还没有笔记" not in empty_list:
                return CheckResult(name, False, f"list={empty_list}")
            empty_search = await execute_personal_tool(settings, "notes.search", "nothing", operator_id=op)
            if "没有匹配" not in empty_search:
                return CheckResult(name, False, f"search={empty_search}")
            bad_usage = await execute_personal_tool(settings, "notes.add", "", operator_id=op)
            ok = "用法" in bad_usage
            return CheckResult(name, ok, "empty cases ok")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- reminders flow -----------------------------------------------------------

def _test_reminder_parse() -> CheckResult:
    name = "reminder parse: in 10m / in 2h / tomorrow / ISO / bad"
    try:
        cases = [
            ("in 10m buy milk", "buy milk"),
            ("standup in 2h", "standup"),
            ("meeting tomorrow 09:00", "meeting"),
            ("2026-06-16T09:00:00 report", "report"),
        ]
        for raw, expected_body in cases:
            parsed = parse_reminder_text(raw, tz_name="UTC")
            if parsed is None:
                return CheckResult(name, False, f"failed parse {raw!r}")
            body, due = parsed
            if body != expected_body:
                return CheckResult(name, False, f"body={body!r} want {expected_body!r}")
            if due.tzinfo is None:
                return CheckResult(name, False, "due missing tz")
        bad = parse_reminder_text("no time here", tz_name="UTC")
        return CheckResult(name, bad is None, f"bad={bad}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_reminders_flow() -> CheckResult:
    name = "reminders: create/list/cancel/due"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            op = "12345"
            store = PersonalToolsStore(settings)
            past_due = datetime.now(timezone.utc) - timedelta(minutes=5)
            row = store.create_reminder(op, "past task", past_due)
            created = await execute_personal_tool(
                settings, "reminders.create", "in 2h future task", operator_id=op,
            )
            if "提醒已创建" not in created:
                return CheckResult(name, False, f"create={created}")
            listed = await execute_personal_tool(settings, "reminders.list", "", operator_id=op)
            if "future task" not in listed or f"#{row.id}" not in listed:
                return CheckResult(name, False, f"list={listed}")
            due = await execute_personal_tool(settings, "reminders.due", "", operator_id=op)
            if "past task" not in due:
                return CheckResult(name, False, f"due={due}")
            cancelled = await execute_personal_tool(
                settings, "reminders.cancel", str(row.id), operator_id=op,
            )
            if "已取消" not in cancelled:
                return CheckResult(name, False, f"cancel={cancelled}")
            return CheckResult(name, True, "flow ok")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_reminders_empty() -> CheckResult:
    name = "reminders: empty list and bad parse usage"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            op = "12345"
            empty = await execute_personal_tool(settings, "reminders.list", "", operator_id=op)
            if "没有提醒" not in empty:
                return CheckResult(name, False, f"list={empty}")
            bad_parse = await execute_personal_tool(settings, "reminders.create", "no time here", operator_id=op)
            if "用法" not in bad_parse:
                return CheckResult(name, False, f"bad_parse={bad_parse}")
            bad_cancel = await execute_personal_tool(settings, "reminders.cancel", "abc", operator_id=op)
            ok = "无效" in bad_cancel
            return CheckResult(name, ok, "empty/bad ok")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- operator isolation ------------------------------------------------------

async def _test_operator_isolation() -> CheckResult:
    name = "isolation: operator A cannot see operator B notes/reminders"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            await execute_personal_tool(settings, "notes.add", "secret-a", operator_id="aaa")
            await execute_personal_tool(settings, "reminders.create", "in 1h task-a", operator_id="aaa")
            b_notes = await execute_personal_tool(settings, "notes.list_recent", "", operator_id="bbb")
            if "还没有笔记" not in b_notes:
                return CheckResult(name, False, f"B sees A notes: {b_notes[:60]}")
            b_reminders = await execute_personal_tool(settings, "reminders.list", "", operator_id="bbb")
            if "没有提醒" not in b_reminders:
                return CheckResult(name, False, f"B sees A reminders: {b_reminders[:60]}")
            store = PersonalToolsStore(settings)
            a_notes = store.list_recent_notes("aaa")
            ok = len(a_notes) == 1 and a_notes[0].text == "secret-a"
            return CheckResult(name, ok, f"a_notes={len(a_notes)} b_notes=0")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- WRITE_SAFE audit --------------------------------------------------------

async def _test_write_safe_audit_notes() -> CheckResult:
    name = "audit: notes.add (WRITE_SAFE) writes executed to tools.log"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            port = FakeOutbound()
            await _invoke_tool(_msg(), port, settings, "notes.add", "test note")
            path = audit_log_path(settings)
            if not path.is_file():
                return CheckResult(name, False, "no audit file")
            recs = [json.loads(l) for l in path.read_text().strip().splitlines() if l.strip()]
            actions = [r["action"] for r in recs if r.get("tool_name") == "notes.add"]
            ok = "executed" in actions and "requested" not in actions
            return CheckResult(name, ok, f"actions={actions}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_write_safe_audit_reminders() -> CheckResult:
    name = "audit: reminders.create (WRITE_SAFE) writes executed to tools.log"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            port = FakeOutbound()
            await _invoke_tool(_msg(), port, settings, "reminders.create", "in 5m test")
            path = audit_log_path(settings)
            if not path.is_file():
                return CheckResult(name, False, "no audit file")
            recs = [json.loads(l) for l in path.read_text().strip().splitlines() if l.strip()]
            actions = [r["action"] for r in recs if r.get("tool_name") == "reminders.create"]
            ok = "executed" in actions and "requested" not in actions
            return CheckResult(name, ok, f"actions={actions}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_write_confirm_still_works() -> CheckResult:
    name = "audit: notes.delete (DESTRUCTIVE) still requests confirmation"
    try:
        clear_all_pending()
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            port = FakeOutbound()
            await _invoke_tool(_msg(), port, settings, "notes.delete", "1")
            pending = get_pending_for_context("12345", "chat-1", "telegram")
            has_confirm_text = any("危险操作需确认" in r for r in port.replies)
            ok = pending is not None and has_confirm_text
            return CheckResult(name, ok, f"pending={pending is not None} confirm_text={has_confirm_text}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- redaction ----------------------------------------------------------------

async def _test_redaction_in_audit() -> CheckResult:
    name = "audit: secrets redacted in tools.log"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            port = FakeOutbound()
            secret = "sk-live-abcdefghijklmnopqrstuvwxyz1234567890"
            await _invoke_tool(_msg(), port, settings, "notes.add", secret)
            path = audit_log_path(settings)
            text = path.read_text(encoding="utf-8")
            ok = "sk-live" not in text and "[REDACTED" in text
            return CheckResult(name, ok, f"len={len(text)}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- command surface ---------------------------------------------------------

async def _test_cmd_note() -> CheckResult:
    name = "cmd: /note (WRITE_SAFE) executes immediately"
    try:
        clear_all_pending()
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            port = FakeOutbound()
            runner = FakeRunner()
            runner.settings = settings
            ok = await run_command("note", _msg("/note hello"), port, runner, settings, "hello")
            if not ok:
                return CheckResult(name, False, "not handled")
            has_saved = any("笔记已保存" in r for r in port.replies)
            has_confirm = any("危险操作需确认" in r for r in port.replies)
            ok = has_saved and not has_confirm
            return CheckResult(name, ok, f"saved={has_saved} confirm={has_confirm}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_cmd_notes() -> CheckResult:
    name = "cmd: /notes list and search"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            port = FakeOutbound()
            runner = FakeRunner()
            runner.settings = settings
            await run_command("note", _msg("/note alpha"), port, runner, settings, "alpha")
            await run_command("note", _msg("/note beta"), port, runner, settings, "beta")
            port.replies.clear()
            ok_list = await run_command("notes", _msg("/notes"), port, runner, settings, "")
            if not ok_list or not any("alpha" in r and "beta" in r for r in port.replies):
                return CheckResult(name, False, f"list={port.replies}")
            port.replies.clear()
            ok_search = await run_command("notes", _msg("/notes alpha"), port, runner, settings, "alpha")
            ok = ok_search and any("alpha" in r for r in port.replies)
            return CheckResult(name, ok, f"search={port.replies[-1][:60] if port.replies else ''}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_cmd_remind() -> CheckResult:
    name = "cmd: /remind (WRITE_SAFE) executes immediately"
    try:
        clear_all_pending()
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            port = FakeOutbound()
            runner = FakeRunner()
            runner.settings = settings
            ok = await run_command("remind", _msg("/remind in 5m ping"), port, runner, settings, "in 5m ping")
            if not ok:
                return CheckResult(name, False, "not handled")
            has_created = any("提醒已创建" in r for r in port.replies)
            has_confirm = any("危险操作需确认" in r for r in port.replies)
            ok = has_created and not has_confirm
            return CheckResult(name, ok, f"created={has_created} confirm={has_confirm}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


async def _test_cmd_reminders() -> CheckResult:
    name = "cmd: /reminders list"
    try:
        clear_all_pending()
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            port = FakeOutbound()
            runner = FakeRunner()
            runner.settings = settings
            await run_command("remind", _msg("/remind in 1h standup"), port, runner, settings, "in 1h standup")
            port.replies.clear()
            ok_list = await run_command("reminders", _msg("/reminders"), port, runner, settings, "")
            ok = ok_list and any("standup" in r for r in port.replies)
            return CheckResult(name, ok, f"list={port.replies[-1][:60] if port.replies else ''}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- P3.2: delivery schema migration ----------------------------------------

def _test_migration_adds_delivery_columns() -> CheckResult:
    name = "P3.2 migration: delivery columns added to existing DB"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            path = db_path(settings)
            # Simulate a pre-P3.2 DB with only the original schema.
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operator_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL
                );
            """)
            conn.commit()
            conn.close()
            # Opening the store should trigger migration.
            store = PersonalToolsStore(settings)
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(reminders)").fetchall()}
            conn.close()
            expected = {"channel", "chat_id", "delivered_at", "delivery_status", "delivery_error", "retry_count"}
            missing = expected - cols
            ok = not missing
            return CheckResult(name, ok, f"cols={sorted(cols)} missing={sorted(missing)}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- P3.2: channel/chat_id storage ------------------------------------------

async def _test_remind_stores_channel_chat_id() -> CheckResult:
    name = "P3.2 /remind: stores channel and chat_id"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            op = "12345"
            result = await execute_personal_tool(
                settings, "reminders.create", "in 10m test",
                operator_id=op, channel="telegram", chat_id="chat-42",
            )
            if "提醒已创建" not in result:
                return CheckResult(name, False, f"create={result}")
            store = PersonalToolsStore(settings)
            rows = store.list_reminders(op)
            if not rows:
                return CheckResult(name, False, "no rows")
            r = rows[0]
            ok = r.channel == "telegram" and r.chat_id == "chat-42"
            return CheckResult(name, ok, f"channel={r.channel} chat_id={r.chat_id}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- P3.2: due query only returns deliverable --------------------------------

def _test_due_query_deliverable_only() -> CheckResult:
    name = "P3.2: list_due_deliverable skips no-channel and cancelled"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            store = PersonalToolsStore(settings)
            op = "u1"
            past = datetime.now(timezone.utc) - timedelta(minutes=2)
            # No channel/chat_id — should be skipped by deliverable query.
            store.create_reminder(op, "no-channel", past)
            # With channel/chat_id — should appear.
            store.create_reminder(op, "has-channel", past, channel="telegram", chat_id="c1")
            # Cancelled — should be skipped.
            row_c = store.create_reminder(op, "cancelled", past, channel="telegram", chat_id="c1")
            store.cancel_reminder(op, row_c.id)
            deliverable = store.list_due_deliverable_reminders()
            texts = [r.text for r in deliverable]
            ok = "has-channel" in texts and "no-channel" not in texts and "cancelled" not in texts
            return CheckResult(name, ok, f"deliverable={texts}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- P3.2: mark done prevents redelivery ------------------------------------

def _test_mark_done_prevents_redelivery() -> CheckResult:
    name = "P3.2: mark_reminder_done prevents redelivery"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            store = PersonalToolsStore(settings)
            op = "u1"
            past = datetime.now(timezone.utc) - timedelta(minutes=2)
            row = store.create_reminder(op, "deliver-me", past, channel="telegram", chat_id="c1")
            before = store.list_due_deliverable_reminders()
            if len(before) != 1:
                return CheckResult(name, False, f"before={len(before)}")
            now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            store.mark_reminder_done(row.id, now_iso)
            after = store.list_due_deliverable_reminders()
            ok = len(after) == 0
            return CheckResult(name, ok, f"after_mark_done={len(after)}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- P3.2: failed delivery records error + increments retry ------------------

def _test_mark_failed_records_error() -> CheckResult:
    name = "P3.2: mark_reminder_failed records error and retry_count"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            store = PersonalToolsStore(settings)
            op = "u1"
            past = datetime.now(timezone.utc) - timedelta(minutes=2)
            row = store.create_reminder(op, "will-fail", past, channel="telegram", chat_id="c1")
            store.mark_reminder_failed(row.id, "ConnectionError", 1)
            # Should still be deliverable (failed < 3 retries).
            deliverable = store.list_due_deliverable_reminders()
            found = [r for r in deliverable if r.id == row.id]
            if not found:
                return CheckResult(name, False, "not in deliverable after fail")
            r = found[0]
            ok = r.delivery_status == "failed" and r.delivery_error == "ConnectionError" and r.retry_count == 1
            return CheckResult(name, ok, f"status={r.delivery_status} err={r.delivery_error} retries={r.retry_count}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- P3.2: max retries stops delivery ---------------------------------------

def _test_max_retries_stops_delivery() -> CheckResult:
    name = "P3.2: retry_count >= 3 excluded from deliverable"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            store = PersonalToolsStore(settings)
            op = "u1"
            past = datetime.now(timezone.utc) - timedelta(minutes=2)
            row = store.create_reminder(op, "exhausted", past, channel="telegram", chat_id="c1")
            store.mark_reminder_failed(row.id, "err", 3)
            deliverable = store.list_due_deliverable_reminders()
            found = [r for r in deliverable if r.id == row.id]
            ok = len(found) == 0
            return CheckResult(name, ok, f"found_after_max_retries={len(found)}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- P3.2: scheduler dry-run ------------------------------------------------

def _test_scheduler_dry_run() -> CheckResult:
    name = "P3.2: scheduler_tick --dry-run delivers without network"
    try:
        with tempfile.TemporaryDirectory() as td:
            settings = _settings(Path(td))
            store = PersonalToolsStore(settings)
            op = "u1"
            past = datetime.now(timezone.utc) - timedelta(minutes=2)
            store.create_reminder(op, "dry-run task", past, channel="telegram", chat_id="c1")
            from scripts.scheduler_tick import run_tick
            with mock.patch("scripts.scheduler_tick.load_settings", return_value=settings):
                delivered, failed = run_tick(dry_run=True)
            # DB should be unchanged after dry-run.
            deliverable = store.list_due_deliverable_reminders()
            still_pending = [r for r in deliverable if r.text == "dry-run task"]
            ok = delivered == 1 and failed == 0 and len(still_pending) == 1
            return CheckResult(name, ok, f"delivered={delivered} failed={failed} still_pending={len(still_pending)}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- P3.2: CommandSpec drift check ------------------------------------------

def _test_commandspec_no_drift() -> CheckResult:
    name = "P3.2 drift: /note and /remind CommandSpec says 立即执行 not 需确认"
    try:
        from handlers.commands import COMMAND_TABLE
        note_spec = COMMAND_TABLE.get("note")
        remind_spec = COMMAND_TABLE.get("remind")
        if note_spec is None or remind_spec is None:
            return CheckResult(name, False, "missing note or remind in COMMAND_TABLE")
        note_summary = note_spec.summary
        remind_summary = remind_spec.summary
        has_stale = "需确认" in note_summary or "需确认" in remind_summary
        has_correct = "立即执行" in note_summary and "立即执行" in remind_summary
        ok = not has_stale and has_correct
        return CheckResult(name, ok, f"note={note_summary!r} remind={remind_summary!r}")
    except Exception as exc:
        return CheckResult(name, False, str(exc))


# ---- main --------------------------------------------------------------------

async def main() -> int:
    results = [
        _test_registry(),
        _test_db_path(),
        _test_reminder_parse(),
        await _test_notes_crud(),
        await _test_notes_empty(),
        await _test_reminders_flow(),
        await _test_reminders_empty(),
        await _test_operator_isolation(),
        await _test_write_safe_audit_notes(),
        await _test_write_safe_audit_reminders(),
        await _test_write_confirm_still_works(),
        await _test_redaction_in_audit(),
        await _test_cmd_note(),
        await _test_cmd_notes(),
        await _test_cmd_remind(),
        await _test_cmd_reminders(),
        # P3.2 delivery tests
        _test_migration_adds_delivery_columns(),
        await _test_remind_stores_channel_chat_id(),
        _test_due_query_deliverable_only(),
        _test_mark_done_prevents_redelivery(),
        _test_mark_failed_records_error(),
        _test_max_retries_stops_delivery(),
        _test_scheduler_dry_run(),
        _test_commandspec_no_drift(),
    ]
    print_results(results)
    ok = all(r.ok for r in results)
    print("personal tools smoke ok" if ok else "personal tools smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
