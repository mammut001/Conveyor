"""tests/test_sentry.py — Unit tests for Always-On Teammate & Sentry Engine."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import dataclasses
from config import Settings, load_settings
from handlers.intent import route_intent
from personal_tools.registry import execute_personal_tool, get_personal_tool
from personal_tools.sentry import (
    SentryAlert,
    SentryState,
    check_cpu_and_load,
    check_disk_space,
    check_github_ci,
    check_log_error_spikes,
    check_repo_git,
    check_services,
    deliver_sentry_alerts,
    format_alert_text,
    is_sentry_due,
    run_sentry_patrol,
    run_teammate_command,
    teammate_status_text,
)


class TestSentryEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="conveyor-test-sentry-"))
        self.mem = self.tmp / "memory"
        self.mem.mkdir(parents=True, exist_ok=True)
        self.ws = self.tmp / "ws"
        self.ws.mkdir(parents=True, exist_ok=True)
        base = load_settings()
        self.settings = dataclasses.replace(
            base,
            telegram_bot_token="fake-token",
            telegram_allowed_user_id=12345,
            codex_workspace_root=self.ws,
            codex_bin="codex",
            codex_task_root=self.tmp / "tasks",
            codex_memory_root=self.mem,
            teammate_enabled=True,
            sentry_interval_seconds=300,
            sentry_cooldown_seconds=7200,
            sentry_disk_threshold_pct=90.0,
            sentry_disk_threshold_gb=3.0,
            sentry_load_threshold_ratio=2.0,
            sentry_error_burst_threshold=5,
            sentry_monitored_services=("conveyor-test-bot",),
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sentry_state_persistence_and_expiry(self):
        state = SentryState.load(self.settings)
        self.assertFalse(state.is_paused)
        self.assertFalse(state.is_effective_paused())

        # Pause 3 hours
        until = state.pause(hours=3.0)
        self.assertTrue(state.is_paused)
        self.assertTrue(state.is_effective_paused())
        self.assertEqual(state.paused_until, until)

        state.save(self.settings)
        reloaded = SentryState.load(self.settings)
        self.assertTrue(reloaded.is_paused)
        self.assertEqual(reloaded.paused_until, until)

        # Resume
        reloaded.resume()
        self.assertFalse(reloaded.is_paused)
        self.assertFalse(reloaded.is_effective_paused())

        # Mute / unmute
        self.assertTrue(reloaded.mute("host.cpu"))
        self.assertFalse(reloaded.mute("host.cpu"))
        self.assertIn("host.cpu", reloaded.muted_sources)
        self.assertTrue(reloaded.unmute("host.cpu"))
        self.assertFalse(reloaded.unmute("host.cpu"))
        self.assertNotIn("host.cpu", reloaded.muted_sources)

        # Expired pause
        past_dt = datetime.now(timezone.utc) - timedelta(hours=1)
        reloaded.is_paused = True
        reloaded.paused_until = past_dt.isoformat()
        self.assertFalse(reloaded.is_effective_paused())
        self.assertFalse(reloaded.is_paused)

    def test_cooldown_anti_spam(self):
        state = SentryState()
        fp = "alert.fingerprint.1"

        self.assertTrue(state.can_alert(fp, cooldown_seconds=3600.0))
        alert = SentryAlert(
            source="host.disk",
            severity="warning",
            title="Disk warning",
            summary="Disk space low",
            fingerprint=fp,
        )
        state.record_alert(alert)
        self.assertEqual(state.total_alerts_count, 1)
        self.assertEqual(len(state.recent_alerts), 1)

        # Throttled by cooldown
        self.assertFalse(state.can_alert(fp, cooldown_seconds=3600.0))

        # After cooldown elapsed
        state.last_alert_at[fp] = time.time() - 4000.0
        self.assertTrue(state.can_alert(fp, cooldown_seconds=3600.0))

    def test_check_disk_space(self):
        mock_usage = MagicMock()
        mock_usage.total = 100 * (1024 ** 3)
        mock_usage.free = 2 * (1024 ** 3)  # 2 GB free -> trigger threshold
        with patch("shutil.disk_usage", return_value=mock_usage):
            alerts = check_disk_space(self.settings)
            self.assertTrue(len(alerts) > 0)
            self.assertEqual(alerts[0].source, "host.disk")
            self.assertIn("磁盘空间偏低", alerts[0].title)

    def test_check_cpu_and_load(self):
        with patch("os.getloadavg", return_value=(8.0, 6.0, 5.0)), \
             patch("os.cpu_count", return_value=2):
            alerts = check_cpu_and_load(self.settings)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].source, "host.cpu")
            self.assertIn("CPU 负载过高", alerts[0].title)

    def test_check_services(self):
        with patch("shutil.which", return_value="/bin/systemctl"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="inactive\n")
            alerts = check_services(self.settings)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].source, "host.service")
            self.assertEqual(alerts[0].severity, "critical")

    def test_check_log_error_spikes(self):
        with patch("shutil.which", return_value="/bin/journalctl"), \
             patch("subprocess.run") as mock_run:
            errors = "\n".join(f"error log line {i}" for i in range(10))
            mock_run.return_value = MagicMock(returncode=0, stdout=errors)
            alerts = check_log_error_spikes(self.settings)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].source, "host.logs")

    def test_check_repo_git(self):
        (self.ws / ".git").mkdir(parents=True, exist_ok=True)
        with patch("subprocess.run") as mock_run:
            dirty = "\n".join(f" M file_{i}.py" for i in range(30))
            mock_run.return_value = MagicMock(returncode=0, stdout=dirty)
            alerts = check_repo_git(self.settings)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].source, "repo.git")

    def test_run_sentry_patrol_due_and_suppression(self):
        alert = SentryAlert(
            source="host.disk",
            severity="warning",
            title="Disk low",
            summary="Disk low summary",
            fingerprint="disk.fp",
        )
        with patch("personal_tools.sentry.check_disk_space", return_value=[alert]), \
             patch("personal_tools.sentry.check_cpu_and_load", return_value=[]), \
             patch("personal_tools.sentry.check_services", return_value=[]), \
             patch("personal_tools.sentry.check_log_error_spikes", return_value=[]), \
             patch("personal_tools.sentry.check_repo_git", return_value=[]), \
             patch("personal_tools.sentry.check_github_ci", return_value=[]):

            to_del, supp = run_sentry_patrol(self.settings, force=False, dry_run=False)
            self.assertEqual(len(to_del), 1)
            self.assertEqual(len(supp), 0)

            # Check not due on second check_due run
            to_del2, supp2 = run_sentry_patrol(self.settings, force=False, check_due=True)
            self.assertEqual(len(to_del2), 0)

    def test_format_alert_and_delivery(self):
        alert = SentryAlert(
            source="host.cpu",
            severity="warning",
            title="CPU Load High",
            summary="Load average is 10.0",
            fingerprint="cpu.high",
            suggested_actions=[{"label": "查看进程", "command": "/ps"}],
        )
        text = format_alert_text(alert)
        self.assertIn("【全天候守护·主动预警】", text)
        self.assertIn("`/ps`", text)

        sent = deliver_sentry_alerts(self.settings, [alert], dry_run=True)
        self.assertEqual(sent, 1)

    def test_teammate_commands(self):
        status = run_teammate_command(self.settings, "")
        self.assertIn("全天候智能体队友", status)

        pause_res = run_teammate_command(self.settings, "pause 5")
        self.assertIn("5 小时", pause_res)

        resume_res = run_teammate_command(self.settings, "resume")
        self.assertIn("已恢复", resume_res)

        mute_res = run_teammate_command(self.settings, "mute host.disk")
        self.assertIn("已静音", mute_res)

        unmute_res = run_teammate_command(self.settings, "unmute host.disk")
        self.assertIn("已取消静音", unmute_res)

        check_res = run_teammate_command(self.settings, "check")
        self.assertIn("实时系统巡检完成", check_res)

    def test_intent_routing_and_personal_tools(self):
        for phrase, tool in [
            ("立刻巡检", "teammate.check"),
            ("系统巡检", "teammate.check"),
            ("队友状态", "teammate.status"),
            ("暂停巡检", "teammate.pause"),
            ("恢复巡检", "teammate.resume"),
        ]:
            route = route_intent(phrase)
            self.assertEqual(route.kind, "deterministic")
            self.assertIn(tool, route.tools)

        self.assertIsNotNone(get_personal_tool("teammate.status"))
        self.assertIsNotNone(get_personal_tool("teammate.check"))

        async def _exec():
            res = await execute_personal_tool(self.settings, "teammate.status", "", operator_id="test")
            self.assertIn("全天候智能体队友", res)

    def test_teammate_pulse(self):
        morning = run_teammate_command(self.settings, "pulse morning")
        self.assertIn("今日早报与晨会", morning)
        self.assertIn("系统与哨兵健康状态", morning)

        evening = run_teammate_command(self.settings, "pulse evening")
        self.assertIn("今日晚报与总结", evening)
        self.assertIn("夜间值守就绪", evening)

        r_m = route_intent("今日早报")
        self.assertEqual(r_m.kind, "deterministic")
        self.assertIn("teammate.pulse", r_m.tools)
        self.assertEqual(r_m.arg, "morning")

        r_e = route_intent("今日晚报")
        self.assertEqual(r_e.kind, "deterministic")
        self.assertIn("teammate.pulse", r_e.tools)
        self.assertEqual(r_e.arg, "evening")


if __name__ == "__main__":
    unittest.main()
