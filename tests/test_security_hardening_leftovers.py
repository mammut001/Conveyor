"""Focused opt-in hardening regressions: egress, Feishu demuxer, login leases."""
from __future__ import annotations

import asyncio
import ipaddress
import os
from pathlib import Path
import socket
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import egress_host_rules
import egress_proxy
from browser_login_domain_gate import login_domain_busy, reserve_login_domain
from workflow_browser_external_identity import ExternalPolicyDenied
from channel.feishu_audio import _convert_opus
from channel.feishu_ffmpeg_policy import opus_to_ogg_command


class EgressPortPolicyTests(unittest.TestCase):
    def setUp(self):
        self.dns_calls = []

    def resolve(self, host, port, **_kw):
        self.dns_calls.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

    def test_trailing_root_dot_and_empty_labels(self):
        self.assertEqual(egress_host_rules.canonical_host("EXAMPLE.COM."),
                         "example.com")
        self.assertEqual(egress_proxy.destination("EXAMPLE.COM.", 443,
                         ("example.com",), resolver=self.resolve),
                         ("8.8.8.8", "allowlisted"))
        self.assertEqual(self.dns_calls, [("example.com", 443)])
        for name in ("example.com..", "a..example.com", "..example.com",
                     "example..", ".", "a...b"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    egress_host_rules.canonical_host(name)
                self.assertEqual(egress_proxy.destination(name, 443,
                                 ("example.com",), resolver=self.resolve),
                                 (None, "dns_or_host_error"))
        self.assertEqual(self.dns_calls, [("example.com", 443)])

    def test_default_443_explicit_80_and_nonstandard_port(self):
        for p in (80, 8080, 8443):
            with self.subTest(port=p):
                self.assertEqual(egress_proxy.destination("example.com", p,
                                 ("example.com",), resolver=self.resolve),
                                 (None, "not_allowlisted"))
        self.assertEqual(self.dns_calls, [])
        self.assertEqual(egress_proxy.destination("example.com", 80,
                         ("example.com:80",), resolver=self.resolve),
                         ("8.8.8.8", "allowlisted"))
        self.assertEqual(egress_proxy.destination("example.com", 8443,
                         ("example.com:8443",), resolver=self.resolve),
                         ("8.8.8.8", "allowlisted"))
        self.assertEqual(egress_proxy.destination("example.com", 443,
                         ("example.com:80",), resolver=self.resolve),
                         (None, "not_allowlisted"))
        self.assertEqual(self.dns_calls, [("example.com", 80),
                                          ("example.com", 8443)])

    def test_wildcard_ports_and_normalization(self):
        rules = egress_host_rules.normalize_rules(
            " EXAMPLE.com. , *.Example.com.:80, EXAMPLE.com:443")
        self.assertEqual(rules, ("*.example.com:80", "example.com"))
        self.assertEqual(egress_proxy.destination("a.example.com", 80,
                         rules, resolver=self.resolve),
                         ("8.8.8.8", "allowlisted"))
        self.assertEqual(egress_proxy.destination("example.com", 80,
                         rules, resolver=self.resolve)[1], "not_allowlisted")
        self.assertEqual(egress_proxy.destination("a.example.com", 443,
                         rules, resolver=self.resolve)[1], "not_allowlisted")

    def test_invalid_rules_and_ipv6_authority(self):
        for raw in ("example.com..", "example..com:80", "*.example.com..:8443",
                    "example.com:0", "example.com:65536", "example.com:x",
                    "example.com:", "*..example.com"):
            with self.subTest(rule=raw), self.assertRaises(ValueError):
                egress_host_rules.normalize_rules(raw)
        rule = egress_host_rules.normalize_rules("[2606:4700:4700::1111]:8443")
        self.assertEqual(rule, ("[2606:4700:4700::1111]:8443",))
        self.assertEqual(egress_host_rules.match("2606:4700:4700::1111", 8443,
                         rule), (True, True))

    def test_proxy_disabled_does_not_change_environment(self):
        with patch.dict(os.environ, {"CONVEYOR_EGRESS_PROXY": "0",
                                     "CONVEYOR_EGRESS_ALLOWLIST": "example.com.."}):
            original = {"PATH": "/bin", "NO_PROXY": "*"}
            self.assertEqual(egress_proxy.proxy_env(original), original)


class FeishuFFmpegPolicyTests(unittest.TestCase):
    def test_no_auto_demuxer_network_or_concat_protocol(self):
        argv = opus_to_ogg_command(Path("/tmp/untrusted.opus"),
                                    Path("/tmp/result.ogg"))
        idx = argv.index("-i")
        self.assertEqual(argv[idx + 1], "/tmp/untrusted.opus")
        self.assertEqual(argv[idx - 4:idx],
                         ["-protocol_whitelist", "file,pipe", "-f", "opus"])
        self.assertEqual(argv[-3:], ["-f", "ogg", "/tmp/result.ogg"])
        self.assertNotIn("-f", argv[:idx - 4])
        self.assertNotIn("http,https", argv)

    def test_actual_converter_passes_restricted_argv_with_timeout(self):
        with tempfile.TemporaryDirectory() as root:
            src, dest = Path(root) / "input.opus", Path(root) / "out.ogg"
            src.write_bytes(b"untrusted-payload")
            def run(args, **kwargs):
                self.assertEqual(args, opus_to_ogg_command(src, dest))
                self.assertEqual(kwargs["timeout"], 45)
                self.assertTrue(kwargs["capture_output"])
                dest.write_bytes(b"OggS")
                return SimpleNamespace(returncode=0)
            with patch("channel.feishu_audio.shutil.which", return_value="/usr/bin/ffmpeg"), \
                 patch("channel.feishu_audio.subprocess.run", side_effect=run):
                _convert_opus(src, dest)
            self.assertTrue(dest.is_file())


class LoginDomainAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_open_busy_during_start_and_until_revoked(self):
        from browser_auth_sessions import AuthSession, manager_for
        origin = "http://127.0.0.1:18098"
        flags = {
            "CONVEYOR_REAL_BROWSER": "1",
            "CONVEYOR_BROWSER_EXTERNAL_READONLY": "1",
            "CONVEYOR_BROWSER_EXTERNAL_INTERACT": "1",
            "CONVEYOR_BROWSER_EXTERNAL_SUBMIT": "1",
            "CONVEYOR_TAKEOVER_ENABLED": "1",
            "CONVEYOR_BROWSER_AUTH_SESSIONS": "1",
            "CONVEYOR_BROWSER_AUTOFILL_LOGIN": "1",
            "CONVEYOR_BROWSER_AUTH_DOMAINS": origin,
            "CONVEYOR_BROWSER_AUTH_TEST_LOOPBACK": "1",
            "CONVEYOR_REQUIRE_REAL_BROWSER": "1",
        }
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, flags):
            os.environ.pop("DISPLAY", None)
            settings = SimpleNamespace(codex_memory_root=Path(root),
                                       conveyor_takeover_enabled=True)
            manager = manager_for(settings)
            entered = asyncio.Event()
            release = asyncio.Event()

            async def start(_session):
                entered.set()
                await asyncio.wait_for(release.wait(), 3)

            with patch.object(AuthSession, "start", start), \
                 patch.object(AuthSession, "close", new_callable=AsyncMock):
                first = asyncio.create_task(manager.open("op1", "task1", origin,
                                                          automated=True))
                await asyncio.wait_for(entered.wait(), 3)
                self.assertTrue(login_domain_busy(manager, origin))
                with self.assertRaisesRegex(ExternalPolicyDenied,
                                            "login_domain_busy"):
                    await asyncio.wait_for(manager.open("op2", "task2", origin,
                                                         automated=True), 3)
                release.set()
                session = await asyncio.wait_for(first, 3)
                with self.assertRaisesRegex(ExternalPolicyDenied,
                                            "login_domain_busy"):
                    await asyncio.wait_for(manager.open("op2", "task2", origin,
                                                         automated=True), 3)
                self.assertTrue(await asyncio.wait_for(manager.revoke(
                    session.id, operator="op1"), 3))
                self.assertFalse(login_domain_busy(manager, origin))
                other = await asyncio.wait_for(manager.open(
                    "op2", "task2", origin, automated=True), 3)
                self.assertTrue(await asyncio.wait_for(manager.revoke(
                    other.id, operator="op2"), 3))

    def test_closing_session_stays_busy_and_release_is_scoped(self):
        origin = "https://example.com"
        manager = SimpleNamespace(sessions={}, _closing_domains={"1": origin})
        self.assertTrue(login_domain_busy(manager, origin))
        with self.assertRaisesRegex(ExternalPolicyDenied, "login_domain_busy"):
            with reserve_login_domain(manager, origin):
                self.fail("domain gate allowed closing login")
        self.assertFalse(login_domain_busy(manager, "https://other.example"))
