import asyncio
import unittest
import inspect
from unittest.mock import MagicMock, patch
from conveyor.entrypoints.background_workers import start_background_workers

class TestEntrypointBackgroundWorkers(unittest.TestCase):
    def test_start_background_workers(self):
        loop = asyncio.new_event_loop()
        server = MagicMock()
        settings = MagicMock()
        runner = MagicMock()
        
        # Mock supervisor_service.is_enabled
        with patch("conveyor.agents.supervisor_service.is_enabled", return_value=True), \
             patch("conveyor.agents.supervisor_service.ensure_server_worker") as mock_ensure, \
             patch("conveyor.automation.routines.start_routines_worker") as mock_routines, \
             patch("conveyor.channels.approval_relay.start_relay_worker") as mock_relay, \
             patch("agent_coordination.agent_delegation_worker.start") as mock_delegation, \
             patch("conveyor.agents.agent_security.start") as mock_security, \
             patch("conveyor.agents.worker_usage.start_background_refresh") as mock_usage:
            
            # memory_v2_enabled = True for test
            settings.memory_v2_enabled = True
            with patch("personal_tools.memory_v2.start_purge_worker") as mock_purge:
                workers = start_background_workers(loop, server, settings, runner)
                
            loop.run_until_complete(asyncio.sleep(0))
            loop.run_until_complete(asyncio.sleep(0))
            
            mock_routines.assert_called_once()
            mock_purge.assert_called_once()
            mock_relay.assert_called_once()
            mock_delegation.assert_called_once()
            mock_security.assert_called_once()
            mock_usage.assert_called_once()
            mock_ensure.assert_called_with(server, settings, wake=True)
            
            self.assertIsNotNone(workers.usage_refresh)
            self.assertIsNotNone(workers.agent_retention_worker)
            self.assertIsNotNone(workers.delegation_worker)
            self.assertIsNotNone(workers.relay_consumer)

    def test_source_guard(self):
        from conveyor.entrypoints import web_console
        from conveyor.entrypoints import web_console_takeover
        
        # We need to make sure main() refers to start_background_workers
        self.assertIn("start_background_workers", inspect.getsource(web_console.main))
        self.assertIn("start_background_workers", inspect.getsource(web_console_takeover.main))

if __name__ == "__main__":
    unittest.main()


class NoDuplicateBootWorkTests(unittest.TestCase):
    def test_entrypoints_do_not_start_workers_or_resume_goals_themselves(self) -> None:
        import inspect
        from conveyor.entrypoints import web_console, web_console_takeover
        for module in (web_console, web_console_takeover):
            source = inspect.getsource(module.main)
            for needle in ("resume_goal_loops", "ensure_server_worker", "start_routines_worker",
                           "start_background_refresh", "start_relay_worker"):
                self.assertNotIn(needle, source, f"{module.__name__}.main starts {needle} itself")
