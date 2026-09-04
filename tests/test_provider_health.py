from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from provider_health import ProviderHealthStore, classify_provider_error
from runner.provider_guard import guarded_run_codex_attempt


class ProviderHealthTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, root: Path):
        return SimpleNamespace(
            codex_memory_root=root / ".codex",
            conveyor_provider_circuit_threshold=1,
            conveyor_provider_circuit_seconds=180,
        )

    def write_provider(self, root: Path) -> None:
        codex = root / ".codex"
        codex.mkdir(parents=True, exist_ok=True)
        (codex / "config.toml").write_text(
            'model = "MiniMax-M3"\n'
            'model_provider = "minimax"\n'
            'model_reasoning_effort = "minimal"\n\n'
            '[model_providers.minimax]\n'
            'name = "MiniMax"\n'
            'base_url = "https://api.minimax.io/v1"\n'
            'env_key = "MINIMAX_API_KEY"\n'
            'wire_api = "responses"\n',
            encoding="utf-8",
        )
        (root / ".env").write_text("MINIMAX_API_KEY=test-secret-key\n", encoding="utf-8")

    def test_error_classification(self):
        self.assertEqual(classify_provider_error("429 high demand"), "rate_limited")
        self.assertEqual(classify_provider_error("401 invalid api key"), "auth_failed")
        self.assertEqual(classify_provider_error("connection refused"), "unreachable")
        self.assertEqual(classify_provider_error("bad response"), "error")

    def test_revision_change_does_not_inherit_old_open_circuit(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            store = ProviderHealthStore(settings)
            failed = store.record_failure("minimax", "rev-a", "429 high demand")
            self.assertTrue(failed["circuit_open"])
            changed = store.snapshot("minimax", "rev-b")
            self.assertFalse(changed["circuit_open"])
            self.assertEqual(changed["status"], "unknown")

    def test_success_closes_circuit(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            store = ProviderHealthStore(settings)
            store.record_failure("minimax", "rev-a", "429 high demand")
            healthy = store.record_success("minimax", "rev-a")
            self.assertEqual(healthy["status"], "healthy")
            self.assertFalse(healthy["circuit_open"])
            self.assertEqual(healthy["consecutive_failures"], 0)

    async def test_guard_opens_circuit_and_next_attempt_fails_fast(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_provider(root)
            settings = self.settings(root)
            job = SimpleNamespace(return_code=None, error="", last_event="starting")
            progress: list[str] = []
            calls = 0

            async def fake_attempt(_runner, attempt_job, _progress):
                nonlocal calls
                calls += 1
                attempt_job.return_code = 1
                attempt_job.error = "upstream high demand"
                attempt_job.last_event = "429"

            async def on_progress(message: str):
                progress.append(message)

            runner = SimpleNamespace(settings=settings)
            with patch.dict("os.environ", {"CONVEYOR_ENV_FILE": str(root / ".env")}, clear=False), \
                 patch("runner.provider_guard._base_run_codex_attempt", fake_attempt):
                await guarded_run_codex_attempt(runner, job, on_progress)
                self.assertEqual(calls, 1)
                self.assertIn("temporarily unavailable", job.error)
                self.assertEqual(job.last_event, "provider unavailable (rate_limited)")

                second = SimpleNamespace(return_code=None, error="", last_event="starting")
                await guarded_run_codex_attempt(runner, second, on_progress)
                self.assertEqual(calls, 1)
                self.assertEqual(second.return_code, 75)
                self.assertEqual(second.last_event, "provider circuit open")
                self.assertGreaterEqual(len(progress), 2)


if __name__ == "__main__":
    unittest.main()
