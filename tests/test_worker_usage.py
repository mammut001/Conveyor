"""Usage-based rotation: usage readers (recorded samples, no network), thresholds, API."""
from __future__ import annotations

import asyncio
import http.client
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from conveyor.agents import worker_usage
from conveyor.agents import workers
from handlers.job_queue import JobQueue
from tests.test_workers import TOKEN, _make_settings
from web_console import WebConsoleHandler, WebConsoleServer
from conveyor.web.web_control import WebControl

NOW = 1_800_000_000.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- recorded sample responses (shape of the real provider answers) ----

GROK_SAMPLE = {
    "config": {
        "isUnifiedBillingUser": True,
        "creditUsagePercent": 42.5,
        "currentPeriod": {"start": iso(NOW - 3 * 86400), "end": iso(NOW + 4 * 86400)},
    },
    "subscriptionTier": "SuperGrok",
}

CODEX_SAMPLE = {
    "plan_type": "plus",
    "rate_limit": {
        "primary_window": {"used_percent": 61, "limit_window_seconds": 18000, "reset_after_seconds": 3600},
        "secondary_window": {"used_percent": 23.4, "limit_window_seconds": 604800, "reset_at": NOW + 5 * 86400},
    },
}

CLAUDE_SAMPLE = {
    "five_hour": {"utilization": 97.0, "resets_at": iso(NOW + 1800)},
    "seven_day": {"utilization": 40.0, "resets_at": iso(NOW + 2 * 86400)},
}

AGY_SAMPLE = {
    "groups": [
        {
            "displayName": "Gemini models",
            "buckets": [
                {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.25, "resetTime": iso(NOW + 7200)},
                {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 0.9,
                 "resetTime": iso(NOW + 3 * 86400)},
            ],
        },
        {
            "displayName": "Claude and GPT",
            "buckets": [{"bucketId": "3p", "window": "weekly", "remainingFraction": 0.1,
                         "resetTime": iso(NOW + 86400)}],
        },
    ]
}

PS_LINUX = (
    "    1 /sbin/init\n"
    " 2210 /usr/bin/python3 app.py\n"
    " 4321 /home/u/.antigravity/bin/language_server_linux_x64 --csrf_token=abc123tok "
    "--extension_server_port 38211 --workspace_id x\n"
)
PS_MAC = (
    "  501 /Applications/Antigravity.app/Contents/Resources/app/extensions/antigravity/bin/"
    "language_server_macos_arm --csrf_token deadbeef --extension_server_port 50123\n"
)
SS_OUTPUT = (
    'LISTEN 0 4096 127.0.0.1:38211 0.0.0.0:* users:(("language_server",pid=4321,fd=9))\n'
    'LISTEN 0 4096 127.0.0.1:41999 0.0.0.0:* users:(("language_server",pid=4321,fd=11))\n'
    'LISTEN 0 4096 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=900,fd=3))\n'
)
LSOF_OUTPUT = (
    "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
    "language_ 501 u 10u IPv4 0x1 0t0 TCP 127.0.0.1:50999 (LISTEN)\n"
    "language_ 501 u 11u IPv4 0x2 0t0 TCP 127.0.0.1:51000 (LISTEN)\n"
)


def win(name: str, used: float, resets_at: float | None) -> dict[str, Any]:
    return {"name": name, "used_percent": used, "resets_at": resets_at}


def by_name(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {w["name"]: w for w in result["windows"]}


class ParserTests(unittest.TestCase):
    def test_grok_weekly(self) -> None:
        result = worker_usage.parse_grok_usage(GROK_SAMPLE, NOW)
        self.assertEqual(result["plan"], "SuperGrok")
        self.assertEqual(len(result["windows"]), 1)
        weekly = result["windows"][0]
        self.assertEqual(weekly["name"], "weekly")
        self.assertEqual(weekly["used_percent"], 42.5)
        self.assertEqual(weekly["resets_at"], int(NOW + 4 * 86400))
        self.assertEqual(result["fetched_at"], int(NOW))

    def test_grok_zero_percent_is_omitted_by_protobuf_json(self) -> None:
        data = {"config": {"isUnifiedBillingUser": True,
                           "currentPeriod": {"start": iso(NOW - 100), "end": iso(NOW + 86400)}}}
        result = worker_usage.parse_grok_usage(data, NOW)
        self.assertEqual(result["windows"][0]["used_percent"], 0.0)

    def test_grok_expired_period_has_no_window(self) -> None:
        data = {"config": {"creditUsagePercent": 99, "currentPeriod": {"end": iso(NOW - 10)}}}
        self.assertIsNone(worker_usage.parse_grok_usage(data, NOW))

    def test_grok_garbage(self) -> None:
        self.assertIsNone(worker_usage.parse_grok_usage({"config": "x"}, NOW))
        self.assertIsNone(worker_usage.parse_grok_usage(None, NOW))

    def test_codex_two_windows(self) -> None:
        result = worker_usage.parse_codex_usage(CODEX_SAMPLE, NOW)
        windows = by_name(result)
        self.assertEqual(result["plan"], "plus")
        self.assertEqual(windows["5h"]["used_percent"], 61.0)
        self.assertEqual(windows["5h"]["resets_at"], int(NOW + 3600))
        self.assertEqual(windows["weekly"]["used_percent"], 23.4)
        self.assertEqual(windows["weekly"]["resets_at"], int(NOW + 5 * 86400))

    def test_codex_missing_rate_limit(self) -> None:
        self.assertIsNone(worker_usage.parse_codex_usage({"plan_type": "plus"}, NOW))

    def test_claude_two_windows(self) -> None:
        result = worker_usage.parse_claude_usage(CLAUDE_SAMPLE, NOW)
        windows = by_name(result)
        self.assertEqual(windows["5h"]["used_percent"], 97.0)
        self.assertEqual(windows["5h"]["resets_at"], int(NOW + 1800))
        self.assertEqual(windows["weekly"]["used_percent"], 40.0)

    def test_claude_past_five_hour_window_is_dropped(self) -> None:
        data = {"five_hour": {"utilization": 99.0, "resets_at": iso(NOW - 60)},
                "seven_day": {"utilization": 10.0, "resets_at": iso(NOW + 86400)}}
        windows = by_name(worker_usage.parse_claude_usage(data, NOW))
        self.assertNotIn("5h", windows)
        self.assertEqual(windows["weekly"]["used_percent"], 10.0)

    def test_antigravity_gemini_windows_only(self) -> None:
        result = worker_usage.parse_antigravity_summary(AGY_SAMPLE, NOW, "Pro")
        windows = by_name(result)
        self.assertEqual(result["plan"], "Pro")
        self.assertEqual(windows["5h"]["used_percent"], 75.0)
        self.assertEqual(windows["weekly"]["used_percent"], 10.0)
        self.assertEqual(len(result["windows"]), 2)

    def test_antigravity_garbage(self) -> None:
        self.assertIsNone(worker_usage.parse_antigravity_summary({"nope": 1}, NOW))


class FetcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        p = patch.object(worker_usage, "_home", return_value=self.home)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, rel: str, data: Any) -> None:
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_grok_fetch_uses_fixed_url_and_token_header(self) -> None:
        self._write(".grok/auth.json", {"x": {"key": "grok-secret-token"}})
        with patch.object(worker_usage, "_http_get_json", return_value=GROK_SAMPLE) as http:
            result = worker_usage.fetch_grok_usage(NOW)
        args = http.call_args
        self.assertEqual(args.args[0], "https://cli-chat-proxy.grok.com/v1/billing?format=credits")
        self.assertEqual(args.args[1], "cli-chat-proxy.grok.com")
        self.assertEqual(args.args[3], {"Authorization": "Bearer grok-secret-token"})
        self.assertNotIn("grok-secret-token", json.dumps(args.args[2]))
        self.assertEqual(result["windows"][0]["used_percent"], 42.5)

    def test_grok_no_credentials_or_expired_token(self) -> None:
        with patch.object(worker_usage, "_http_get_json") as http:
            self.assertIsNone(worker_usage.fetch_grok_usage(NOW))
            http.assert_not_called()
        self._write(".grok/auth.json", {"x": {"key": "t", "expires_at": iso(time.time() - 100)}})
        with patch.object(worker_usage, "_http_get_json") as http:
            self.assertIsNone(worker_usage.fetch_grok_usage(NOW))
            http.assert_not_called()

    def test_grok_network_failure_returns_none(self) -> None:
        self._write(".grok/auth.json", {"x": {"key": "t"}})
        with patch.object(worker_usage, "_http_get_json", side_effect=urllib.error.URLError("boom")):
            self.assertIsNone(worker_usage.fetch_grok_usage(NOW))

    def test_codex_fetch(self) -> None:
        self._write(".codex/auth.json", {"tokens": {"access_token": "cx-tok", "account_id": "acct-1"}})
        with patch.object(worker_usage, "_http_get_json", return_value=CODEX_SAMPLE) as http:
            result = worker_usage.fetch_codex_usage(NOW)
        self.assertEqual(http.call_args.args[1], "chatgpt.com")
        self.assertEqual(
            http.call_args.args[3],
            {"Authorization": "Bearer cx-tok", "ChatGPT-Account-Id": "acct-1"},
        )
        self.assertEqual(by_name(result)["5h"]["used_percent"], 61.0)

    def test_claude_linux_credentials_file(self) -> None:
        self._write(".claude/.credentials.json",
                    {"claudeAiOauth": {"accessToken": "cl-tok", "expiresAt": (time.time() + 3600) * 1000}})
        with patch.object(worker_usage.sys, "platform", "linux"), \
             patch.object(worker_usage, "_run_text", side_effect=AssertionError("no keychain on linux")), \
             patch.object(worker_usage, "_http_get_json", return_value=CLAUDE_SAMPLE) as http:
            result = worker_usage.fetch_claude_usage(NOW)
        self.assertEqual(http.call_args.args[1], "api.anthropic.com")
        self.assertEqual(http.call_args.args[3], {"Authorization": "Bearer cl-tok"})
        self.assertEqual(by_name(result)["5h"]["used_percent"], 97.0)

    def test_claude_macos_keychain(self) -> None:
        blob = json.dumps({"claudeAiOauth": {"accessToken": "kc-tok", "expiresAt": (time.time() + 3600) * 1000}})
        with patch.object(worker_usage.sys, "platform", "darwin"), \
             patch.object(worker_usage, "_run_text", return_value=blob) as run, \
             patch.object(worker_usage, "_http_get_json", return_value=CLAUDE_SAMPLE) as http:
            worker_usage.fetch_claude_usage(NOW)
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/security")
        self.assertEqual(http.call_args.args[3], {"Authorization": "Bearer kc-tok"})

    def test_claude_expired_token_falls_back_to_cached_config(self) -> None:
        self._write(".claude/.credentials.json",
                    {"claudeAiOauth": {"accessToken": "old", "expiresAt": (time.time() - 3600) * 1000}})
        self._write(".claude.json", {"cachedUsageUtilization": {"utilization": {
            "five_hour": {"utilization": 12.0, "resets_at": iso(NOW + 100)},
            "seven_day": {"utilization": 5.0, "resets_at": iso(NOW + 86400)},
        }}})
        with patch.object(worker_usage.sys, "platform", "linux"), \
             patch.object(worker_usage, "_http_get_json") as http:
            result = worker_usage.fetch_claude_usage(NOW)
            http.assert_not_called()
        self.assertEqual(by_name(result)["5h"]["used_percent"], 12.0)

    def test_http_guard_refuses_other_hosts(self) -> None:
        with self.assertRaises(ValueError):
            worker_usage._http_get_json("https://evil.example.com/x", "cli-chat-proxy.grok.com", {}, {})
        with self.assertRaises(ValueError):
            worker_usage._http_get_json("http://cli-chat-proxy.grok.com/x", "cli-chat-proxy.grok.com", {}, {})

    def test_redirect_to_other_host_is_refused(self) -> None:
        handler = worker_usage._SameHostRedirect()
        req = MagicMock()
        req.full_url = "https://chatgpt.com/backend-api/wham/usage"
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/steal")
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://chatgpt.com/downgrade")


class AntigravityDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        for name in ("ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_LS_ADDRESS"):
            patcher = patch.dict("os.environ", {}, clear=False)
            patcher.start()
            self.addCleanup(patcher.stop)
            import os
            os.environ.pop(name, None)

    def test_linux_process_discovery_uses_ps_eo(self) -> None:
        with patch.object(worker_usage.sys, "platform", "linux"), \
             patch.object(worker_usage, "_run_text", return_value=PS_LINUX) as run:
            procs = worker_usage.find_antigravity_processes()
        self.assertEqual(run.call_args.args[0], ["ps", "-eo", "pid=,args="])
        self.assertEqual(procs, [{"pid": 4321, "csrf_token": "abc123tok", "port": 38211}])

    def test_macos_process_discovery_uses_bin_ps(self) -> None:
        with patch.object(worker_usage.sys, "platform", "darwin"), \
             patch.object(worker_usage, "_run_text", return_value=PS_MAC) as run:
            procs = worker_usage.find_antigravity_processes()
        self.assertEqual(run.call_args.args[0], ["/bin/ps", "-ax", "-o", "pid=,command="])
        self.assertEqual(procs, [{"pid": 501, "csrf_token": "deadbeef", "port": 50123}])

    def test_linux_ports_from_ss(self) -> None:
        with patch.object(worker_usage.sys, "platform", "linux"), \
             patch.object(worker_usage, "_run_text", return_value=SS_OUTPUT) as run:
            ports = worker_usage.antigravity_ports(4321, 38211)
        self.assertEqual(run.call_args.args[0][0], "ss")
        self.assertEqual(ports, [38211, 41999])

    def test_linux_ports_fall_back_to_proc_when_ss_missing(self) -> None:
        with patch.object(worker_usage.sys, "platform", "linux"), \
             patch.object(worker_usage, "_run_text", return_value=""), \
             patch.object(worker_usage, "_ports_from_proc", return_value=[42000]) as proc:
            ports = worker_usage.antigravity_ports(4321, None)
        proc.assert_called_once_with(4321)
        self.assertEqual(ports, [42000])

    def test_macos_ports_from_lsof(self) -> None:
        with patch.object(worker_usage.sys, "platform", "darwin"), \
             patch.object(worker_usage.os.path, "isfile", return_value=True), \
             patch.object(worker_usage.os, "access", return_value=True), \
             patch.object(worker_usage, "_run_text", return_value=LSOF_OUTPUT) as run:
            ports = worker_usage.antigravity_ports(501, 50123)
        self.assertEqual(run.call_args.args[0][0], "/usr/sbin/lsof")
        self.assertEqual(ports, [50123, 50999, 51000])

    def test_no_process_returns_none(self) -> None:
        with patch.object(worker_usage.sys, "platform", "linux"), \
             patch.object(worker_usage, "_run_text", return_value="    1 /sbin/init\n"), \
             patch.object(worker_usage, "_post_local_json", side_effect=AssertionError("no server")):
            self.assertIsNone(worker_usage.fetch_antigravity_usage(NOW))

    def test_fetch_talks_only_to_loopback_and_parses(self) -> None:
        calls: list[tuple] = []

        def fake_post(scheme, port, path, body, csrf, timeout):
            calls.append((scheme, port, path, csrf))
            if path.endswith("RetrieveUserQuotaSummary"):
                return AGY_SAMPLE
            return {"userTier": {"name": "Google AI Pro"}}

        with patch.object(worker_usage.sys, "platform", "linux"), \
             patch.object(worker_usage, "_run_text", side_effect=[PS_LINUX, SS_OUTPUT]), \
             patch.object(worker_usage, "_post_local_json", side_effect=fake_post):
            result = worker_usage.fetch_antigravity_usage(NOW)
        self.assertEqual(result["plan"], "Google AI Pro")
        self.assertEqual(by_name(result)["5h"]["used_percent"], 75.0)
        self.assertEqual(calls[0][:2], ("http", 38211))
        self.assertEqual(calls[0][3], "abc123tok")

    def test_server_error_everywhere_returns_none(self) -> None:
        with patch.object(worker_usage.sys, "platform", "linux"), \
             patch.object(worker_usage, "_run_text", side_effect=[PS_LINUX, SS_OUTPUT]), \
             patch.object(worker_usage, "_post_local_json", side_effect=OSError("refused")):
            self.assertIsNone(worker_usage.fetch_antigravity_usage(NOW))


class CacheAndRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        worker_usage.clear_cache()
        self.addCleanup(worker_usage.clear_cache)

    def test_get_usage_empty_means_unavailable(self) -> None:
        usage = worker_usage.get_usage("agy")
        self.assertEqual(usage, {"windows": [], "plan": None, "fetched_at": None, "available": False})

    def test_refresh_stores_and_failure_keeps_last_value(self) -> None:
        good = {"windows": [win("weekly", 50.0, int(time.time()) + 1000)], "plan": "x", "fetched_at": 123}
        with patch.object(worker_usage, "fetch_usage", return_value=good):
            worker_usage.refresh_worker("grok")
        self.assertTrue(worker_usage.get_usage("grok")["available"])
        with patch.object(worker_usage, "fetch_usage", return_value=None):
            worker_usage.refresh_worker("grok")
        kept = worker_usage.get_usage("grok")
        self.assertTrue(kept["available"])
        self.assertEqual(kept["windows"][0]["used_percent"], 50.0)
        self.assertEqual(kept["fetched_at"], 123)

    def test_refresh_stale_only_when_older_than_ten_minutes(self) -> None:
        with patch.object(worker_usage, "fetch_usage", return_value=None) as fetch:
            worker_usage.refresh_stale(["grok"])
            self.assertEqual(fetch.call_count, 1)
            worker_usage.refresh_stale(["grok"])  # attempt just happened, even though it failed
            self.assertEqual(fetch.call_count, 1)
            worker_usage._cache["grok"]["attempted_at"] = time.time() - 601
            worker_usage.refresh_stale(["grok"])
            self.assertEqual(fetch.call_count, 2)

    def test_unknown_worker_has_no_fetcher(self) -> None:
        self.assertIsNone(worker_usage.fetch_usage("pi"))
        self.assertIsNone(worker_usage.fetch_usage("cmd"))

    def test_fetcher_exception_never_escapes(self) -> None:
        with patch.dict(worker_usage.FETCHERS, {"grok": MagicMock(side_effect=RuntimeError("x"))}):
            self.assertIsNone(worker_usage.fetch_usage("grok"))

    def test_interval_minimum_300(self) -> None:
        self.assertEqual(worker_usage.normalize_interval(10), 300)
        self.assertEqual(worker_usage.normalize_interval(7200), 7200)
        self.assertEqual(worker_usage.normalize_interval("junk"), 3600)

    def test_background_refresh_off_when_pool_disabled(self) -> None:
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        settings = MagicMock(workers_enabled=False)
        self.assertIsNone(worker_usage.start_background_refresh(loop, settings))

    def test_background_refresh_runs_refresh_all(self) -> None:
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        called = threading.Event()
        settings = MagicMock(workers_enabled=True, workers_usage_interval_seconds=3600)
        with patch.object(worker_usage, "refresh_all", side_effect=called.set):
            future = worker_usage.start_background_refresh(loop, settings)
            self.assertTrue(called.wait(3))
            future.cancel()
            time.sleep(0.3)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
        loop.close()


def _detections(**overrides: Any) -> dict[str, dict[str, Any]]:
    base = {
        wid: {"installed": True, "version": "1", "path": f"/bin/{wid}", "auth": "ok"}
        for wid in ("codex", "claude-code", "pi", "agy", "cmd", "grok")
    }
    for wid, extra in overrides.items():
        base[wid.replace("_", "-")].update(extra)
    return base


def _seed_usage(wid: str, *windows: dict[str, Any]) -> None:
    worker_usage._cache[wid] = {
        "data": {"windows": list(windows), "plan": None, "fetched_at": int(time.time())},
        "attempted_at": time.time(),
    }


class ThresholdPickTests(unittest.TestCase):
    def setUp(self) -> None:
        worker_usage.clear_cache()
        workers.clear_detection_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = _make_settings(Path(self._tmp.name))
        p = patch("conveyor.agents.workers.detect_all_workers", return_value=_detections())
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(worker_usage.clear_cache)
        self.future = time.time() + 3600

    def test_defaults_in_payload(self) -> None:
        payload = workers.get_workers_payload(self.settings)
        codex = next(w for w in payload["workers"] if w["id"] == "codex")
        self.assertEqual(codex["threshold_percent"], 95)
        self.assertFalse(codex["over_threshold"])
        self.assertEqual(codex["usage"], {"windows": [], "plan": None, "fetched_at": None, "available": False})
        self.assertEqual(payload["switch_log"], [])
        self.assertEqual(payload["active"], "codex")

    def test_over_threshold_worker_is_skipped(self) -> None:
        _seed_usage("codex", win("5h", 96.0, self.future), win("weekly", 10.0, self.future))
        picked = workers.pick_worker(self.settings)
        self.assertEqual(picked["id"], "claude-code")
        payload = workers.get_workers_payload(self.settings)
        codex = next(w for w in payload["workers"] if w["id"] == "codex")
        self.assertTrue(codex["over_threshold"])
        self.assertEqual(payload["active"], "claude-code")

    def test_exactly_at_threshold_counts_as_over(self) -> None:
        _seed_usage("codex", win("weekly", 95.0, self.future))
        self.assertEqual(workers.pick_worker(self.settings)["id"], "claude-code")

    def test_below_threshold_keeps_first(self) -> None:
        _seed_usage("codex", win("weekly", 94.9, self.future))
        self.assertEqual(workers.pick_worker(self.settings)["id"], "codex")

    def test_window_past_its_reset_no_longer_blocks(self) -> None:
        _seed_usage("codex", win("5h", 99.0, time.time() - 5))
        self.assertEqual(workers.pick_worker(self.settings)["id"], "codex")

    def test_custom_threshold(self) -> None:
        workers.set_worker_threshold(self.settings, "codex", 50)
        _seed_usage("codex", win("weekly", 60.0, self.future))
        self.assertEqual(workers.pick_worker(self.settings)["id"], "claude-code")
        workers.set_worker_threshold(self.settings, "codex", 70)
        self.assertEqual(workers.pick_worker(self.settings)["id"], "codex")

    def test_all_over_threshold_uses_least_used(self) -> None:
        for wid in ("codex", "claude-code", "pi", "agy", "cmd", "grok"):
            workers.set_worker_threshold(self.settings, wid, 10)
        _seed_usage("codex", win("weekly", 90.0, self.future))
        _seed_usage("claude-code", win("5h", 30.0, self.future), win("weekly", 60.0, self.future))
        _seed_usage("agy", win("weekly", 40.0, self.future))
        _seed_usage("grok", win("weekly", 80.0, self.future))
        # pi and cmd have no usage -> they are not over threshold and win first
        workers.update_order_and_enabled(self.settings, ["codex", "claude-code", "agy", "grok", "pi", "cmd"],
                                         ["codex", "claude-code", "agy", "grok"])
        picked = workers.pick_worker(self.settings)
        self.assertEqual(picked["id"], "agy")  # max(40) < max(60) < max(80) < max(90)

    def test_all_over_ties_keep_owner_order(self) -> None:
        for wid in ("codex", "claude-code"):
            workers.set_worker_threshold(self.settings, wid, 10)
            _seed_usage(wid, win("weekly", 50.0, self.future))
        workers.update_order_and_enabled(self.settings, ["claude-code", "codex"], ["claude-code", "codex"])
        self.assertEqual(workers.pick_worker(self.settings)["id"], "claude-code")

    def test_cooling_worker_still_skipped_when_all_over(self) -> None:
        for wid in ("codex", "claude-code"):
            workers.set_worker_threshold(self.settings, wid, 10)
        _seed_usage("codex", win("weekly", 20.0, self.future))
        _seed_usage("claude-code", win("weekly", 50.0, self.future))
        workers.update_order_and_enabled(self.settings, ["codex", "claude-code"], ["codex", "claude-code"])
        workers.mark_cooling(self.settings, "codex", reset_seconds=600)
        self.assertEqual(workers.pick_worker(self.settings)["id"], "claude-code")

    def test_threshold_validation(self) -> None:
        for bad in (0, 101, -5, 50.5, "80", None, True, [90]):
            with self.assertRaises(workers.BadThresholdError, msg=repr(bad)):
                workers.set_worker_threshold(self.settings, "codex", bad)
        with self.assertRaises(ValueError):
            workers.set_worker_threshold(self.settings, "nope", 50)
        payload = workers.set_worker_threshold(self.settings, "codex", 100)
        self.assertEqual(next(w for w in payload["workers"] if w["id"] == "codex")["threshold_percent"], 100)
        self.assertEqual(workers.load_workers_state(self.settings)["thresholds"], {"codex": 100})

    def test_threshold_survives_order_update(self) -> None:
        workers.set_worker_threshold(self.settings, "grok", 80)
        workers.update_order_and_enabled(self.settings, ["grok", "codex"], ["grok", "codex"])
        self.assertEqual(workers.load_workers_state(self.settings)["thresholds"], {"grok": 80})

    def test_switch_log_records_threshold_switch_once(self) -> None:
        self.assertEqual(workers.pick_worker(self.settings)["id"], "codex")
        log = workers.load_workers_state(self.settings)["switch_log"]
        self.assertEqual(len(log), 1)
        self.assertEqual((log[0]["from"], log[0]["to"], log[0]["reason"]), (None, "codex", "unavailable"))

        # same pick again: no new entry
        workers.pick_worker(self.settings)
        self.assertEqual(len(workers.load_workers_state(self.settings)["switch_log"]), 1)

        _seed_usage("codex", win("5h", 97.0, self.future))
        self.assertEqual(workers.pick_worker(self.settings)["id"], "claude-code")
        log = workers.load_workers_state(self.settings)["switch_log"]
        self.assertEqual(len(log), 2)
        entry = log[-1]
        self.assertEqual((entry["from"], entry["to"], entry["reason"]), ("codex", "claude-code", "threshold"))
        self.assertIn("97%", entry["detail"])
        self.assertIsInstance(entry["at"], int)

    def test_switch_log_quota_error_and_unavailable(self) -> None:
        workers.pick_worker(self.settings)
        workers.mark_cooling(self.settings, "codex", reset_seconds=600)
        workers.pick_worker(self.settings)
        self.assertEqual(workers.load_workers_state(self.settings)["switch_log"][-1]["reason"], "quota_error")

        workers.reset_quota(self.settings, "codex")
        workers.pick_worker(self.settings)  # back to codex, log: claude-code -> codex
        workers.update_order_and_enabled(self.settings, ["codex", "claude-code"], ["claude-code"])
        workers.pick_worker(self.settings)  # codex disabled
        last = workers.load_workers_state(self.settings)["switch_log"][-1]
        self.assertEqual((last["from"], last["to"], last["reason"]), ("codex", "claude-code", "unavailable"))

    def test_payload_does_not_log_but_pick_does(self) -> None:
        workers.get_workers_payload(self.settings)
        self.assertEqual(workers.load_workers_state(self.settings)["switch_log"], [])

    def test_switch_log_capped_at_20_and_payload_returns_last_20(self) -> None:
        for i in range(30):
            workers.record_switch(self.settings, "codex", "claude-code", "threshold", f"n{i}")
        log = workers.load_workers_state(self.settings)["switch_log"]
        self.assertEqual(len(log), 20)
        self.assertEqual(log[-1]["detail"], "n29")
        self.assertEqual(log[0]["detail"], "n10")
        payload = workers.get_workers_payload(self.settings)
        self.assertEqual(len(payload["switch_log"]), 20)

    def test_async_pick_refreshes_stale_usage_first(self) -> None:
        with patch.object(worker_usage, "fetch_usage", return_value=None) as fetch:
            picked = asyncio.run(workers.async_pick_worker(self.settings))
        self.assertEqual(picked["id"], "codex")
        self.assertGreaterEqual(fetch.call_count, 1)


class UsageApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._detect = patch("conveyor.agents.workers.detect_all_workers", side_effect=lambda force=False: _detections())
        cls._detect.start()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.settings = _make_settings(Path(cls._tmp.name), workers_enabled=True, workers_install_enabled=True)
        cls.loop = asyncio.new_event_loop()
        cls.loop_thread = threading.Thread(target=cls.loop.run_forever, daemon=True)
        cls.loop_thread.start()
        cls.queue = JobQueue()
        cls.queue.configure(cls.settings, runner=None, recover=False)
        control = WebControl(cls.settings, runner=None, queue=cls.queue)
        cls.server = WebConsoleServer(("127.0.0.1", 0), WebConsoleHandler, control=control, loop=cls.loop, token=TOKEN)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.loop_thread.join(timeout=2)
        cls.server_thread.join(timeout=2)
        cls._tmp.cleanup()
        cls._detect.stop()
        worker_usage.clear_cache()

    def setUp(self) -> None:
        worker_usage.clear_cache()

    def request(self, method: str, path: str, body: Any = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"}
        data = json.dumps(body).encode("utf-8") if body is not None else None
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        payload = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, payload

    def test_threshold_api(self) -> None:
        status, body = self.request("POST", "/api/workers/codex/threshold", {"threshold_percent": 80})
        self.assertEqual(status, 200)
        self.assertEqual(next(w for w in body["workers"] if w["id"] == "codex")["threshold_percent"], 80)

        for bad in (0, 101, "90", 12.5, None, True):
            status, body = self.request("POST", "/api/workers/codex/threshold", {"threshold_percent": bad})
            self.assertEqual(status, 400, msg=repr(bad))
            self.assertEqual(body["code"], "bad_threshold")
        status, body = self.request("POST", "/api/workers/codex/threshold", {})
        self.assertEqual((status, body["code"]), (400, "bad_threshold"))
        status, body = self.request("POST", "/api/workers/nope/threshold", {"threshold_percent": 50})
        self.assertEqual((status, body["code"]), (400, "unknown_worker"))

        # a rejected value leaves the saved one untouched
        status, body = self.request("GET", "/api/workers")
        self.assertEqual(next(w for w in body["workers"] if w["id"] == "codex")["threshold_percent"], 80)
        self.request("POST", "/api/workers/codex/threshold", {"threshold_percent": 95})

    def test_usage_refresh_api(self) -> None:
        sample = {"windows": [win("weekly", 97.0, int(time.time()) + 5000)], "plan": "Plus",
                  "fetched_at": int(time.time())}

        def fake_fetch(wid, now=None):
            return sample if wid == "codex" else None

        with patch.object(worker_usage, "fetch_usage", side_effect=fake_fetch) as fetch:
            status, body = self.request("POST", "/api/workers/usage/refresh", {})
        self.assertEqual(status, 200)
        self.assertEqual({c.args[0] for c in fetch.call_args_list}, set(worker_usage.USAGE_WORKER_IDS))
        codex = next(w for w in body["workers"] if w["id"] == "codex")
        self.assertTrue(codex["usage"]["available"])
        self.assertEqual(codex["usage"]["plan"], "Plus")
        self.assertTrue(codex["over_threshold"])
        agy = next(w for w in body["workers"] if w["id"] == "agy")
        self.assertFalse(agy["usage"]["available"])
        self.assertEqual(body["active"], "claude-code")
        self.assertIn("switch_log", body)

    def test_get_payload_has_new_fields_and_flag_off_404(self) -> None:
        status, body = self.request("GET", "/api/workers")
        self.assertEqual(status, 200)
        self.assertIn("grok", [w["id"] for w in body["workers"]])
        for w in body["workers"]:
            self.assertIn("usage", w)
            self.assertIn("threshold_percent", w)
            self.assertIn("over_threshold", w)
        self.settings.workers_enabled = False
        try:
            for method, path, payload in (
                ("POST", "/api/workers/usage/refresh", {}),
                ("POST", "/api/workers/codex/threshold", {"threshold_percent": 50}),
            ):
                status, _ = self.request(method, path, payload)
                self.assertEqual(status, 404)
        finally:
            self.settings.workers_enabled = True


class GrokDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        workers.clear_detection_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(workers.clear_detection_cache)

    def _detect(self, auth_file: bool, rc: int) -> str:
        home = Path(self._tmp.name)
        if auth_file:
            (home / ".grok").mkdir(exist_ok=True)
            (home / ".grok" / "auth.json").write_text("{}")
        version = MagicMock(returncode=0, stdout="grok 1.0\n", stderr="")
        models = MagicMock(returncode=rc, stdout="grok-4\n", stderr="")
        with patch("conveyor.agents.workers.shutil.which", return_value="/bin/grok"), \
             patch("conveyor.agents.workers.os.path.expanduser", return_value=str(home)), \
             patch("conveyor.agents.workers.subprocess.run", side_effect=[version, models]):
            return workers.detect_worker(workers.get_catalog_worker("grok"))["auth"]

    def test_auth_needs_file_and_models_ok(self) -> None:
        self.assertEqual(self._detect(True, 0), "ok")

    def test_missing_file_or_failing_models(self) -> None:
        self.assertEqual(self._detect(False, 0), "needs_login")
        self.assertEqual(self._detect(True, 1), "needs_login")


if __name__ == "__main__":
    unittest.main()
