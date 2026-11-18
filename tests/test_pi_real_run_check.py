import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import tempfile
import shutil
import os
from datetime import datetime, timezone
from conveyor.agents import worker_check

class PiRealRunCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pi-real-run-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.settings = MagicMock()
        self.settings.codex_memory_root = self.tmp

    def test_save_pi_real_run_records_success(self) -> None:
        with patch("conveyor.agents.workers.load_workers_state") as mock_load:
            with patch("conveyor.agents.workers.save_workers_state") as mock_save:
                mock_load.return_value = {"last_check": {}}
                
                worker_check.save_pi_real_run(self.settings)
                
                self.assertTrue(mock_save.called)
                saved_state = mock_save.call_args[0][1]
                self.assertTrue(saved_state["last_check"]["pi"]["ok"])
                self.assertEqual(saved_state["last_check"]["pi"]["kind"], "real_run")

    def test_save_pi_real_run_skips_if_recent(self) -> None:
        # 5 mins ago
        recent = datetime.now(timezone.utc).isoformat()
        with patch("conveyor.agents.workers.load_workers_state") as mock_load:
            with patch("conveyor.agents.workers.save_workers_state") as mock_save:
                mock_load.return_value = {"last_check": {"pi": {"ok": True, "checked_at": recent}}}
                
                worker_check.save_pi_real_run(self.settings)
                
                self.assertFalse(mock_save.called)


class PiRealRunCheckMoreTests(PiRealRunCheckTests):
    def _run_with(self, existing):
        with patch("conveyor.agents.workers.load_workers_state") as mock_load, \
                patch("conveyor.agents.workers.save_workers_state") as mock_save:
            mock_load.return_value = {"last_check": {"pi": existing} if existing is not None else {}}
            worker_check.save_pi_real_run(self.settings)
            return mock_save

    def test_old_success_is_refreshed(self) -> None:
        from datetime import timedelta
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        self.assertTrue(self._run_with({"ok": True, "kind": "ok", "checked_at": old}).called)

    def test_recent_failed_test_is_replaced_by_a_real_success(self) -> None:
        recent = datetime.now(timezone.utc).isoformat()
        save = self._run_with({"ok": False, "kind": "auth", "checked_at": recent})
        self.assertTrue(save.called)
        self.assertEqual(save.call_args[0][1]["last_check"]["pi"]["kind"], "real_run")

    def test_saved_shape_satisfies_web_checklist_and_goal_gate(self) -> None:
        save = self._run_with(None)
        check = save.call_args[0][1]["last_check"]["pi"]
        self.assertIs(check["ok"], True)          # SetupChecklist step ①: ok === true
        self.assertEqual(check["worker_id"], "pi")
        self.assertEqual(check["message"], "最近一次真实运行成功")
        self.assertTrue(check["checked_at"])


class PiExecutorRecordsOnlySuccessTests(unittest.TestCase):
    def test_record_call_is_guarded_by_success_and_swallows_errors(self) -> None:
        import inspect
        from conveyor.agents import agent_supervisor as ags
        source = inspect.getsource(ags.PiExecutor.run)
        guard = source.index("if result.success:")
        call = source.index("save_pi_real_run")
        self.assertLess(guard, call)
        self.assertIn("except Exception", source[guard:call + 200])
