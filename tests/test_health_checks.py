"""Health-check and deploy-helper regressions found on the production VPS.

- the provider check must probe the provider Codex is configured for, not
  require MINIMAX_API_KEY unconditionally;
- daily worktrees are not orphans;
- the command harness must not inherit deployment env;
- deploy_db resolves the queue database from CODEX_MEMORY_ROOT.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import deploy_db, harness_common, job_audit  # noqa: E402

_PROVIDER_ENV = ("MINIMAX_API_KEY", "MINIMAX_BASE_URL", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _PROVIDER_ENV}
    env.update(extra)
    return env


class ProviderCheckTests(unittest.TestCase):
    settings = SimpleNamespace(codex_model=None)
    deepseek = ("deepseek", {"env_key": "DEEPSEEK_API_KEY", "base_url": "https://api.example.test/v1"}, "deepseek-flash")

    def test_missing_key_names_configured_provider_not_minimax(self) -> None:
        with mock.patch.object(harness_common, "_codex_provider_config", return_value=self.deepseek), \
                mock.patch.dict(os.environ, _clean_env(), clear=True):
            result = harness_common.check_provider_models(self.settings)
        self.assertFalse(result.ok)
        self.assertEqual(result.name, "provider")
        self.assertIn("DEEPSEEK_API_KEY", result.detail)
        self.assertNotIn("MINIMAX", result.detail)

    def test_probes_configured_provider_models(self) -> None:
        seen: dict[str, str] = {}

        def fake_urlopen(request, timeout=0):
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            return io.BytesIO(json.dumps({"data": [{"id": "deepseek-flash"}]}).encode())

        with mock.patch.object(harness_common, "_codex_provider_config", return_value=self.deepseek), \
                mock.patch.dict(os.environ, _clean_env(DEEPSEEK_API_KEY="k-test"), clear=True), \
                mock.patch.object(harness_common.urllib.request, "urlopen", fake_urlopen):
            result = harness_common.check_provider_models(self.settings)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(seen["url"], "https://api.example.test/v1/models")
        self.assertEqual(seen["auth"], "Bearer k-test")

    def test_unlisted_model_fails(self) -> None:
        def fake_urlopen(request, timeout=0):
            return io.BytesIO(json.dumps({"data": [{"id": "other"}]}).encode())

        with mock.patch.object(harness_common, "_codex_provider_config", return_value=self.deepseek), \
                mock.patch.dict(os.environ, _clean_env(DEEPSEEK_API_KEY="k-test"), clear=True), \
                mock.patch.object(harness_common.urllib.request, "urlopen", fake_urlopen):
            result = harness_common.check_provider_models(self.settings)
        self.assertFalse(result.ok)
        self.assertIn("deepseek-flash not listed", result.detail)

    def test_no_provider_and_no_key_is_not_a_failure(self) -> None:
        with mock.patch.object(harness_common, "_codex_provider_config", return_value=("", {}, None)), \
                mock.patch.dict(os.environ, _clean_env(), clear=True):
            result = harness_common.check_provider_models(self.settings)
        self.assertTrue(result.ok, result.detail)


class JobAuditTests(unittest.TestCase):
    def test_daily_worktrees_are_not_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_root = Path(tmp) / "tasks"
            for name in ("day-2026-06-09", "20260101-000000-deadbeef"):
                (task_root / "worktrees" / name).mkdir(parents=True)
            (task_root / "logs").mkdir()
            settings = SimpleNamespace(codex_task_root=task_root)
            runner = SimpleNamespace(job_records=lambda _limit: [])
            with mock.patch.object(job_audit, "load_settings", return_value=settings), \
                    mock.patch.object(job_audit, "CodexRunner", return_value=runner):
                results = {r.name: r for r in job_audit.run_job_audit(".env", stale_minutes=60)}
        orphan = results["orphan worktrees"]
        self.assertFalse(orphan.ok)
        self.assertEqual(orphan.detail, "20260101-000000-deadbeef")


class CommandHarnessIsolationTests(unittest.TestCase):
    def test_harness_drops_deployment_env(self) -> None:
        env = dict(os.environ)
        env.update(
            CONVEYOR_CHAT_MODE="auto",
            CONVEYOR_CHAT_API_KEY="live-key",
            DEEPSEEK_API_KEY="live-key",
            TELEGRAM_BOT_TOKEN="live-token",
            CODEX_MEMORY_ROOT="/nonexistent/production-memory",
            PYTHONDONTWRITEBYTECODE="1",
        )
        code = (
            "import json, os, scripts.command_harness as h;"
            "print(json.dumps({k: os.environ.get(k) for k in "
            "['CONVEYOR_CHAT_MODE','CONVEYOR_CHAT_API_KEY','DEEPSEEK_API_KEY',"
            "'TELEGRAM_BOT_TOKEN','CODEX_MEMORY_ROOT','CONVEYOR_ENV_FILE']}))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=True,
        ).stdout.strip().splitlines()[-1]
        seen = json.loads(out)
        self.assertIsNone(seen["CONVEYOR_CHAT_MODE"])
        self.assertIsNone(seen["CONVEYOR_CHAT_API_KEY"])
        self.assertIsNone(seen["DEEPSEEK_API_KEY"])
        self.assertEqual(seen["TELEGRAM_BOT_TOKEN"], "fake-token")
        self.assertNotIn("production-memory", seen["CODEX_MEMORY_ROOT"])
        self.assertTrue(seen["CONVEYOR_ENV_FILE"].endswith("harness.env"))


class FeishuCardActionTests(unittest.TestCase):
    def test_bot_registers_the_sdk_card_action_event(self) -> None:
        from lark_oapi.channel import _coerce

        source = (ROOT / "feishu_bot.py").read_text(encoding="utf-8")
        self.assertIn('channel.on("cardAction", _handle_card_action)', source)
        self.assertIn("cardAction", _coerce.VALID_EVENTS)
        self.assertNotIn(_coerce.normalize_event_name("card.action.trigger"), _coerce.VALID_EVENTS)

    def test_extracts_sdk_card_action_event(self) -> None:
        from lark_oapi.channel.types import CardActionEvent, CardActionPayload, EventOperator

        from channel.feishu_cards import extract_card_action

        event = CardActionEvent(
            message_id="om_msg",
            chat_id="oc_chat",
            operator=EventOperator(open_id="ou_user"),
            action=CardActionPayload(value={"action": "apply", "job_id": "abc"}, tag="button"),
        )
        extracted = extract_card_action(event)
        self.assertIsNotNone(extracted)
        identity, payload = extracted
        self.assertEqual(identity, {"operator_id": "ou_user", "chat_id": "oc_chat", "message_id": "om_msg"})
        self.assertEqual(payload["action"], "apply")


class LogRedactionTests(unittest.TestCase):
    def test_feishu_session_credentials_are_redacted(self) -> None:
        from redaction import redact_text

        line = "connected to wss://msg-frontier.feishu.cn/ws/v2?fpid=493&access_key=c353f06271cc&service_id=3&ticket=63145737-95ce [conn_id=7]"
        out = redact_text(line)
        self.assertNotIn("c353f06271cc", out)
        self.assertNotIn("63145737-95ce", out)
        self.assertIn("service_id=3", out)


class DeployDbTests(unittest.TestCase):
    def test_counts_and_backup_follow_memory_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state" / "job_queue.sqlite3"
            db.parent.mkdir()
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE queued_jobs (id TEXT, state TEXT)")
            conn.executemany("INSERT INTO queued_jobs VALUES (?, ?)", [("a", "queued"), ("b", "running"), ("c", "completed")])
            conn.commit()
            conn.close()
            with mock.patch.dict(os.environ, {"CODEX_MEMORY_ROOT": tmp}):
                self.assertEqual(deploy_db.db_path(), db.resolve())
                self.assertEqual(deploy_db.queue_counts(deploy_db.db_path()), (1, 1))
            env = dict(os.environ, CODEX_MEMORY_ROOT=tmp)
            backup = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "deploy_db.py"), "backup"],
                env=env, stdout=subprocess.PIPE, timeout=30, check=True,
            ).stdout
            copy = Path(tmp) / "copy.sqlite3"
            copy.write_bytes(backup)
            restored = sqlite3.connect(str(copy))
            try:
                self.assertEqual(restored.execute("SELECT COUNT(*) FROM queued_jobs").fetchone()[0], 3)
            finally:
                restored.close()

    def test_missing_database_is_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(deploy_db.queue_counts(Path(tmp) / "nope.sqlite3"), (0, 0))


if __name__ == "__main__":
    unittest.main()
