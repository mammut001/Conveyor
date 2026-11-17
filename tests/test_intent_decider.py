"""Hermetic unit and HTTP tests for conveyor.agents.intent_decider."""
from __future__ import annotations

import http.client
import json
import logging
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from conveyor.agents import intent_decider
from conveyor.core import provider_config
from conveyor.web import console_secrets
from tests.test_supervisor_api import TOKEN, _HttpHarness, _make_settings


class _FakeDecisionServer:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.next_status = 200
        self.next_body: dict | str = {
            "answers": {
                "intent": {
                    "type": "choice",
                    "choice": "progress",
                    "confidence": 0.979,
                    "probabilities": {"progress": 0.979, "chat": 0.021},
                }
            },
            "usage": {"total_output_tokens": 0},
        }
        self.delay_seconds = 0.0

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                try:
                    parsed = json.loads(raw) if raw else None
                except Exception:
                    parsed = raw
                outer.requests.append({
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": parsed,
                })
                if outer.delay_seconds > 0:
                    time.sleep(outer.delay_seconds)
                payload = outer.next_body if isinstance(outer.next_body, str) else json.dumps(outer.next_body)
                try:
                    self.send_response(outer.next_status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(payload.encode("utf-8"))
                except BrokenPipeError:
                    pass

            def log_message(self, format: str, *args) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1beta/decisions"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class IntentDeciderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-decider-")
        self.root = Path(self._tmp.name)
        self.settings = _make_settings(self.root / "env")
        self.fake = _FakeDecisionServer()

    def tearDown(self) -> None:
        self.fake.close()
        self._tmp.cleanup()

    def test_auto_provider_choice_rules(self) -> None:
        # Neither key -> off
        cfg = intent_decider.get_intent_decider_config(self.settings)
        self.assertEqual(cfg["provider"], "off")
        self.assertFalse(cfg["ready"])

        # Only GEMINI_API_KEY -> auto-selects gemini
        console_secrets.set_secret(self.settings, "GEMINI_API_KEY", "secret-gem-123456")
        cfg = intent_decider.get_intent_decider_config(self.settings)
        self.assertEqual(cfg["provider"], "gemini")
        self.assertTrue(cfg["auto_selected"])
        self.assertTrue(cfg["ready"])

        # Both GEMINI_API_KEY and TYPESAFE_API_KEY -> off (ambiguous)
        console_secrets.set_secret(self.settings, "TYPESAFE_API_KEY", "secret-jev-123456")
        cfg = intent_decider.get_intent_decider_config(self.settings)
        self.assertEqual(cfg["provider"], "off")
        self.assertFalse(cfg["ready"])

        # Only TYPESAFE_API_KEY -> auto-selects jev, reports missing endpoint
        console_secrets.delete_secret(self.settings, "GEMINI_API_KEY")
        cfg = intent_decider.get_intent_decider_config(self.settings)
        self.assertEqual(cfg["provider"], "jev")
        self.assertTrue(cfg["auto_selected"])
        self.assertFalse(cfg["ready"])
        self.assertEqual(cfg["error"], "还没填 Jev 的接口地址")
        self.assertEqual(cfg["model"], "jev-latest")

        # Explicit off stays off even when only GEMINI_API_KEY exists
        console_secrets.delete_secret(self.settings, "TYPESAFE_API_KEY")
        console_secrets.set_secret(self.settings, "GEMINI_API_KEY", "secret-gem-123456")
        intent_decider.save_intent_decider_config(self.settings, {"provider": "off"})
        cfg = intent_decider.get_intent_decider_config(self.settings)
        self.assertEqual(cfg["provider"], "off")
        self.assertFalse(cfg["auto_selected"])

    def test_gemini_success_criteria_object_and_key_header(self) -> None:
        secret = "AIzaSySecretDoNotLeak9876"
        console_secrets.set_secret(self.settings, "GEMINI_API_KEY", secret)
        with patch.object(intent_decider, "GEMINI_ENDPOINT", self.fake.url):
            dec = intent_decider.decide_intent(self.settings, "进度怎么样了", intent_decider.INTENT_OPTIONS)

        self.assertIsNotNone(dec)
        assert dec is not None
        self.assertEqual(dec.choice, "progress")
        self.assertAlmostEqual(dec.confidence, 0.979)
        self.assertEqual(dec.provider, "gemini")
        self.assertGreaterEqual(dec.latency_ms, 0)

        self.assertEqual(len(self.fake.requests), 1)
        req = self.fake.requests[0]
        self.assertEqual(req["headers"].get("x-goog-api-key"), secret)
        self.assertEqual(req["body"]["model"], "gemini-decision-1")
        self.assertEqual(req["body"]["input"], "进度怎么样了")
        q = req["body"]["questions"]["intent"]
        self.assertEqual(q["type"], "choice")
        self.assertIsInstance(q["criteria"], dict)
        self.assertEqual(q["criteria"]["progress"], intent_decider.INTENT_OPTIONS["progress"])

    def test_http_errors_timeout_and_no_secret_in_logs(self) -> None:
        secret = "AIzaSyUltraSecretKeyDoNotLog1234"
        console_secrets.set_secret(self.settings, "GEMINI_API_KEY", secret)

        with patch.object(intent_decider, "GEMINI_ENDPOINT", self.fake.url):
            for status_code in (400, 401):
                self.fake.next_status = status_code
                self.fake.next_body = {"error": {"message": "bad"}}
                with self.assertLogs(intent_decider.logger, level="WARNING") as logs:
                    dec = intent_decider.decide_intent(self.settings, "进度怎么样了", intent_decider.INTENT_OPTIONS)
                self.assertIsNone(dec)
                self.assertNotIn(secret, "\n".join(logs.output))

            # Timeout -> None and no secret in logs
            self.fake.next_status = 200
            self.fake.delay_seconds = 0.35
            with patch.object(intent_decider, "TIMEOUT_SECONDS", 0.08):
                with self.assertLogs(intent_decider.logger, level="WARNING") as logs:
                    dec = intent_decider.decide_intent(self.settings, "进度怎么样了", intent_decider.INTENT_OPTIONS)
                self.assertIsNone(dec)
                self.assertNotIn(secret, "\n".join(logs.output))

            # Invalid choice -> None
            self.fake.delay_seconds = 0.0
            self.fake.next_body = {"answers": {"intent": {"choice": "unknown_id", "confidence": 0.9}}}
            with self.assertLogs(intent_decider.logger, level="WARNING") as logs:
                self.assertIsNone(
                    intent_decider.decide_intent(self.settings, "进度怎么样了", intent_decider.INTENT_OPTIONS)
                )
                self.assertNotIn(secret, "\n".join(logs.output))

    def _decider_health_after(self, status_code: int, body: dict | str) -> dict:
        """Run one failing decider call and return the persisted health record."""
        self.fake.next_status = status_code
        self.fake.next_body = body
        self.assertIsNone(
            intent_decider.decide_intent(self.settings, "进度怎么样了", intent_decider.INTENT_OPTIONS)
        )
        return intent_decider.get_decider_health(self.settings)

    def test_health_error_kind_mapping(self) -> None:
        secret = "AIzaSyKindMappingKey0001"
        console_secrets.set_secret(self.settings, "GEMINI_API_KEY", secret)
        cases = [
            (401, {"error": {"code": 401, "status": "UNAUTHENTICATED", "message": "no key"}}, "auth"),
            (403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "denied"}}, "auth"),
            (429, {"error": {"code": 429, "message": "Too Many Requests"}}, "rate_limit"),
            # Google answers an exhausted quota with 429 + RESOURCE_EXHAUSTED: the body wins.
            (429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}}, "quota"),
            (400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "bad shape"}}, "bad_request"),
            (408, {"error": {"code": 408, "status": "DEADLINE_EXCEEDED", "message": "slow"}}, "timeout"),
            (503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "later"}}, "other"),
        ]
        with patch.object(intent_decider, "GEMINI_ENDPOINT", self.fake.url):
            for status_code, body, expected_kind in cases:
                with self.subTest(status=status_code, expected=expected_kind):
                    health = self._decider_health_after(status_code, body)
                    self.assertEqual(health["last_error_kind"], expected_kind)
                    self.assertTrue(health["last_error_message"])
                    self.assertGreaterEqual(health["consecutive_failures"], 1)
                    self.assertNotIn(secret, json.dumps(health, ensure_ascii=False))

            # A transport timeout is its own kind, never an HTTP error.
            self.fake.next_status = 200
            self.fake.delay_seconds = 0.35
            with patch.object(intent_decider, "TIMEOUT_SECONDS", 0.08):
                self.assertIsNone(
                    intent_decider.decide_intent(self.settings, "进度怎么样了", intent_decider.INTENT_OPTIONS)
                )
            timeout_health = intent_decider.get_decider_health(self.settings)
            self.assertEqual(timeout_health["last_error_kind"], "timeout")
            self.fake.delay_seconds = 0.0

    def test_health_persists_and_resets_on_success(self) -> None:
        secret = "AIzaSyHealthPersistKey01"
        console_secrets.set_secret(self.settings, "GEMINI_API_KEY", secret)
        rejected = {
            "error": {
                "code": 401,
                "status": "PERMISSION_DENIED",
                "message": "The bound service account is deleted or disabled",
                "details": [{"@type": "reason", "reason": "ACCOUNT_STATE_INVALID"}],
            }
        }
        with patch.object(intent_decider, "GEMINI_ENDPOINT", self.fake.url):
            for _ in range(3):
                self._decider_health_after(401, rejected)

            health = intent_decider.get_decider_health(self.settings)
            self.assertEqual(health["last_error_kind"], "auth")
            self.assertEqual(health["consecutive_failures"], 3)
            self.assertTrue(health["unhealthy"])
            self.assertIn("密钥被拒绝了", health["last_error_message"])
            self.assertIn("绑定的账号已删除或停用", health["last_error_message"])

            # The record lives beside the decider config and never holds the key.
            stored = provider_config.config_path(self.settings).read_text(encoding="utf-8")
            self.assertIn("[intent_decider_health.gemini]", stored)
            self.assertIn("consecutive_failures = \"3\"", stored)
            self.assertNotIn(secret, stored)

            # One success clears the failure streak and records 上次成功.
            self.fake.next_status = 200
            self.fake.next_body = {
                "answers": {
                    "intent": {
                        "choice": "progress",
                        "confidence": 0.9,
                        "probabilities": {"progress": 0.9, "chat": 0.1},
                    }
                }
            }
            self.assertIsNotNone(
                intent_decider.decide_intent(self.settings, "进度怎么样了", intent_decider.INTENT_OPTIONS)
            )
            healthy = intent_decider.get_decider_health(self.settings)
            self.assertEqual(healthy["consecutive_failures"], 0)
            self.assertIsNone(healthy["last_error_kind"])
            self.assertFalse(healthy["unhealthy"])
            self.assertTrue(healthy["last_ok_at"])

    def test_health_write_is_throttled_for_repeated_identical_errors(self) -> None:
        console_secrets.set_secret(self.settings, "GEMINI_API_KEY", "AIzaSyThrottleKey0001")
        rejected = {"error": {"code": 401, "status": "UNAUTHENTICATED", "message": "nope"}}
        with patch.object(intent_decider, "GEMINI_ENDPOINT", self.fake.url):
            self.fake.next_status = 401
            self.fake.next_body = rejected
            for _ in range(5):
                self.assertIsNone(
                    intent_decider.decide_intent(self.settings, "进度怎么样了", intent_decider.INTENT_OPTIONS)
                )
            # Failures 1-3 are state changes (climbing to the unhealthy line); the
            # identical 4th and 5th must not rewrite the record every time.
            self.assertEqual(intent_decider.get_decider_health(self.settings)["consecutive_failures"], 3)

            # Once the minute is over the same error refreshes the record again.
            key = "gemini:" + intent_decider._config_fingerprint(
                intent_decider.resolve_decider_config(self.settings)
            )
            with patch.dict(intent_decider._HEALTH_LAST_WRITE, {key: time.time() - 61.0}):
                self.assertIsNone(
                    intent_decider.decide_intent(self.settings, "进度怎么样了", intent_decider.INTENT_OPTIONS)
                )
            self.assertEqual(intent_decider.get_decider_health(self.settings)["consecutive_failures"], 4)

    def test_http_endpoint_returns_decider_health(self) -> None:
        secret = "AIzaSyHealthApiKey0001"
        console_secrets.set_secret(self.settings, "GEMINI_API_KEY", secret)
        with patch.object(intent_decider, "GEMINI_ENDPOINT", self.fake.url):
            self._decider_health_after(
                401,
                {"error": {"code": 401, "status": "PERMISSION_DENIED", "message": "deleted"}},
            )
            h = _HttpHarness(self.settings)
            try:
                conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=10)
                headers = {"Authorization": f"Bearer {TOKEN}"}
                conn.request("GET", "/api/config/intent-decider", headers=headers)
                resp = conn.getresponse()
                data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(resp.status, 200)
                health = data["health"]
                self.assertEqual(health["provider"], "gemini")
                self.assertEqual(health["last_error_kind"], "auth")
                self.assertTrue(health["unhealthy"])
                self.assertTrue(health["last_error_message"])
                self.assertNotIn(secret, json.dumps(data, ensure_ascii=False))
                conn.close()
            finally:
                from conveyor.agents import supervisor_service as svc
                svc.stop_server_worker(h.server)
                h.close()

    def test_jev_and_custom_providers_and_config_coexistence(self) -> None:
        # Save model provider config first, then intent_decider, then model provider again -> both preserved.
        provider_config.save_provider_config(
            self.settings,
            {
                "provider_id": "deepseek",
                "provider_name": "DeepSeek",
                "model": "deepseek-chat",
                "base_url": "https://api.deepseek.com/v1",
                "wire_api": "chat",
                "env_key": "DEEPSEEK_API_KEY",
                "api_key": "sk-deepseek-dummy-1234",
            },
        )
        intent_decider.save_intent_decider_config(
            self.settings,
            {
                "provider": "custom",
                "endpoint": self.fake.url,
                "model": "my-decider-v1",
                "secret_name": "MY_DECIDER_KEY",
            },
        )
        # Re-saving provider_config must preserve [intent_decider]
        provider_config.save_provider_config(
            self.settings,
            {
                "provider_id": "deepseek",
                "provider_name": "DeepSeek",
                "model": "deepseek-reasoner",
                "base_url": "https://api.deepseek.com/v1",
                "wire_api": "chat",
                "env_key": "DEEPSEEK_API_KEY",
                "api_key": "",
            },
        )
        cfg = intent_decider.get_intent_decider_config(self.settings)
        self.assertEqual(cfg["provider"], "custom")
        self.assertEqual(cfg["model"], "my-decider-v1")
        self.assertEqual(cfg["secret_name"], "MY_DECIDER_KEY")
        self.assertEqual(provider_config.get_provider_config(self.settings)["model"], "deepseek-reasoner")

        custom_secret = "custom-bearer-secret-9999"
        console_secrets.set_secret(self.settings, "MY_DECIDER_KEY", custom_secret)
        dec = intent_decider.decide_intent(self.settings, "停下来", intent_decider.INTENT_OPTIONS)
        self.assertIsNotNone(dec)
        assert dec is not None
        self.assertEqual(dec.provider, "custom")
        self.assertEqual(self.fake.requests[-1]["headers"].get("authorization"), f"Bearer {custom_secret}")
        self.assertEqual(self.fake.requests[-1]["body"]["model"], "my-decider-v1")

        # Jev without endpoint reports missing endpoint
        intent_decider.save_intent_decider_config(self.settings, {"provider": "jev"})
        console_secrets.set_secret(self.settings, "TYPESAFE_API_KEY", "jev-key-123456")
        status, test_res = intent_decider.test_intent_decider(self.settings)
        self.assertEqual(status, 400)
        self.assertFalse(test_res["ok"])
        self.assertEqual(test_res["error"], "还没填 Jev 的接口地址")

    def test_http_endpoints_get_post_and_test(self) -> None:
        secret = "AIzaSyHttpTestSecret0001"
        console_secrets.set_secret(self.settings, "GEMINI_API_KEY", secret)
        h = _HttpHarness(self.settings)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=10)
            headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

            # Unauth rejected
            conn.request("GET", "/api/config/intent-decider")
            r_unauth = conn.getresponse()
            r_unauth.read()
            self.assertEqual(r_unauth.status, 401)

            # GET auto-selects gemini
            conn.request("GET", "/api/config/intent-decider", headers=headers)
            r = conn.getresponse()
            data = json.loads(r.read().decode("utf-8"))
            self.assertEqual(r.status, 200)
            self.assertEqual(data["provider"], "gemini")
            self.assertTrue(data["secret_configured"])
            self.assertNotIn(secret, json.dumps(data))

            # POST save jev with invalid remote http URL -> 400 Chinese error
            conn.request(
                "POST",
                "/api/config/intent-decider",
                body=json.dumps({"provider": "jev", "endpoint": "http://remote.example.com/v1"}),
                headers=headers,
            )
            r_bad = conn.getresponse()
            err_body = json.loads(r_bad.read().decode("utf-8"))
            self.assertEqual(r_bad.status, 400)
            self.assertIn("https", err_body["error"])

            # POST /test with fake gemini endpoint
            with patch.object(intent_decider, "GEMINI_ENDPOINT", self.fake.url):
                conn.request(
                    "POST",
                    "/api/config/intent-decider/test",
                    body=json.dumps({"provider": "gemini"}),
                    headers=headers,
                )
                r_test = conn.getresponse()
                test_body = json.loads(r_test.read().decode("utf-8"))
                self.assertEqual(r_test.status, 200)
                self.assertTrue(test_body["ok"])
                self.assertEqual(test_body["choice"], "progress")
                self.assertEqual(test_body["choice_label"], "查看进度")
                self.assertAlmostEqual(test_body["confidence"], 0.979)
                self.assertNotIn(secret, json.dumps(test_body))

            # POST save off
            conn.request(
                "POST",
                "/api/config/intent-decider",
                body=json.dumps({"provider": "off"}),
                headers=headers,
            )
            r_off = conn.getresponse()
            off_body = json.loads(r_off.read().decode("utf-8"))
            self.assertEqual(r_off.status, 200)
            self.assertEqual(off_body["config"]["provider"], "off")
            conn.close()
        finally:
            from conveyor.agents import supervisor_service as svc
            svc.stop_server_worker(h.server)
            h.close()
