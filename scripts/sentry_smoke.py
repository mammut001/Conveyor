#!/usr/bin/env python3
"""scripts/sentry_smoke.py — verify Always-On Teammate & Proactive Sentry Engine.

Tests:
1. SentryState lifecycle: pause, resume, mute, unmute, effective paused expiry, persistence.
2. Anti-spam cooldown gate: fingerprint deduplication & cooldown throttling.
3. Sentry checkers: disk usage threshold, CPU/load check, service failure, error logs burst.
4. Alert formatting: actionable cards with suggested slash commands.
5. Command dispatch & subcommands: /teammate status, check, pause, resume, mute.
6. Natural language routing: deterministic routing for teammate/sentry phrases.
7. Personal tool registry execution.
"""
from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings, load_settings
from handlers.intent import route_intent
from personal_tools.registry import execute_personal_tool, get_personal_tool
from personal_tools.sentry import (
    SentryAlert,
    SentryState,
    check_cpu_and_load,
    check_disk_space,
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

TMP = Path(tempfile.mkdtemp(prefix="conveyor-sentry-smoke-"))


def _settings(**kw) -> Settings:
    base = dataclasses.replace(
        load_settings(),
        codex_workspace_root=TMP / "ws",
        codex_task_root=TMP / "tasks",
        codex_memory_root=TMP / "mem",
        teammate_enabled=True,
        sentry_interval_seconds=300,
        sentry_cooldown_seconds=7200,
        sentry_disk_threshold_pct=90.0,
        sentry_disk_threshold_gb=3.0,
        sentry_load_threshold_ratio=2.0,
        sentry_error_burst_threshold=5,
        sentry_monitored_services=("conveyor-test-bot",),
    )
    return dataclasses.replace(base, **kw)


def test_state_lifecycle():
    settings = _settings()
    state = SentryState.load(settings)
    assert not state.is_paused
    assert not state.is_effective_paused()

    # Pause
    until = state.pause(hours=2.0)
    assert state.is_paused
    assert state.is_effective_paused()
    assert state.paused_until == until

    # Save and reload
    state.save(settings)
    loaded = SentryState.load(settings)
    assert loaded.is_paused
    assert loaded.paused_until == until

    # Resume
    loaded.resume()
    assert not loaded.is_paused
    assert not loaded.is_effective_paused()
    loaded.save(settings)

    # Mute / Unmute
    assert loaded.mute("host.disk") is True
    assert loaded.mute("host.disk") is False
    assert "host.disk" in loaded.muted_sources
    assert loaded.unmute("host.disk") is True
    assert loaded.unmute("host.disk") is False
    assert "host.disk" not in loaded.muted_sources

    # Expired pause test
    past_dt = datetime.now(timezone.utc) - timedelta(hours=1)
    loaded.is_paused = True
    loaded.paused_until = past_dt.isoformat()
    assert not loaded.is_effective_paused()
    assert not loaded.is_paused  # Auto unpaused

    print("[ok] sentry state lifecycle: pause/resume/mute/persistence verified")


def test_anti_spam_cooldown():
    settings = _settings()
    state = SentryState()
    fp = "test.alert.fingerprint"

    # Initially can alert
    assert state.can_alert(fp, cooldown_seconds=100.0) is True

    # Record alert
    alert = SentryAlert(
        source="test",
        severity="warning",
        title="Test Alert",
        summary="Test summary",
        fingerprint=fp,
    )
    state.record_alert(alert)
    assert state.total_alerts_count == 1
    assert len(state.recent_alerts) == 1

    # Immediate next check should be throttled by cooldown
    assert state.can_alert(fp, cooldown_seconds=100.0) is False

    # Simulate past timestamp
    state.last_alert_at[fp] = time.time() - 200.0
    assert state.can_alert(fp, cooldown_seconds=100.0) is True

    print("[ok] sentry anti-spam cooldown gate verified")


def test_checkers():
    settings = _settings()

    # 1. Disk space checker with mock low disk
    mock_usage = MagicMock()
    mock_usage.total = 100 * (1024 ** 3)
    mock_usage.free = 1 * (1024 ** 3)  # 1 GB free (exceeds < 3GB and > 90% threshold)
    with patch("shutil.disk_usage", return_value=mock_usage):
        alerts = check_disk_space(settings)
        assert len(alerts) > 0
        assert any(a.source == "host.disk" for a in alerts)
        assert any("磁盘空间偏低" in a.title for a in alerts)
        assert any(act["command"] == "/clean" for act in alerts[0].suggested_actions)

    # 2. CPU / load checker with mock high load
    with patch("os.getloadavg", return_value=(16.0, 12.0, 10.0)), \
         patch("os.cpu_count", return_value=4):
        alerts = check_cpu_and_load(settings)
        assert len(alerts) == 1
        assert alerts[0].source == "host.cpu"
        assert "CPU 负载过高" in alerts[0].title
        assert any(act["command"] == "/ps" for act in alerts[0].suggested_actions)

    # 3. Systemd service failure checker
    with patch("shutil.which", return_value="/bin/systemctl"), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="failed\n")
        alerts = check_services(settings)
        assert len(alerts) == 1
        assert alerts[0].source == "host.service"
        assert "conveyor-test-bot" in alerts[0].title
        assert alerts[0].severity == "critical"

    # 4. Error log spikes checker
    with patch("shutil.which", return_value="/bin/journalctl"), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="err line 1\nerr line 2\nerr line 3\nerr line 4\nerr line 5\nerr line 6\n"
        )
        alerts = check_log_error_spikes(settings)
        assert len(alerts) == 1
        assert alerts[0].source == "host.logs"
        assert "日志错误突增预警" in alerts[0].title

    # 5. Git status uncommitted files checker
    ws = settings.codex_workspace_root
    ws.mkdir(parents=True, exist_ok=True)
    (ws / ".git").mkdir(parents=True, exist_ok=True)
    with patch("subprocess.run") as mock_run:
        dirty_lines = "\n".join(f" M file_{i}.py" for i in range(25))
        mock_run.return_value = MagicMock(returncode=0, stdout=dirty_lines)
        alerts = check_repo_git(settings)
        assert len(alerts) == 1
        assert alerts[0].source == "repo.git"
        assert "未提交变更" in alerts[0].title

    print("[ok] sentry checkers (disk, load, services, logs, git) verified")


def test_alert_formatting_and_patrol():
    settings = _settings()
    alert = SentryAlert(
        source="host.disk",
        severity="critical",
        title="磁盘空间偏低 (/)",
        summary="挂载路径 `/` 可用空间仅剩 0.5 GB",
        fingerprint="host.disk./",
        suggested_actions=[{"label": "清理任务", "command": "/clean"}],
    )
    formatted = format_alert_text(alert)
    assert "🚨" in formatted
    assert "【全天候守护·主动预警】" in formatted
    assert "`/clean`" in formatted
    assert "/teammate pause" in formatted

    # Patrol suppression test
    with patch("personal_tools.sentry.check_disk_space", return_value=[alert]), \
         patch("personal_tools.sentry.check_cpu_and_load", return_value=[]), \
         patch("personal_tools.sentry.check_services", return_value=[]), \
         patch("personal_tools.sentry.check_log_error_spikes", return_value=[]), \
         patch("personal_tools.sentry.check_repo_git", return_value=[]), \
         patch("personal_tools.sentry.check_github_ci", return_value=[]):

        # First patrol delivers
        to_del, supp = run_sentry_patrol(settings, force=False, dry_run=False)
        assert len(to_del) == 1
        assert len(supp) == 0

        # Immediate second patrol suppresses due to cooldown
        to_del2, supp2 = run_sentry_patrol(settings, force=False, dry_run=False)
        assert len(to_del2) == 0
        assert len(supp2) == 1

        # Force patrol ignores cooldown
        to_del3, supp3 = run_sentry_patrol(settings, force=True, dry_run=True)
        assert len(to_del3) == 1

    # Delivery dry-run
    delivered = deliver_sentry_alerts(settings, [alert], dry_run=True)
    assert delivered == 1

    print("[ok] alert formatting, patrol suppression, and dry-run delivery verified")


def test_teammate_commands():
    settings = _settings()

    # Status
    status_str = run_teammate_command(settings, "")
    assert "全天候智能体队友" in status_str
    assert "活跃巡检中" in status_str

    # Pause & Resume
    pause_res = run_teammate_command(settings, "pause 4")
    assert "已暂停全天候队友主动提醒" in pause_res
    assert "4 小时" in pause_res

    resume_res = run_teammate_command(settings, "resume")
    assert "已恢复全天候队友的主动巡检" in resume_res

    # Mute & Unmute
    mute_res = run_teammate_command(settings, "mute host.cpu")
    assert "已静音告警源" in mute_res

    unmute_res = run_teammate_command(settings, "unmute host.cpu")
    assert "已取消静音告警源" in unmute_res

    # Live check
    check_res = run_teammate_command(settings, "check")
    assert "实时系统巡检完成" in check_res

    print("[ok] /teammate subcommands (status, pause, resume, mute, check) verified")


def test_nl_routing_and_tool_execution():
    # Deterministic NL routing
    r_check = route_intent("立刻巡检")
    assert r_check.kind == "deterministic"
    assert "teammate.check" in r_check.tools

    r_sys_check = route_intent("系统巡检")
    assert r_sys_check.kind == "deterministic"
    assert "teammate.check" in r_sys_check.tools

    r_status = route_intent("队友状态")
    assert r_status.kind == "deterministic"
    assert "teammate.status" in r_status.tools

    r_pause = route_intent("暂停巡检")
    assert r_pause.kind == "deterministic"
    assert "teammate.pause" in r_pause.tools

    r_resume = route_intent("恢复巡检")
    assert r_resume.kind == "deterministic"
    assert "teammate.resume" in r_resume.tools

    # Registry lookup
    assert get_personal_tool("teammate.status") is not None
    assert get_personal_tool("teammate.check") is not None
    assert get_personal_tool("teammate.pause") is not None
    assert get_personal_tool("teammate.resume") is not None

    # Async execution of personal tools
    settings = _settings()

    async def _run_async_checks():
        res_status = await execute_personal_tool(settings, "teammate.status", "", operator_id="test")
        assert "全天候智能体队友" in res_status

        res_check = await execute_personal_tool(settings, "teammate.check", "", operator_id="test")
        assert "实时系统巡检完成" in res_check

        res_pause = await execute_personal_tool(settings, "teammate.pause", "3", operator_id="test")
        assert "已暂停" in res_pause

        res_resume = await execute_personal_tool(settings, "teammate.resume", "", operator_id="test")
        assert "已恢复" in res_resume

    asyncio.run(_run_async_checks())
    print("[ok] natural language routing & personal tool execution verified")


def main():
    try:
        test_state_lifecycle()
        test_anti_spam_cooldown()
        test_checkers()
        test_alert_formatting_and_patrol()
        test_teammate_commands()
        test_nl_routing_and_tool_execution()
        print("\nAll Always-On Teammate sentry smoke tests PASSED successfully! 🛡️")
        return 0
    finally:
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
