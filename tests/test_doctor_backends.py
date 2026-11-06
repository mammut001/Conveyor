"""Regression tests for the Pi-backend and chat-provider doctor checks.

Production runs ``CONVEYOR_AGENT_BACKEND=pi`` (StepFun) with the chat tier in
``auto`` mode, but scripts/doctor.py historically only probed the Codex
provider. These tests cover ``scripts.harness_common.check_pi_backend`` and
``check_chat_provider``:

- a missing pi binary, a broken ``models.json`` or a missing StepFun key must
  now be visible to doctor;
- an API key VALUE must never appear in any CheckResult detail (only the env
  var name and set/missing).
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from harness_common import check_chat_provider, check_pi_backend  # noqa: E402

_SECRET_VALUE = "sk-super-secret-value-DO-NOT-LEAK"


def _settings(task_root: Path, **overrides) -> SimpleNamespace:
    base = dict(
        codex_task_root=task_root,
        codex_memory_root=task_root / "memory",
        secrets_file=None,
        chat_mode="off",
        chat_base_url=None,
        chat_api_key=None,
        chat_model=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _write_models(task_root: Path, obj) -> None:
    pi_dir = task_root / "pi-agent"
    pi_dir.mkdir(parents=True, exist_ok=True)
    (pi_dir / "models.json").write_text(
        obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8"
    )


def _by_name(results):
    return {r.name: r for r in results}


class _FakeResponse(io.BytesIO):
    """A urlopen() response usable as a context manager, with .status/getcode."""

    def __init__(self, payload: bytes, status: int = 200) -> None:
        super().__init__(payload)
        self.status = status

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _models_payload(*ids: str) -> bytes:
    return json.dumps({"data": [{"id": m} for m in ids]}).encode("utf-8")


class PiBackendCheckTests(unittest.TestCase):
    def _env(self, **extra: str) -> dict:
        env = {"CONVEYOR_AGENT_BACKEND": "pi"}
        env.update(extra)
        return env

    def test_non_pi_backend_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            with mock.patch.dict(os.environ, {"CONVEYOR_AGENT_BACKEND": "codex"}, clear=True):
                self.assertEqual(check_pi_backend(settings), [])

    def test_binary_missing_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            env = self._env(
                CONVEYOR_PI_PROVIDER="stepfun",
                CONVEYOR_PI_MODEL="step-5-preview",
                STEPFUN_API_KEY=_SECRET_VALUE,
            )
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("harness_common.shutil.which", return_value=None):
                results = _by_name(check_pi_backend(settings))
            self.assertFalse(results["pi binary"].ok)
            self.assertIn("pi", results["pi binary"].detail.lower())

    def test_models_declares_provider_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_root = Path(tmp)
            settings = _settings(task_root)
            _write_models(
                task_root,
                {"providers": {"stepfun": {"models": [{"id": "step-5-preview"}, {"id": "step-3"}]}}},
            )
            env = self._env(
                CONVEYOR_PI_PROVIDER="stepfun",
                CONVEYOR_PI_MODEL="step-5-preview",
                STEPFUN_API_KEY=_SECRET_VALUE,
            )
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("harness_common.shutil.which", return_value="/usr/bin/pi"):
                results = _by_name(check_pi_backend(settings))
            self.assertTrue(results["pi models.json"].ok, results["pi models.json"].detail)
            self.assertIn("provider=stepfun", results["pi models.json"].detail)
            self.assertIn("step-5-preview", results["pi models.json"].detail)
            self.assertTrue(results["pi binary"].ok)

    def test_model_not_declared_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_root = Path(tmp)
            settings = _settings(task_root)
            _write_models(
                task_root,
                {"providers": {"stepfun": {"models": [{"id": "step-3"}]}}},
            )
            env = self._env(
                CONVEYOR_PI_PROVIDER="stepfun",
                CONVEYOR_PI_MODEL="step-5-preview",
                STEPFUN_API_KEY=_SECRET_VALUE,
            )
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("harness_common.shutil.which", return_value="/usr/bin/pi"):
                results = _by_name(check_pi_backend(settings))
            self.assertFalse(results["pi models.json"].ok)
            self.assertIn("step-5-preview", results["pi models.json"].detail)

    def test_invalid_json_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_root = Path(tmp)
            settings = _settings(task_root)
            _write_models(task_root, "{not valid json")
            env = self._env(
                CONVEYOR_PI_PROVIDER="stepfun",
                CONVEYOR_PI_MODEL="step-5-preview",
                STEPFUN_API_KEY=_SECRET_VALUE,
            )
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("harness_common.shutil.which", return_value="/usr/bin/pi"):
                results = _by_name(check_pi_backend(settings))
            self.assertFalse(results["pi models.json"].ok)
            self.assertIn("invalid JSON", results["pi models.json"].detail)

    def test_missing_models_file_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            env = self._env(
                CONVEYOR_PI_PROVIDER="stepfun",
                CONVEYOR_PI_MODEL="step-5-preview",
                STEPFUN_API_KEY=_SECRET_VALUE,
            )
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("harness_common.shutil.which", return_value="/usr/bin/pi"):
                results = _by_name(check_pi_backend(settings))
            self.assertTrue(results["pi models.json"].ok)
            self.assertIn("absent", results["pi models.json"].detail)

    def test_api_key_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            env = self._env(
                CONVEYOR_PI_PROVIDER="stepfun",
                CONVEYOR_PI_MODEL="step-5-preview",
                STEPFUN_API_KEY=_SECRET_VALUE,
            )
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("harness_common.shutil.which", return_value="/usr/bin/pi"):
                results = _by_name(check_pi_backend(settings))
            self.assertTrue(results["pi api key"].ok)
            self.assertIn("STEPFUN_API_KEY", results["pi api key"].detail)
            self.assertIn("set", results["pi api key"].detail)

    def test_api_key_missing_and_value_never_leaked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            env = self._env(
                CONVEYOR_PI_PROVIDER="stepfun",
                CONVEYOR_PI_MODEL="step-5-preview",
            )
            # No STEPFUN_API_KEY; console store also returns nothing.
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("harness_common.shutil.which", return_value="/usr/bin/pi"), \
                    mock.patch("console_secrets.read_secret", return_value=None):
                results = check_pi_backend(settings)
            by_name = _by_name(results)
            self.assertFalse(by_name["pi api key"].ok)
            self.assertIn("STEPFUN_API_KEY", by_name["pi api key"].detail)
            self.assertIn("missing", by_name["pi api key"].detail)
            for result in results:
                self.assertNotIn(_SECRET_VALUE, result.detail)


class ChatProviderCheckTests(unittest.TestCase):
    def test_mode_off_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp), chat_mode="off")
            self.assertIsNone(check_chat_provider(settings))

    def test_auto_without_key_or_model_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp), chat_mode="auto")
            result = check_chat_provider(settings)
            self.assertIsNotNone(result)
            self.assertFalse(result.ok)
            self.assertIn("chat key/model missing", result.detail)

    def test_model_listed_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(
                Path(tmp),
                chat_mode="auto",
                chat_base_url="https://api.example.test/v1",
                chat_api_key=_SECRET_VALUE,
                chat_model="step-5-preview",
            )
            with mock.patch(
                "harness_common.urllib.request.urlopen",
                return_value=_FakeResponse(_models_payload("step-5-preview", "step-3")),
            ):
                result = check_chat_provider(settings)
            self.assertTrue(result.ok, result.detail)
            self.assertEqual(result.detail, "model=step-5-preview listed")
            self.assertNotIn(_SECRET_VALUE, result.detail)

    def test_model_not_listed_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(
                Path(tmp),
                chat_mode="auto",
                chat_base_url="https://api.example.test/v1",
                chat_api_key=_SECRET_VALUE,
                chat_model="step-5-preview",
            )
            with mock.patch(
                "harness_common.urllib.request.urlopen",
                return_value=_FakeResponse(_models_payload("step-3")),
            ):
                result = check_chat_provider(settings)
            self.assertFalse(result.ok)
            self.assertIn("not listed", result.detail)

    def test_http_401_fails_without_key(self) -> None:
        import urllib.error

        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(
                Path(tmp),
                chat_mode="auto",
                chat_base_url="https://api.example.test/v1",
                chat_api_key=_SECRET_VALUE,
                chat_model="step-5-preview",
            )

            def _raise(request, timeout=0):
                raise urllib.error.HTTPError(
                    request.full_url, 401, "Unauthorized", hdrs=None, fp=None
                )

            with mock.patch("harness_common.urllib.request.urlopen", _raise):
                result = check_chat_provider(settings)
            self.assertFalse(result.ok)
            self.assertIn("401", result.detail)
            self.assertNotIn(_SECRET_VALUE, result.detail)


if __name__ == "__main__":
    unittest.main()
