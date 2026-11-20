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
from conveyor.core.config import Settings, load_settings
from handlers.intent import route_intent
from personal_tools.registry import execute_personal_tool, get_personal_tool
from personal_tools.sentry import (
    SentryAlert,
    SentryState,
    _is_ssh_scanner_noise,
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
            self.assertEqual(
                [a["label"] for a in alerts[0].suggested_actions],
                ["清理旧任务", "查看详细磁盘挂载情况"],
            )

    def test_check_cpu_and_load(self):
        with patch("os.getloadavg", return_value=(8.0, 6.0, 5.0)), \
             patch("os.cpu_count", return_value=2), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            alerts = check_cpu_and_load(self.settings)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].source, "host.cpu")
            self.assertIn("CPU 负载过高", alerts[0].title)

    def test_check_cpu_and_load_linux_top_processes(self):
        ps_stdout = (
            "ghrunner         65.5  101 chrome-headless\n"
            "ghrunner         61.0  102 chrome-headless\n"
            "ghrunner         59.3  103 next-server (v1\n"
            "ubuntu           21.8  104 python\n"
            "root              0.0    1 systemd\n"
        )
        with patch("os.getloadavg", return_value=(17.63, 12.0, 8.0)), \
             patch("os.cpu_count", return_value=4), \
             patch("sys.platform", "linux"), \
             patch("personal_tools.sentry._sample_cpu_percent", return_value=None), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=ps_stdout)) as mock_run:
            alerts = check_cpu_and_load(self.settings)

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].source, "host.cpu")
        # One line: the 值守 card shows only the first line of a summary.
        self.assertEqual(
            alerts[0].summary,
            "1分钟负载达到 17.63 (4 核 CPU，负载比例 4.4x)。"
            "占用最多：chrome-headless（ghrunner，126%）、next-server（ghrunner，59%）、python（ubuntu，22%）。",
        )
        argv = mock_run.call_args.args[0]
        self.assertIn("--sort=-pcpu", argv)
        self.assertNotIn("-r", argv)

    def test_node_main_thread_is_named_by_its_program(self):
        # Node.js renames its main thread; ps then reports comm "MainThread".
        ps_stdout = (
            "ghrunner        168.0  3185231 MainThread\n"
            "ghrunner        129.0  3185239 MainThread\n"
            "ubuntu           13.7  3178306 python\n"
        )
        def fake_program(pid):
            return {"3185231": "node", "3185239": "node"}.get(pid, "")
        with patch("os.getloadavg", return_value=(13.15, 10.0, 8.0)), \
             patch("os.cpu_count", return_value=4), \
             patch("sys.platform", "linux"), \
             patch("personal_tools.sentry._sample_cpu_percent", return_value=None), \
             patch("personal_tools.sentry._program_name", side_effect=fake_program), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=ps_stdout)):
            alerts = check_cpu_and_load(self.settings)
        self.assertIn("占用最多：node（ghrunner，297%）、python（ubuntu，14%）。", alerts[0].summary)
        self.assertNotIn("MainThread", alerts[0].summary)

    def test_truncated_linux_names_use_the_program_name(self):
        ps_stdout = (
            "ubuntu            4.0   422115 Isolated Web Co\n"
            "ubuntu            2.0   422116 Isolated Web Co\n"
            "ghrunner         30.0      900 chrome-headless\n"
            "root              1.5       12 kworker/R-rcu_g\n"
            "root              1.0       77 networkd-dispat\n"
        )
        names = {"422115": "firefox", "422116": "firefox", "900": "chrome-headless-shell", "12": "", "77": "python3"}
        with patch("os.getloadavg", return_value=(9.0, 8.0, 8.0)), \
             patch("os.cpu_count", return_value=4), \
             patch("sys.platform", "linux"), \
             patch("personal_tools.sentry._sample_cpu_percent", return_value=None), \
             patch("personal_tools.sentry._program_name", side_effect=lambda pid: names.get(pid, "")), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=ps_stdout)):
            alerts = check_cpu_and_load(self.settings)
        self.assertIn(
            "占用最多：chrome-headless-shell（ghrunner，30%）、firefox（ubuntu，6%）、kworker/R-rcu_g（root，2%）。",
            alerts[0].summary,
        )
        with patch("sys.platform", "linux"), \
             patch("personal_tools.sentry._sample_cpu_percent", return_value=None), \
             patch("personal_tools.sentry._program_name", side_effect=lambda pid: names.get(pid, "")), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=ps_stdout)):
            from personal_tools.sentry import _top_cpu_processes
            groups = {name for name, _user, _cpu in _top_cpu_processes(limit=5)}
        # An interpreted script keeps its own (truncated) name, not "python3".
        self.assertIn("networkd-dispat", groups)
        self.assertNotIn("python3", groups)

    def test_program_name_prefers_exe_and_handles_rewritten_cmdlines(self):
        import tempfile
        from personal_tools import sentry as sentry_mod
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "41").mkdir()
            os.symlink("/snap/firefox/9136/usr/lib/firefox/firefox", root / "41" / "exe")
            (root / "41" / "cmdline").write_bytes(b"/snap/firefox/x/firefox -contentproc -appDir /b 8 tab")
            (root / "42").mkdir()  # exe unreadable (another user): command line rewritten by Firefox
            (root / "42" / "cmdline").write_bytes(b"/snap/firefox/x/firefox -contentproc -appDir /b 8 tab")
            (root / "43").mkdir()  # ordinary NUL-separated command line
            (root / "43" / "cmdline").write_bytes(b"/usr/bin/node\0/srv/app/server.js\0--port\0")
            real_path = sentry_mod.Path
            with patch.object(sentry_mod, "Path", side_effect=lambda p: root if str(p) == "/proc" else real_path(p)):
                self.assertEqual(sentry_mod._program_name("41"), "firefox")
                self.assertEqual(sentry_mod._program_name("42"), "firefox")
                self.assertEqual(sentry_mod._program_name("43"), "node")

    def test_linux_ranking_uses_sampled_cpu_not_lifetime_average(self):
        # ps %CPU is a lifetime average: the long-idle browser ranks high, the busy
        # node process barely shows. The /proc sample decides the order.
        ps_stdout = (
            "ubuntu           60.0   501 firefox\n"
            "ghrunner          2.0   777 node\n"
            "root              0.5   901 sshd\n"
        )
        sample = {"501": 0.4, "777": 95.0, "901": 0.0}
        with patch("os.getloadavg", return_value=(9.0, 8.0, 8.0)), \
             patch("os.cpu_count", return_value=4), \
             patch("sys.platform", "linux"), \
             patch("personal_tools.sentry._sample_cpu_percent", return_value=sample) as sampler, \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=ps_stdout)):
            alerts = check_cpu_and_load(self.settings)
        self.assertIn("占用最多：node（ghrunner，95%）、firefox（ubuntu，0%）。", alerts[0].summary)
        self.assertEqual(sorted(sampler.call_args.args[0]), ["501", "777", "901"])

    def test_sample_cpu_percent_reads_proc_stat_deltas(self):
        import tempfile
        from personal_tools import sentry as sentry_mod
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "self").mkdir()
            (root / "self" / "stat").write_text("1 (x) S 0 0 0 0 0 0 0 0 0 0 0 0\n")
            for pid in ("10", "11"):
                (root / pid).mkdir()
            def write(pid, utime, stime):
                fields = ["S"] + ["0"] * 10 + [str(utime), str(stime)] + ["0"] * 5
                (root / pid / "stat").write_text(f"{pid} (my (odd) name) " + " ".join(fields) + "\n")
            write("10", 100, 50)
            write("11", 7, 3)
            def advance(_seconds):
                write("10", 150, 100)  # +100 ticks
                write("11", 7, 3)      # idle
            real_path = sentry_mod.Path
            with patch.object(sentry_mod, "Path", side_effect=lambda p: (root / str(p)[len("/proc/"):]) if str(p).startswith("/proc/") else (root if str(p) == "/proc" else real_path(p))), \
                 patch("os.sysconf", return_value=100), \
                 patch("time.sleep", side_effect=advance):
                result = sentry_mod._sample_cpu_percent(["10", "11", "99"], interval=1.0)
        self.assertEqual(result, {"10": 100.0, "11": 0.0})

    def test_program_name_reads_proc_cmdline_and_tolerates_missing_pids(self):
        from personal_tools.sentry import _program_name
        self.assertEqual(_program_name("not-a-pid"), "")
        self.assertEqual(_program_name("999999999"), "")
        if Path("/proc/self/cmdline").exists():
            self.assertTrue(_program_name(str(os.getpid())))

    def test_check_cpu_and_load_darwin_top_processes(self):
        ps_stdout = (
            "USER              %CPU   PID COMM\n"
            "alice             45.2   501 /Applications/My App.app/Contents/MacOS/My App\n"
            "root              10.4   502 /usr/bin/foo\n"
        )
        with patch("os.getloadavg", return_value=(8.0, 6.0, 5.0)), \
             patch("os.cpu_count", return_value=2), \
             patch("sys.platform", "darwin"), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=ps_stdout)) as mock_run:
            alerts = check_cpu_and_load(self.settings)

        self.assertEqual(len(alerts), 1)
        self.assertIn("占用最多：My App（alice，45%）、foo（root，10%）。", alerts[0].summary)
        argv = mock_run.call_args.args[0]
        self.assertIn("-r", argv)

    def test_check_cpu_and_load_ps_failure(self):
        expected_summary = "1分钟负载达到 8.00 (2 核 CPU，负载比例 4.0x)。"
        for run_patch in (
            patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")),
            patch("subprocess.run", side_effect=OSError("ps failed")),
        ):
            with self.subTest(run_patch=run_patch):
                with patch("os.getloadavg", return_value=(8.0, 6.0, 5.0)), \
                     patch("os.cpu_count", return_value=2), \
                     run_patch:
                    alerts = check_cpu_and_load(self.settings)
                self.assertEqual(len(alerts), 1)
                self.assertEqual(alerts[0].summary, expected_summary)
                self.assertNotIn("占用最多", alerts[0].summary)

    def test_check_services(self):
        with patch("shutil.which", return_value="/bin/systemctl"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="inactive\n")
            alerts = check_services(self.settings)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].source, "host.service")
            self.assertEqual(alerts[0].severity, "critical")
            self.assertEqual(alerts[0].summary, "当前状态：未运行。")

    def test_check_services_describes_systemd_failure_in_chinese(self):
        def systemctl(cmd, **_kwargs):
            if cmd[1] == "show":
                return MagicMock(returncode=0, stdout="ActiveState=failed\nSubState=failed\nResult=exit-code\n")
            return MagicMock(returncode=3, stdout="failed\n" if cmd[2] == "conveyor-test-bot" else "inactive\n")

        with patch("shutil.which", return_value="/bin/systemctl"), \
             patch("subprocess.run", side_effect=systemctl):
            alerts = check_services(self.settings)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].summary, "当前状态：已失败（进程异常退出）。")
        self.assertNotIn("Result", alerts[0].summary)
        self.assertNotIn("exit-code", alerts[0].summary)

    def test_short_cpu_spike_does_not_alert(self):
        # 1-minute load over the line, 5-minute load well under it (a CI job).
        with patch("os.getloadavg", return_value=(9.0, 4.0, 3.0)), \
             patch("os.cpu_count", return_value=4), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            self.assertEqual(check_cpu_and_load(self.settings), [])

    def _services_with_states(self, first_state, show, later_state):
        calls = {"is_active": 0}

        def systemctl(cmd, **_kwargs):
            if cmd[1] == "show":
                return MagicMock(returncode=0, stdout=show)
            if cmd[2].endswith(".timer"):
                return MagicMock(returncode=3, stdout="inactive\n")
            if cmd[2] != "conveyor-test-bot":
                return MagicMock(returncode=0, stdout="active\n")
            calls["is_active"] += 1
            state = first_state if calls["is_active"] == 1 else later_state
            return MagicMock(returncode=0 if state == "active" else 3, stdout=state + "\n")

        with patch("shutil.which", return_value="/bin/systemctl"), \
             patch("subprocess.run", side_effect=systemctl), \
             patch("personal_tools.sentry.time.sleep") as sleep:
            alerts = check_services(self.settings)
        return alerts, sleep

    def test_service_caught_mid_restart_does_not_alert(self):
        for state in ("deactivating", "activating"):
            alerts, sleep = self._services_with_states(
                state, f"ActiveState={state}\nSubState=stop-sigterm\nResult=success\n", "active")
            self.assertEqual(alerts, [], state)
            sleep.assert_called_once()

    def test_service_still_restarting_after_wait_alerts(self):
        alerts, _ = self._services_with_states(
            "deactivating", "ActiveState=deactivating\nSubState=stop-sigterm\nResult=success\n", "deactivating")
        self.assertEqual([a.fingerprint for a in alerts], ["host.service.conveyor-test-bot"])

    def test_crash_loop_alerts_without_waiting(self):
        alerts, sleep = self._services_with_states(
            "activating", "ActiveState=activating\nSubState=auto-restart\nResult=exit-code\n", "active")
        self.assertEqual(len(alerts), 1)
        self.assertIn("等待自动重启", alerts[0].summary)
        sleep.assert_not_called()

    def test_unit_state_wording(self):
        from personal_tools.sentry import _describe_unit_state

        cases = [
            ("failed", "ActiveState=failed\nSubState=failed\nResult=exit-code", "已失败（进程异常退出）"),
            ("inactive", "ActiveState=inactive\nSubState=dead\nResult=success", "未运行（已停止）"),
            ("activating", "ActiveState=activating\nSubState=auto-restart\nResult=exit-code",
             "正在启动（等待自动重启，进程异常退出）"),
            ("failed", "ActiveState=failed\nSubState=failed\nResult=start-limit-hit",
             "已失败（重启次数过多，已停止重试）"),
            # `systemctl show` failed: only the is-active answer is known.
            ("inactive", "", "未运行"),
            # Values systemd may add later are shown as printed.
            ("failed", "ActiveState=failed\nSubState=brand-new\nResult=new-result", "已失败（new-result）"),
        ]
        for state, show, expected in cases:
            with self.subTest(show=show):
                self.assertEqual(_describe_unit_state(state, show), expected)

    def test_ci_failure_suggests_chinese_fix_command(self):
        settings = dataclasses.replace(
            self.settings,
            github_token="t",
            github_default_repo="owner/repo",
        )
        with patch(
            "personal_tools.github_tools.github_ci_counts",
            return_value=("main", {"total": 3, "success": 2, "failure": 1, "pending": 0}),
        ):
            alerts = check_github_ci(settings)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(
            alerts[0].suggested_actions[1],
            {"label": "启动自动排查与修复", "command": "/fix 排查并修复默认分支的构建失败"},
        )

    def test_check_github_ci_alerts_on_finished_failure(self):
        settings = dataclasses.replace(
            self.settings,
            github_token="t",
            github_default_repo="owner/repo",
        )
        with patch(
            "personal_tools.github_tools.github_ci_counts",
            return_value=("main", {"total": 3, "success": 2, "failure": 1, "pending": 0}),
        ) as mock_counts:
            alerts = check_github_ci(settings)
        mock_counts.assert_called_once_with(settings, "")
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert.source, "repo.ci")
        self.assertEqual(alert.severity, "critical")
        self.assertEqual(alert.title, "自动构建没有通过")
        self.assertEqual(alert.fingerprint, "repo.ci.owner/repo.failure")
        self.assertEqual(
            alert.summary.splitlines()[0],
            "仓库「owner/repo」main 分支最新一次自动构建失败（1/3 项检查失败）。",
        )
        self.assertEqual(
            alert.suggested_actions,
            [
                {"label": "查看构建状态", "command": "/github_ci"},
                {"label": "启动自动排查与修复", "command": "/fix 排查并修复默认分支的构建失败"},
            ],
        )

    def test_check_github_ci_skips_when_pending(self):
        settings = dataclasses.replace(
            self.settings,
            github_token="t",
            github_default_repo="owner/repo",
        )
        with patch(
            "personal_tools.github_tools.github_ci_counts",
            return_value=("main", {"total": 3, "success": 1, "failure": 1, "pending": 1}),
        ):
            alerts = check_github_ci(settings)
        self.assertEqual(alerts, [])

    def test_check_github_ci_missing_token_does_not_call_helper(self):
        settings = dataclasses.replace(
            self.settings,
            github_token=None,
            github_default_repo="owner/repo",
        )
        with patch("personal_tools.github_tools.github_ci_counts") as mock_counts:
            alerts = check_github_ci(settings)
        self.assertEqual(alerts, [])
        mock_counts.assert_not_called()

    def test_check_github_ci_uses_github_default_repo(self):
        settings = dataclasses.replace(
            self.settings,
            github_token="t",
            github_default_repo="acme/conveyor",
        )
        with patch(
            "personal_tools.github_tools.github_ci_counts",
            return_value=("master", {"total": 4, "success": 2, "failure": 2, "pending": 0}),
        ) as mock_counts:
            alerts = check_github_ci(settings)
        mock_counts.assert_called_once_with(settings, "")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].fingerprint, "repo.ci.acme/conveyor.failure")
        self.assertEqual(
            alerts[0].summary.splitlines()[0],
            "仓库「acme/conveyor」master 分支最新一次自动构建失败（2/4 项检查失败）。",
        )

    def test_check_log_error_spikes(self):
        with patch("shutil.which", return_value="/bin/journalctl"), \
             patch("subprocess.run") as mock_run:
            errors = "\n".join(f"error log line {i}" for i in range(10))
            mock_run.return_value = MagicMock(returncode=0, stdout=errors)
            alerts = check_log_error_spikes(self.settings)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].source, "host.logs")

    _SSH_NOISE_TEMPLATES = (
        "Nov 19 07:01:02 dogecoin sshd[12345]: error: kex_exchange_identification: Connection closed by remote host",
        "Nov 19 07:01:05 dogecoin sshd[12346]: error: maximum authentication attempts exceeded for invalid user test from 1.2.3.4 port 5555 ssh2 [preauth]",
        "Nov 19 07:01:09 dogecoin sshd[12347]: error: kex_exchange_identification: banner line contains invalid characters",
        "Nov 19 07:01:10 dogecoin sshd[12348]: fatal: userauth_pubkey: parse publickey packet: incomplete message [preauth]",
        "Nov 19 07:01:11 dogecoin sshd[12349]: error: send_error: write: Connection reset by peer",
    )

    def test_check_log_error_spikes_ignores_ssh_scanner_noise(self):
        stdout = "\n".join(
            self._SSH_NOISE_TEMPLATES[i % len(self._SSH_NOISE_TEMPLATES)] for i in range(25)
        )
        with patch("shutil.which", return_value="/bin/journalctl"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=stdout)
            alerts = check_log_error_spikes(self.settings)
        self.assertEqual(alerts, [])

    def test_check_log_error_spikes_mixed_noise_and_real_errors(self):
        noise_lines = [
            self._SSH_NOISE_TEMPLATES[i % len(self._SSH_NOISE_TEMPLATES)] for i in range(25)
        ]
        real_lines = [
            f"Nov 19 07:02:00 dogecoin python[999]: ERROR conveyor.web boom {n}"
            for n in range(1, 7)
        ]
        stdout = "\n".join(["-- Logs begin at ... --", *noise_lines, *real_lines])
        with patch("shutil.which", return_value="/bin/journalctl"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=stdout)
            alerts = check_log_error_spikes(self.settings)
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert.source, "host.logs")
        self.assertEqual(alert.title, "日志错误突增预警")
        self.assertEqual(alert.fingerprint, "host.logs.error_burst")
        first_line = alert.summary.splitlines()[0]
        self.assertIn("有 6 条错误日志", first_line)
        self.assertIn("python[999]: ERROR conveyor.web boom 6", first_line)
        self.assertNotIn("Nov 19 07:02:00 dogecoin", first_line)
        self.assertIn("最新错误样例：", alert.summary)
        argv = mock_run.call_args[0][0]
        self.assertIn("-n", argv)
        self.assertEqual(argv[argv.index("-n") + 1], "200")

    def test_is_ssh_scanner_noise_keeps_real_sshd_errors(self):
        real_sshd = "sshd[1]: error: Bind to port 22 on 0.0.0.0 failed: Address already in use."
        self.assertFalse(_is_ssh_scanner_noise(real_sshd))
        self.assertTrue(
            _is_ssh_scanner_noise(
                "Nov 19 07:01:11 dogecoin sshd: error: send_error: write: Connection reset by peer"
            )
        )
        stdout = "\n".join(real_sshd for _ in range(5))
        with patch("shutil.which", return_value="/bin/journalctl"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=stdout)
            alerts = check_log_error_spikes(self.settings)
        self.assertEqual(len(alerts), 1)
        self.assertIn("有 5 条错误日志", alerts[0].summary.splitlines()[0])

    def test_check_log_error_spikes_truncates_first_line_sample(self):
        long_msg = "x" * 200
        stdout = "\n".join(
            f"Nov 19 07:02:00 dogecoin python[999]: {long_msg}" for _ in range(5)
        )
        with patch("shutil.which", return_value="/bin/journalctl"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=stdout)
            alerts = check_log_error_spikes(self.settings)
        self.assertEqual(len(alerts), 1)
        first_line = alerts[0].summary.splitlines()[0]
        prefix, _, sample = first_line.partition("最新一条：")
        self.assertTrue(prefix)
        self.assertTrue(sample.endswith("…"))
        self.assertLessEqual(len(sample), 160)

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
        self.assertIn("*（来源：CPU 负载 host.cpu · 发送 `/teammate pause` 可暂停提醒）*", text)
        unknown = dataclasses.replace(alert, source="host.other")
        self.assertIn("（来源：host.other ·", format_alert_text(unknown))

        sent = deliver_sentry_alerts(self.settings, [alert], dry_run=True)
        self.assertEqual(sent, 1)

    def test_teammate_commands(self):
        status = run_teammate_command(self.settings, "")
        self.assertIn("全天候智能体队友", status)
        self.assertIn("运行状态: 🟢 巡检中", status)
        for english in ("Always-On", "(Active)", "(Paused)"):
            self.assertNotIn(english, status)

        pause_res = run_teammate_command(self.settings, "pause 5")
        self.assertIn("5 小时", pause_res)
        self.assertIn("运行状态: ⏸️ 已暂停\n", run_teammate_command(self.settings, ""))

        resume_res = run_teammate_command(self.settings, "resume")
        self.assertIn("已恢复", resume_res)

        mute_res = run_teammate_command(self.settings, "mute host.disk")
        self.assertIn("已静音", mute_res)

        unmute_res = run_teammate_command(self.settings, "unmute host.disk")
        self.assertIn("已取消静音", unmute_res)

        check_res = run_teammate_command(self.settings, "check")
        self.assertIn("实时系统巡检完成", check_res)

    def test_check_and_status_list_alerts_in_chinese(self):
        alert = SentryAlert(
            source="host.disk", severity="warning", title="磁盘空间偏低 (/)",
            summary="可用空间不足", fingerprint="disk.fp",
        )
        for name in ("check_cpu_and_load", "check_services", "check_log_error_spikes",
                     "check_repo_git", "check_github_ci"):
            quiet = patch(f"personal_tools.sentry.{name}", return_value=[])
            quiet.start()
            self.addCleanup(quiet.stop)
        with patch("personal_tools.sentry.check_disk_space", return_value=[alert]):
            check_res = run_teammate_command(self.settings, "check")
            self.assertIn("• **磁盘空间偏低 (/)**（警告）\n  可用空间不足", check_res)
            run_sentry_patrol(self.settings, force=True)

        self.assertIn("  - [磁盘空间] 磁盘空间偏低 (/)", run_teammate_command(self.settings, "pulse evening"))
        self.assertIn("] 磁盘空间偏低 (/)：可用空间不足", teammate_status_text(self.settings))
        # An entry saved without a title still reads as Chinese.
        state = SentryState.load(self.settings)
        state.recent_alerts[0].pop("title")
        state.save(self.settings)
        self.assertIn("] 告警：可用空间不足", teammate_status_text(self.settings))

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
