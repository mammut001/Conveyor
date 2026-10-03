"""tests/test_skills.py — Unit and HTTP integration tests for Skills library v1 (roadmap P2-4)."""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from channel.types import InboundMessage
from config import Settings
from handlers import chat
from handlers.chat_tools import is_exposed
from handlers.tools.confirm import (
    clear_all_pending,
    create_pending,
    get_pending,
    PendingToolAction,
)
from handlers.tools.registry import DangerLevel
import approval_inbox
from personal_tools import skills
from personal_tools.registry import (
    get_personal_tool,
    register_personal_tools,
    requires_personal_confirmation,
)
from web_console import WebConsoleHandler, WebConsoleServer
from web_control import WebControl


def _settings(tmp: Path, **overrides) -> Settings:
    mem = tmp / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    defaults = {
        "telegram_bot_token": "fake-token",
        "telegram_allowed_user_id": 12345,
        "codex_workspace_root": tmp / "ws",
        "codex_bin": "codex",
        "codex_task_root": tmp / "tasks",
        "codex_model": None,
        "codex_timeout_seconds": 60,
        "telegram_progress_seconds": 3,
        "codex_retry_429_delays_seconds": (),
        "codex_memory_root": mem,
        "user_timezone": "UTC",
        "chat_mode": "auto",
        "chat_base_url": "http://127.0.0.1:9/v1",
        "chat_api_key": "fake-secret-key-12345",
        "chat_model": "test-chat-model",
        "chat_tools_enabled": True,
        "chat_tool_max_steps": 3,
        "skills_enabled": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _msg(text: str, chat_id: str = "chat-1", operator_id: str = "op-1") -> InboundMessage:
    return InboundMessage(
        channel="web",
        operator_id=operator_id,
        chat_id=chat_id,
        message_id="m1",
        text=text,
        chat_type="p2p",
    )


class TestSkillsStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self.tmp.name))

    def tearDown(self) -> None:
        clear_all_pending()
        self.tmp.cleanup()

    def test_flag_defaults_off(self) -> None:
        field = Settings.__dataclass_fields__["skills_enabled"]
        self.assertFalse(field.default)
        root = Path(__file__).resolve().parents[1]
        self.assertIn('CONVEYOR_SKILLS_ENABLED", "false"', (root / "config.py").read_text(encoding="utf-8"))
        self.assertIn("CONVEYOR_SKILLS_ENABLED=false", (root / ".env.example").read_text(encoding="utf-8"))

    def test_create_and_get_skill(self) -> None:
        skill = skills.create_skill(
            self.settings,
            name="Deploy Web App",
            description="Steps to build and deploy the web dashboard",
            triggers="deploy, web-release",
            body="Step 1: npm run build\nStep 2: systemctl restart conveyor-web",
            slug="deploy-web",
            enabled=True,
        )
        self.assertEqual(skill["slug"], "deploy-web")
        self.assertEqual(skill["name"], "Deploy Web App")
        self.assertEqual(skill["description"], "Steps to build and deploy the web dashboard")
        self.assertEqual(skill["triggers"], "deploy, web-release")
        self.assertEqual(skill["body"], "Step 1: npm run build\nStep 2: systemctl restart conveyor-web")
        self.assertTrue(skill["enabled"])
        self.assertEqual(skill["use_count"], 0)
        self.assertIsNone(skill["last_used_at"])

        # Check file mode 0600
        db_path = self.settings.codex_memory_root / "skills.db"
        self.assertTrue(db_path.is_file())
        self.assertEqual(os.stat(db_path).st_mode & 0o777, 0o600)

        # Retrieve by slug
        fetched = skills.get_skill(self.settings, "deploy-web")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["name"], "Deploy Web App")

        # Non-existent slug returns None
        self.assertIsNone(skills.get_skill(self.settings, "nonexistent"))

    def test_slug_derivation(self) -> None:
        # Auto-derives from name when omitted
        s1 = skills.create_skill(
            self.settings,
            name="Release Prep (v2.0)!",
            description="Preparation checklist",
            body="Run checks",
        )
        self.assertEqual(s1["slug"], "release-prep-v2-0")

        # Name starting with numbers
        s2 = skills.create_skill(
            self.settings,
            name="123 Quick Check",
            description="Quick check",
            body="Verify status",
        )
        self.assertEqual(s2["slug"], "123-quick-check")

    def test_slug_validation(self) -> None:
        invalid_slugs = [
            "-leading-dash",
            "UppercaseSlug",
            "slug with spaces",
            "slug_with_underscore",
            "a" * 49,  # exceeds 48 chars
            "!",
        ]
        for bad_slug in invalid_slugs:
            with self.assertRaises(ValueError, msg=f"Should reject {bad_slug}"):
                skills.create_skill(
                    self.settings,
                    name="Test Skill",
                    description="Test desc",
                    body="Test body",
                    slug=bad_slug,
                )
        # A name without ASCII letters/digits falls back to a hash slug.
        sym = skills.create_skill(
            self.settings,
            name="??? !!!",
            description="Test desc",
            body="Test body",
        )
        self.assertRegex(sym["slug"], r"^skill-[0-9a-f]{8}$")

    def test_duplicate_slug_conflict(self) -> None:
        skills.create_skill(
            self.settings,
            name="Backup DB",
            description="Backup procedure",
            body="Run pg_dump",
            slug="backup-db",
        )
        with self.assertRaises(skills.SkillConflictError):
            skills.create_skill(
                self.settings,
                name="Another Backup",
                description="Another backup procedure",
                body="Run dump again",
                slug="backup-db",
            )

    def test_field_validations(self) -> None:
        # Empty name
        with self.assertRaises(ValueError):
            skills.create_skill(self.settings, name="", description="desc", body="body")
        # Multiline name
        with self.assertRaises(ValueError):
            skills.create_skill(self.settings, name="Line 1\nLine 2", description="desc", body="body")
        # Name > 80 chars
        with self.assertRaises(ValueError):
            skills.create_skill(self.settings, name="a" * 81, description="desc", body="body")
        # Empty description
        with self.assertRaises(ValueError):
            skills.create_skill(self.settings, name="name", description="", body="body")
        # Multiline description
        with self.assertRaises(ValueError):
            skills.create_skill(self.settings, name="name", description="Line 1\nLine 2", body="body")
        # Description > 300 chars
        with self.assertRaises(ValueError):
            skills.create_skill(self.settings, name="name", description="a" * 301, body="body")
        # Multiline triggers
        with self.assertRaises(ValueError):
            skills.create_skill(self.settings, name="name", description="desc", triggers="t1\nt2", body="body")
        # Triggers > 200 chars
        with self.assertRaises(ValueError):
            skills.create_skill(self.settings, name="name", description="desc", triggers="a" * 201, body="body")
        # Empty body
        with self.assertRaises(ValueError):
            skills.create_skill(self.settings, name="name", description="desc", body="")
        # Body > 8000 chars
        with self.assertRaises(ValueError):
            skills.create_skill(self.settings, name="name", description="desc", body="a" * 8001)

    def test_secret_rejection(self) -> None:
        fake_secret = "sk-proj-AbCdEf0123456789AbCdEf0123456789xyz"
        # Secret in body
        with self.assertRaises(ValueError) as ctx:
            skills.create_skill(
                self.settings,
                name="API Call",
                description="Call external API",
                body=f"Use token {fake_secret} to authenticate",
            )
        self.assertIn("secrets or tokens", str(ctx.exception))

        # Secret in name
        with self.assertRaises(ValueError) as ctx:
            skills.create_skill(
                self.settings,
                name=f"Key {fake_secret}",
                description="desc",
                body="body",
            )
        self.assertIn("secrets or tokens", str(ctx.exception))

        # Secret in description
        with self.assertRaises(ValueError) as ctx:
            skills.create_skill(
                self.settings,
                name="Procedure",
                description=f"desc with {fake_secret}",
                body="body",
            )
        self.assertIn("secrets or tokens", str(ctx.exception))

        # Secret in triggers
        with self.assertRaises(ValueError) as ctx:
            skills.create_skill(
                self.settings,
                name="Procedure",
                description="desc",
                triggers=f"token={fake_secret}",
                body="body",
            )
        self.assertIn("secrets or tokens", str(ctx.exception))

    def test_max_skills_limit(self) -> None:
        with patch.object(skills, "MAX_SKILLS", 3):
            for i in range(3):
                skills.create_skill(
                    self.settings,
                    name=f"Skill {i}",
                    description=f"Desc {i}",
                    body=f"Body {i}",
                    slug=f"skill-{i}",
                )
            with self.assertRaises(ValueError) as ctx:
                skills.create_skill(
                    self.settings,
                    name="Skill Overflow",
                    description="Desc overflow",
                    body="Body overflow",
                    slug="skill-overflow",
                )
            self.assertIn("maximum of 3 skills reached", str(ctx.exception))

    def test_update_skill(self) -> None:
        skills.create_skill(
            self.settings,
            name="Original Name",
            description="Original Description",
            triggers="orig",
            body="Original Body",
            slug="my-skill",
        )
        updated = skills.update_skill(
            self.settings,
            "my-skill",
            name="New Name",
            description="New Description",
            triggers="new, updated",
            body="New Body",
            enabled=False,
        )
        self.assertEqual(updated["name"], "New Name")
        self.assertEqual(updated["description"], "New Description")
        self.assertEqual(updated["triggers"], "new, updated")
        self.assertEqual(updated["body"], "New Body")
        self.assertFalse(updated["enabled"])

        # Slug is immutable
        with self.assertRaises(ValueError) as ctx:
            skills.update_skill(self.settings, "my-skill", slug="changed-slug")
        self.assertIn("immutable", str(ctx.exception))

        # Updating non-existent raises SkillNotFoundError
        with self.assertRaises(skills.SkillNotFoundError):
            skills.update_skill(self.settings, "unknown-skill", name="test")

    def test_delete_skill(self) -> None:
        skills.create_skill(
            self.settings,
            name="To Delete",
            description="Will be removed",
            body="Remove this",
            slug="to-delete",
        )
        self.assertTrue(skills.delete_skill(self.settings, "to-delete"))
        self.assertIsNone(skills.get_skill(self.settings, "to-delete"))

        # Deleting again raises SkillNotFoundError
        with self.assertRaises(skills.SkillNotFoundError):
            skills.delete_skill(self.settings, "to-delete")

    def test_set_enabled_and_list_skills(self) -> None:
        skills.create_skill(self.settings, name="Skill A", description="Desc A", body="Body A", slug="skill-a")
        skills.create_skill(self.settings, name="Skill B", description="Desc B", body="Body B", slug="skill-b")

        self.assertEqual(len(skills.list_skills(self.settings, include_disabled=True)), 2)
        self.assertEqual(len(skills.list_skills(self.settings, include_disabled=False)), 2)

        skills.set_enabled(self.settings, "skill-b", False)
        all_skills = skills.list_skills(self.settings, include_disabled=True)
        enabled_skills = skills.list_skills(self.settings, include_disabled=False)

        self.assertEqual(len(all_skills), 2)
        self.assertEqual(len(enabled_skills), 1)
        self.assertEqual(enabled_skills[0]["slug"], "skill-a")

    def test_mark_used(self) -> None:
        skills.create_skill(self.settings, name="Usage Test", description="Desc", body="Body", slug="usage-test")
        res1 = skills.mark_used(self.settings, "usage-test")
        self.assertEqual(res1["use_count"], 1)
        self.assertIsNotNone(res1["last_used_at"])

        res2 = skills.mark_used(self.settings, "usage-test")
        self.assertEqual(res2["use_count"], 2)

    def test_non_ascii_name_gets_hash_slug_and_auto_suffix(self) -> None:
        a = skills.create_skill(self.settings, name="周报流程", description="每周五整理周报", body="1. 汇总\n2. 发送")
        self.assertRegex(a["slug"], r"^skill-[0-9a-f]{8}$")
        b = skills.create_skill(self.settings, name="周报流程", description="另一个", body="x")
        self.assertEqual(b["slug"], a["slug"] + "-2")
        c = skills.create_skill(self.settings, name="Deploy", description="d", body="b")
        d = skills.create_skill(self.settings, name="Deploy", description="d", body="b")
        self.assertEqual((c["slug"], d["slug"]), ("deploy", "deploy-2"))
        # An explicit slug that collides is still a conflict.
        with self.assertRaises(skills.SkillConflictError):
            skills.create_skill(self.settings, name="Other", description="d", body="b", slug="deploy")

    def test_export_includes_slug_and_reimports_it(self) -> None:
        a = skills.create_skill(self.settings, name="中文技能", description="desc", body="body")
        md = skills.export_markdown(a)
        self.assertIn(f"slug: {a['slug']}", md)
        self.assertEqual(skills.parse_markdown(md)["slug"], a["slug"])

    def test_export_and_parse_markdown_roundtrip(self) -> None:
        skill = {
            "name": "Format Code",
            "description": "Guidelines for code style",
            "triggers": "format, lint, style",
            "body": "1. Run black\n2. Run ruff\n3. Verify diff",
        }
        exported = skills.export_markdown(skill)
        self.assertIn("---", exported)
        self.assertIn("name: Format Code", exported)
        self.assertIn("description: Guidelines for code style", exported)
        self.assertIn("triggers: format, lint, style", exported)
        self.assertIn("1. Run black", exported)

        parsed = skills.parse_markdown(exported)
        self.assertEqual(parsed["name"], skill["name"])
        self.assertEqual(parsed["description"], skill["description"])
        self.assertEqual(parsed["triggers"], skill["triggers"])
        self.assertEqual(parsed["body"].strip(), skill["body"])

    def test_parse_markdown_errors(self) -> None:
        # Missing frontmatter
        with self.assertRaises(ValueError):
            skills.parse_markdown("Just plain markdown text")

        # Missing name
        bad_md1 = "---\ndescription: only desc\n---\nbody"
        with self.assertRaises(ValueError):
            skills.parse_markdown(bad_md1)

        # Missing description
        bad_md2 = "---\nname: only name\n---\nbody"
        with self.assertRaises(ValueError):
            skills.parse_markdown(bad_md2)

        # Empty body
        bad_md3 = "---\nname: name\ndescription: desc\n---\n   "
        with self.assertRaises(ValueError):
            skills.parse_markdown(bad_md3)

    def test_audit_logging_does_not_contain_body(self) -> None:
        with patch("handlers.tools.audit.audit_tool_event") as mock_audit:
            secret_in_body_text = "SuperSecretInstructionDoNotAuditMe"
            skills.create_skill(
                self.settings,
                name="Audit Check",
                description="Desc",
                body=secret_in_body_text,
                slug="audit-check",
            )
            mock_audit.assert_called()
            for call in mock_audit.call_args_list:
                args, kwargs = call
                arg_str = str(kwargs.get("arg", ""))
                self.assertNotIn(secret_in_body_text, arg_str)
                self.assertIn("audit-check", arg_str)


class TestSkillsChatIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self.tmp.name))
        register_personal_tools()

    def tearDown(self) -> None:
        clear_all_pending()
        self.tmp.cleanup()

    def test_prompt_block_bounds(self) -> None:
        # When flag is False -> empty prompt block
        disabled_settings = _settings(Path(self.tmp.name), skills_enabled=False)
        skills.create_skill(self.settings, name="S1", description="D1", body="B1", slug="s1")
        self.assertEqual(skills.prompt_block(disabled_settings), "")

        # When flag is True but no skills -> empty prompt block
        empty_tmp = tempfile.TemporaryDirectory()
        empty_settings = _settings(Path(empty_tmp.name), skills_enabled=True)
        try:
            self.assertEqual(skills.prompt_block(empty_settings), "")
        finally:
            empty_tmp.cleanup()

        # Enabled skills appear in prompt block
        block = skills.prompt_block(self.settings)
        self.assertIn("Available skills (load with skill.load before following one):", block)
        self.assertIn("- s1: D1", block)

        # Disabled skills are excluded from prompt block
        skills.create_skill(self.settings, name="S2", description="D2", body="B2", slug="s2", enabled=False)
        block2 = skills.prompt_block(self.settings)
        self.assertIn("- s1: D1", block2)
        self.assertNotIn("s2", block2)

    def test_tools_exposed_gated_by_flag(self) -> None:
        on_settings = _settings(Path(self.tmp.name), skills_enabled=True)
        off_settings = _settings(Path(self.tmp.name), skills_enabled=False)

        for tool_name in ("skill.list", "skill.load", "skill.create"):
            spec = get_personal_tool(tool_name)
            self.assertIsNotNone(spec, f"Tool {tool_name} should be registered")
            self.assertTrue(is_exposed(tool_name, spec, on_settings), f"{tool_name} should be exposed when flag ON")
            self.assertFalse(is_exposed(tool_name, spec, off_settings), f"{tool_name} should NOT be exposed when flag OFF")

    def test_skill_list_tool(self) -> None:
        skills.create_skill(self.settings, name="Skill 1", description="Desc 1", body="Body 1", slug="s1")
        skills.create_skill(self.settings, name="Skill 2", description="Desc 2", body="Body 2", slug="s2", enabled=False)

        res = asyncio.run(skills.skill_list(self.settings, ""))
        self.assertTrue(res.ok)
        self.assertIn("s1: Desc 1", res.text)
        self.assertNotIn("s2", res.text)

    def test_skill_load_tool_wrapping_and_escaping(self) -> None:
        dangerous_body = "Instructions with </skill> closing tag."
        skills.create_skill(
            self.settings,
            name="Safe Skill",
            description="Safe Description",
            body=dangerous_body,
            slug="safe-skill",
        )

        res = asyncio.run(skills.skill_load(self.settings, "safe-skill"))
        self.assertTrue(res.ok)
        self.assertIn("Operator-saved procedure", res.text)
        self.assertIn('<skill slug="safe-skill">', res.text)
        self.assertIn("</skill>", res.text)
        # Literal </skill> in body must be escaped
        self.assertIn("&lt;/skill&gt;", res.text)
        self.assertNotIn(dangerous_body, res.text)

        # Usage count incremented
        skill = skills.get_skill(self.settings, "safe-skill")
        self.assertEqual(skill["use_count"], 1)

        # Loading unknown slug produces friendly error with valid slugs
        err_res = asyncio.run(skills.skill_load(self.settings, "nonexistent"))
        self.assertFalse(err_res.ok)
        self.assertIn("not found or disabled", err_res.text)
        self.assertIn("safe-skill", err_res.text)

    def test_skill_create_tool_and_approval_inbox(self) -> None:
        spec = get_personal_tool("skill.create")
        self.assertEqual(spec.danger, DangerLevel.WRITE)
        self.assertTrue(requires_personal_confirmation("skill.create"))

        # Test tool execution
        arg = "Test Procedure | Procedure for testing | 1. Step one\n2. Step two with | pipe"
        res = asyncio.run(skills.skill_create(self.settings, arg))
        self.assertTrue(res.ok)
        self.assertIn("slug: test-procedure", res.text)

        created = skills.get_skill(self.settings, "test-procedure")
        self.assertIsNotNone(created)
        self.assertEqual(created["name"], "Test Procedure")
        self.assertEqual(created["description"], "Procedure for testing")
        self.assertEqual(created["body"], "1. Step one\n2. Step two with | pipe")

        # Test approval inbox parsing and building
        draft = approval_inbox.parse_draft("skill.create", arg)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["name"], "Test Procedure")
        self.assertEqual(draft["description"], "Procedure for testing")
        self.assertEqual(draft["body"], "1. Step one\n2. Step two with | pipe")

        rebuilt = approval_inbox.build_arg("skill.create", draft)
        self.assertEqual(rebuilt, arg)

        # Validation in approval inbox build_arg
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("skill.create", {"name": "Pipe | In | Name", "description": "desc", "body": "body"})
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("skill.create", {"name": "name", "description": "Desc | with | pipe", "body": "body"})
        with self.assertRaises(ValueError):
            approval_inbox.build_arg("skill.create", {"name": "name", "description": "desc", "body": "sk-proj-AbCdEf0123456789AbCdEf0123456789xyz"})


class TestSkillsExplicitInvocation(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self.tmp.name), chat_tools_enabled=False)
        skills.create_skill(
            self.settings,
            name="Daily Review",
            description="Review daily logs and metrics",
            body="Step 1: check error count\nStep 2: summarize incidents",
            slug="daily-review",
        )
        self.port = MagicMock()
        self.port.reply = AsyncMock()
        self.port.send_new = AsyncMock()
        self.port.edit_progress = AsyncMock(return_value=True)

    def tearDown(self) -> None:
        clear_all_pending()
        chat.reset()
        self.tmp.cleanup()

    async def test_ask_chat_with_skill_invocation(self) -> None:
        captured: dict = {}

        async def fake_stream(_cfg, messages):
            captured["messages"] = messages
            yield "Skill executed successfully.\n[[CONFIDENCE: high]]"

        msg = _msg("/skill daily-review check last 24h")
        with patch("runner.chat_client.stream_chat", side_effect=fake_stream):
            outcome, _ = await chat.ask_chat(msg, self.port, self.settings, question=msg.text)

        self.assertEqual(outcome, "answered")
        # System prompt should have the skill wrapped body injected
        system_content = captured["messages"][0]["content"]
        self.assertIn('<skill slug="daily-review">', system_content)
        self.assertIn("Step 1: check error count", system_content)

        # User question passed into chat messages should be the remaining request text
        user_content = captured["messages"][-1]["content"]
        self.assertIn("check last 24h", user_content)

        # use_count was incremented
        sk = skills.get_skill(self.settings, "daily-review")
        self.assertEqual(sk["use_count"], 1)

    async def test_ask_chat_skill_default_prompt_when_empty_request(self) -> None:
        captured: dict = {}

        async def fake_stream(_cfg, messages):
            captured["messages"] = messages
            yield "Done.\n[[CONFIDENCE: high]]"

        msg = _msg("/skill daily-review")
        with patch("runner.chat_client.stream_chat", side_effect=fake_stream):
            await chat.ask_chat(msg, self.port, self.settings, question=msg.text)

        user_content = captured["messages"][-1]["content"]
        self.assertIn("Run this skill.", user_content)

    async def test_ask_chat_unknown_slug_replies_without_model_call(self) -> None:
        mock_stream = AsyncMock()
        msg = _msg("/skill nonexistent-skill do something")
        with patch("runner.chat_client.stream_chat", mock_stream):
            outcome, _ = await chat.ask_chat(msg, self.port, self.settings, question=msg.text)

        self.assertEqual(outcome, "answered")
        mock_stream.assert_not_called()
        self.port.reply.assert_awaited()
        reply_text = self.port.reply.await_args[0][1]
        self.assertIn("Unknown or disabled skill 'nonexistent-skill'", reply_text)
        self.assertIn("daily-review", reply_text)

    async def test_routine_prompt_with_skill_invocation(self) -> None:
        captured: dict = {}

        async def fake_stream(_cfg, messages):
            captured["messages"] = messages
            yield "Routine skill run finished.\n[[CONFIDENCE: high]]"

        # Scheduled routine prompt prefix
        routine_prompt = (
            "[Scheduled routine 'Nightly Audit' running at 2026-10-03T02:00:00Z]\n\n"
            "/skill daily-review audit errors"
        )
        msg = _msg(routine_prompt)
        with patch("runner.chat_client.stream_chat", side_effect=fake_stream):
            await chat.ask_chat(msg, self.port, self.settings, question=routine_prompt)

        system_content = captured["messages"][0]["content"]
        self.assertIn('<skill slug="daily-review">', system_content)
        user_content = captured["messages"][-1]["content"]
        self.assertIn("[Scheduled routine 'Nightly Audit' running at 2026-10-03T02:00:00Z]", user_content)
        self.assertIn("audit errors", user_content)
        self.assertNotIn("/skill daily-review", user_content)

    async def test_flag_off_skill_not_special(self) -> None:
        captured: dict = {}

        async def fake_stream(_cfg, messages):
            captured["messages"] = messages
            yield "Normal reply.\n[[CONFIDENCE: high]]"

        off_settings = _settings(Path(self.tmp.name), skills_enabled=False, chat_tools_enabled=False)
        msg = _msg("/skill daily-review check last 24h")
        with patch("runner.chat_client.stream_chat", side_effect=fake_stream):
            await chat.ask_chat(msg, self.port, off_settings, question=msg.text)

        system_content = captured["messages"][0]["content"]
        self.assertNotIn('<skill slug="daily-review">', system_content)
        user_content = captured["messages"][-1]["content"]
        self.assertIn("/skill daily-review check last 24h", user_content)


class TestSkillsWebApi(unittest.TestCase):
    TOKEN = "test-skills-web-token-12345"

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.settings = _settings(Path(cls.tmp.name))
        cls.control = SimpleNamespace(settings=cls.settings)
        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()
        cls.server = WebConsoleServer(
            ("127.0.0.1", 0), WebConsoleHandler, control=cls.control, loop=cls.loop, token=cls.TOKEN,
        )
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.loop_thread.join(timeout=2)
        cls.loop.close()
        cls.tmp.cleanup()

    def _set_enabled(self, value: bool) -> None:
        import dataclasses
        type(self).settings = dataclasses.replace(self.settings, skills_enabled=value)
        self.control.settings = type(self).settings

    def setUp(self) -> None:
        self._set_enabled(True)
        for s in skills.list_skills(self.settings, include_disabled=True):
            skills.delete_skill(self.settings, s["slug"])

    def request(self, method: str, path: str, body: dict | None = None, *, auth: bool = True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if auth:
            headers["Authorization"] = f"Bearer {self.TOKEN}"
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        try:
            return res.status, json.loads(data.decode("utf-8") or "{}")
        except Exception:
            return res.status, data

    def test_auth_and_flag_disabled(self) -> None:
        # 401 when unauthenticated
        self.assertEqual(self.request("GET", "/api/skills", auth=False)[0], 401)
        self.assertEqual(self.request("POST", "/api/skills", {"name": "x"}, auth=False)[0], 401)
        self.assertEqual(self.request("PUT", "/api/skills/x", {"name": "x"}, auth=False)[0], 401)
        self.assertEqual(self.request("DELETE", "/api/skills/x", auth=False)[0], 401)

        # 409 when flag is disabled
        self._set_enabled(False)
        status, data = self.request("GET", "/api/skills")
        self.assertEqual(status, 409)
        self.assertIn("CONVEYOR_SKILLS_ENABLED", data.get("error", ""))

        status, data = self.request("POST", "/api/skills", {"name": "x", "description": "d", "body": "b"})
        self.assertEqual(status, 409)

    def test_crud_flow(self) -> None:
        # 1. Initial list empty
        status, data = self.request("GET", "/api/skills")
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["items"], [])

        # 2. Create skill
        payload = {
            "name": "Deploy Web",
            "description": "Deploy instructions",
            "triggers": "deploy, prod",
            "body": "Run make deploy",
            "slug": "deploy-web",
            "enabled": True,
        }
        status, created = self.request("POST", "/api/skills", payload)
        self.assertEqual(status, 201)
        self.assertEqual(created["slug"], "deploy-web")
        self.assertEqual(created["name"], "Deploy Web")

        # 3. Get skill
        status, fetched = self.request("GET", "/api/skills/deploy-web")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["description"], "Deploy instructions")

        # 4. Update skill
        update_payload = {
            "description": "Updated deploy instructions",
            "enabled": False,
        }
        status, updated = self.request("PUT", "/api/skills/deploy-web", update_payload)
        self.assertEqual(status, 200)
        self.assertEqual(updated["description"], "Updated deploy instructions")
        self.assertFalse(updated["enabled"])

        # 5. Delete skill
        status, deleted = self.request("DELETE", "/api/skills/deploy-web")
        self.assertEqual(status, 200)
        self.assertTrue(deleted.get("ok"))

        # 6. Get deleted skill -> 404
        status, _ = self.request("GET", "/api/skills/deploy-web")
        self.assertEqual(status, 404)

    def test_validation_and_conflict_errors(self) -> None:
        # Missing required fields
        status, err = self.request("POST", "/api/skills", {"name": "Incomplete"})
        self.assertEqual(status, 400)
        self.assertIn("error", err)

        # Invalid slug
        status, err = self.request("POST", "/api/skills", {
            "name": "Bad Slug",
            "description": "desc",
            "body": "body",
            "slug": "UPPERCASE_NOT_ALLOWED",
        })
        self.assertEqual(status, 400)

        # Duplicate slug conflict -> 409
        status, _ = self.request("POST", "/api/skills", {
            "name": "First",
            "description": "desc",
            "body": "body",
            "slug": "unique-slug",
        })
        self.assertEqual(status, 201)

        status, conflict = self.request("POST", "/api/skills", {
            "name": "Second",
            "description": "desc",
            "body": "body",
            "slug": "unique-slug",
        })
        self.assertEqual(status, 409)
        self.assertIn("already exists", conflict.get("error", ""))

    def test_put_rejects_unknown_fields_and_bad_types(self) -> None:
        skills.create_skill(self.settings, name="Typed", description="d", body="b", slug="typed")
        for payload in ({"slug": "other"}, {"use_count": 9}, {"name": 5}, {"enabled": "false"}, {"body": None}):
            status, data = self.request("PUT", "/api/skills/typed", payload)
            self.assertEqual(status, 400, payload)
            self.assertIn("error", data)
        status, data = self.request("PUT", "/api/skills/typed", {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(data["enabled"])

    def test_post_rejects_bad_types(self) -> None:
        for extra in ({"enabled": "no"}, {"slug": 3}, {"triggers": ["a"]}):
            status, _ = self.request("POST", "/api/skills", {"name": "N", "description": "d", "body": "b", **extra})
            self.assertEqual(status, 400, extra)

    def test_export_and_import_markdown(self) -> None:
        # Create a skill
        skills.create_skill(
            self.settings,
            name="Markdown Skill",
            description="Frontmatter test",
            triggers="md, test",
            body="Markdown body content",
            slug="md-skill",
        )

        # Export endpoint
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/skills/md-skill/export", headers={"Authorization": f"Bearer {self.TOKEN}"})
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        self.assertEqual(res.headers.get("Content-Type"), "text/markdown; charset=utf-8")
        md_content = res.read().decode("utf-8")
        conn.close()

        self.assertIn("name: Markdown Skill", md_content)
        self.assertIn("Markdown body content", md_content)

        # Import via POST /api/skills with {"markdown": "..."}
        import_payload = {
            "markdown": (
                "---\n"
                "name: Imported Skill\n"
                "description: Created from markdown import\n"
                "triggers: import\n"
                "---\n"
                "Imported body procedure"
            )
        }
        status, imported = self.request("POST", "/api/skills", import_payload)
        self.assertEqual(status, 201)
        self.assertEqual(imported["name"], "Imported Skill")
        self.assertEqual(imported["slug"], "imported-skill")
        self.assertEqual(imported["body"].strip(), "Imported body procedure")


class TestSkillsUiReachable(unittest.TestCase):
    def test_system_status_reports_skills_feature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for flag in (True, False):
                fake = SimpleNamespace(
                    settings=_settings(Path(tmp), skills_enabled=flag),
                    queue=SimpleNamespace(list_jobs=lambda n: [], queue_length=0, is_paused=False),
                    started_at=time.time(),
                    nodes=lambda: [],
                )
                status = WebControl.system_status(fake)  # type: ignore[arg-type]
                self.assertIs(status["features"]["skills"], flag)

    def test_skills_tab_in_app_tsx(self) -> None:
        app = (Path(__file__).resolve().parents[1] / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
        switch = app[app.index("onClick={() => setView('tasks')}"):]
        switch = switch[: switch.index("</div>")]
        self.assertIn("setView('skills')", switch)
        self.assertIn("features?.skills", switch)


if __name__ == "__main__":
    unittest.main()
