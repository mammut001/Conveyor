"""tests/test_supervisor_pool_executor.py — Supervisor "pool" executor.

Uses fake delegate executors and an injected workers list; no real CLI is run.
"""
from __future__ import annotations

import asyncio
import dataclasses
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent_supervisor as ags
import agent_supervisor_pool as pool
import workers
from agent_supervisor import ExecutionResult, RoleConfig
from supervisor_service import _UI_EXPOSED_EXECUTORS

ANALYST = RoleConfig(name="analyst", executor="pool", tools=["read"])
IMPLEMENTER = RoleConfig(name="implementer", executor="pool", tools=["read", "write"])
REVIEWER = RoleConfig(name="reviewer", executor="pool", tools=["read"])
QUOTA_ERROR = "RESOURCE_EXHAUSTED: quota exceeded, resets in 2h"


def _worker(wid, *, enabled=True, installed=True, auth="ok", state="available",
            usage=None, threshold=95):
    return {
        "id": wid, "label": wid, "installed": installed, "runnable": True,
        "enabled": enabled, "auth": auth,
        "quota": {"state": state, "reset_at": None, "reason": None},
        "usage": usage, "threshold_percent": threshold,
    }


def _busy(percent):
    return {"windows": [{"used_percent": percent, "resets_at": time.time() + 3600}]}


class FakeDelegate:
    def __init__(self, name, results, calls, received):
        self.name = name
        self._results = results
        self._calls = calls
        self._received = received

    def available(self):
        return True, "ok"

    async def run(self, *, role, prompt, worktree_path, timeout_seconds, task_id="", context=None):
        self._calls.append(self.name)
        self._received.append(role)
        return self._results[self.name]


def _result(success, error="", output="", execution_id="ex-1"):
    return ExecutionResult(success=success, error=error, output=output,
                           started_at="t0", finished_at="t1", execution_id=execution_id)


class PoolExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="pool-test-")
        self.settings = SimpleNamespace(workers_unsandboxed=False, codex_memory_root=self._tmp)
        self.calls: list[str] = []
        self.results: dict[str, ExecutionResult] = {}
        self.workers: list[dict] = []
        self.factory_names: list[str] = []
        self.received_roles: list[RoleConfig] = []

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _executor(self) -> pool.PoolExecutor:
        def factory(settings, name):
            self.factory_names.append(name)
            return FakeDelegate(name, self.results, self.calls, self.received_roles)
        return pool.PoolExecutor(
            self.settings,
            executor_factory=factory,
            workers_provider=lambda _s: list(self.workers),
        )

    def _run(self, role=ANALYST, executor=None):
        executor = executor or self._executor()
        return asyncio.run(executor.run(role=role, prompt="p", worktree_path=self._tmp,
                                        timeout_seconds=30))

    # 1. pool order, with ineligible workers skipped
    def test_picks_first_eligible_in_pool_order_and_skips_ineligible(self) -> None:
        self.workers = [
            _worker("agy", enabled=False),
            _worker("cmd", auth="needs_login"),
            _worker("claude-code", state="cooling"),
            _worker("pi", auth="needs_login"),
            _worker("grok"),
            _worker("codex"),
        ]
        self.assertEqual(self._executor().candidates(ANALYST), [])
        self.workers.append(_worker("claude-code"))
        self.workers.append(_worker("cmd"))
        self.workers.append(_worker("pi"))
        self.assertEqual([w["id"] for w in self._executor().candidates(ANALYST)],
                         ["claude-code", "cmd", "pi"])

    def test_not_installed_is_skipped(self) -> None:
        self.workers = [_worker("claude-code", installed=False), _worker("agy")]
        self.assertEqual([w["id"] for w in self._executor().candidates(ANALYST)], ["agy"])

    # 2. over-threshold workers go after under-threshold ones
    def test_over_threshold_worker_goes_after_under_threshold(self) -> None:
        self.workers = [
            _worker("claude-code", usage=_busy(97)),
            _worker("agy", usage=_busy(10)),
        ]
        self.assertEqual([w["id"] for w in self._executor().candidates(ANALYST)],
                         ["agy", "claude-code"])

    # 3. quota on a read role: cool, record the switch, next worker succeeds
    def test_quota_failure_on_read_role_rotates_to_next_worker(self) -> None:
        self.workers = [_worker("claude-code"), _worker("agy")]
        self.results = {
            "claude": _result(False, error=QUOTA_ERROR, execution_id="ex-claude"),
            "antigravity": _result(True, output="done", execution_id="ex-agy"),
        }
        result = self._run()

        self.assertTrue(result.success)
        self.assertEqual(self.calls, ["claude", "antigravity"])
        pool_ev = result.evidence["pool"]
        self.assertEqual(pool_ev["worker_id"], "agy")
        self.assertEqual(len(pool_ev["attempts"]), 2)
        self.assertEqual(pool_ev["attempts"][0]["worker_id"], "claude-code")
        self.assertFalse(pool_ev["attempts"][0]["success"])
        self.assertEqual(pool_ev["attempts"][0]["execution_id"], "ex-claude")
        state = workers.load_workers_state(self.settings)
        self.assertIn("claude-code", state["cooling"])
        self.assertEqual(state["switch_log"][-1]["reason"], "quota_error")
        self.assertEqual(state["switch_log"][-1]["to"], "agy")

    # 4. write role that already changed the worktree is not rotated
    def test_quota_on_write_role_after_worktree_changed_does_not_rotate(self) -> None:
        self.workers = [_worker("claude-code"), _worker("agy")]
        self.results = {"claude": _result(False, error=QUOTA_ERROR)}
        identities = [{"sha": "before"}, {"sha": "after"}]
        with patch("agent_supervisor.target_identity", side_effect=identities):
            result = self._run(role=IMPLEMENTER)

        self.assertFalse(result.success)
        self.assertEqual(self.calls, ["claude"])
        self.assertEqual(len(result.evidence["pool"]["attempts"]), 1)
        self.assertNotIn("执行者池里的执行者额度都用完了", result.error)
        self.assertNotIn("claude-code", workers.load_workers_state(self.settings)["cooling"])

    def test_quota_on_write_role_with_unchanged_worktree_rotates(self) -> None:
        self.workers = [_worker("claude-code"), _worker("agy")]
        self.results = {
            "claude": _result(False, error=QUOTA_ERROR),
            "antigravity": _result(True, output="ok"),
        }
        with patch("agent_supervisor.target_identity", return_value={"sha": "same"}):
            result = self._run(role=IMPLEMENTER)
        self.assertTrue(result.success)
        self.assertEqual(self.calls, ["claude", "antigravity"])

    # 5. non-quota failure: no rotation
    def test_non_quota_failure_does_not_rotate(self) -> None:
        self.workers = [_worker("claude-code"), _worker("agy")]
        self.results = {"claude": _result(False, error="SyntaxError: bad patch")}
        result = self._run()
        self.assertFalse(result.success)
        self.assertEqual(self.calls, ["claude"])
        self.assertEqual(result.error, "SyntaxError: bad patch")

    # 6. every worker hits quota
    def test_all_quota_returns_prefixed_error(self) -> None:
        self.workers = [_worker("claude-code"), _worker("agy")]
        self.results = {
            "claude": _result(False, error=QUOTA_ERROR),
            "antigravity": _result(False, error=QUOTA_ERROR),
        }
        result = self._run()
        self.assertFalse(result.success)
        self.assertEqual(self.calls, ["claude", "antigravity"])
        self.assertTrue(result.error.startswith(pool.ALL_QUOTA_PREFIX))
        self.assertEqual(len(result.evidence["pool"]["attempts"]), 2)

    # 7. no candidates: never falls back, factory never called
    def test_no_candidates_fails_without_calling_factory(self) -> None:
        self.workers = [_worker("pi", auth="needs_login"), _worker("grok"), _worker("claude-code", enabled=False)]
        ex = self._executor()
        self.assertEqual(ex.available(), (False, pool.NO_WORKER_REASON))
        result = self._run(executor=ex)
        self.assertFalse(result.success)
        self.assertEqual(result.error, pool.NO_WORKER_REASON)
        self.assertIn("Pi", pool.NO_WORKER_REASON)
        self.assertEqual(self.factory_names, [])
        self.assertEqual(self.calls, [])

    def test_available_true_when_some_worker_is_eligible(self) -> None:
        self.workers = [_worker("agy")]
        self.assertEqual(self._executor().available(), (True, "ok"))

    def test_workers_provider_error_is_not_raised(self) -> None:
        def boom(_s):
            raise RuntimeError("detect failed")
        ex = pool.PoolExecutor(self.settings, workers_provider=boom)
        self.assertEqual(ex.available(), (False, pool.NO_WORKER_REASON))

    # 9. read roles never use fix mode; cmd is only excluded for write roles
    def test_command_code_excluded_for_implementer_but_allowed_for_read_roles(self) -> None:
        self.workers = [_worker("cmd")]
        ex = self._executor()
        self.assertEqual(ex.candidates(IMPLEMENTER), [])
        self.assertEqual([w["id"] for w in ex.candidates(ANALYST)], ["cmd"])
        self.assertEqual([w["id"] for w in ex.candidates(REVIEWER)], ["cmd"])

    def test_delegate_gets_mapped_executor_name_and_no_supervisor_model(self) -> None:
        # Analyst role model comes from the chat tier (e.g. deepseek-chat); it must not reach CLIs.
        self.workers = [_worker("claude-code")]
        self.results = {"claude": _result(True, output="ok")}
        role = dataclasses.replace(ANALYST, model="deepseek-chat")
        self._run(role=role)
        received = self.received_roles[0]
        self.assertEqual(received.executor, "claude")
        self.assertIsNone(received.model)
        self.assertEqual(role.model, "deepseek-chat")

    def test_delegate_gets_worker_own_model(self) -> None:
        self.workers = [dict(_worker("claude-code"), model="claude-sonnet-5-5")]
        self.results = {"claude": _result(True, output="ok")}
        role = dataclasses.replace(ANALYST, model="deepseek-chat")
        self._run(role=role)
        self.assertEqual(self.received_roles[0].model, "claude-sonnet-5-5")

    def test_each_attempt_gets_its_own_executor_and_model(self) -> None:
        self.workers = [
            dict(_worker("claude-code"), model="claude-sonnet-5-5"),
            dict(_worker("agy"), model="  "),
        ]
        self.results = {
            "claude": _result(False, error=QUOTA_ERROR),
            "antigravity": _result(True, output="ok"),
        }
        self._run(role=dataclasses.replace(ANALYST, model="deepseek-chat"))
        sent = [(r.executor, r.model) for r in self.received_roles]
        self.assertEqual(sent, [("claude", "claude-sonnet-5-5"), ("antigravity", None)])

    def test_pi_is_eligible_for_read_and_write_roles_and_maps_to_pi(self) -> None:
        self.workers = [_worker("pi")]
        ex = self._executor()
        self.assertEqual(ex.available(), (True, "ok"))
        self.assertEqual([w["id"] for w in ex.candidates(ANALYST)], ["pi"])
        self.assertEqual([w["id"] for w in ex.candidates(IMPLEMENTER)], ["pi"])
        self.assertEqual([w["id"] for w in ex.candidates(REVIEWER)], ["pi"])
        self.results = {"pi": _result(True, output="done", execution_id="ex-pi")}
        result = self._run(role=IMPLEMENTER, executor=ex)
        self.assertTrue(result.success)
        self.assertEqual(self.calls, ["pi"])
        self.assertEqual(self.received_roles[0].executor, "pi")
        self.assertEqual(result.evidence["pool"]["worker_id"], "pi")

    def test_pi_never_receives_pool_worker_model_or_supervisor_role_model(self) -> None:
        # Pi resolves its provider/model via resolve_pi_provider; passing a pool worker
        # model without a matching provider/models.json entry would fail Pi CLI startup.
        self.workers = [
            dict(_worker("claude-code"), model="claude-sonnet-5-5"),
            dict(_worker("pi"), model="pool-model-pi-cannot-use"),
        ]
        self.results = {
            "claude": _result(False, error=QUOTA_ERROR),
            "pi": _result(True, output="ok"),
        }
        self._run(role=dataclasses.replace(ANALYST, model="deepseek-chat"))
        sent = [(r.executor, r.model) for r in self.received_roles]
        self.assertEqual(sent, [("claude", "claude-sonnet-5-5"), ("pi", None)])

    def test_pi_participates_in_quota_rotation(self) -> None:
        self.workers = [_worker("pi"), _worker("agy")]
        self.results = {
            "pi": _result(False, error=QUOTA_ERROR, execution_id="ex-pi"),
            "antigravity": _result(True, output="done", execution_id="ex-agy"),
        }
        result = self._run(role=REVIEWER)
        self.assertTrue(result.success)
        self.assertEqual(self.calls, ["pi", "antigravity"])
        state = workers.load_workers_state(self.settings)
        self.assertIn("pi", state["cooling"])
        self.assertEqual(state["switch_log"][-1]["from"], "pi")
        self.assertEqual(state["switch_log"][-1]["to"], "agy")

    def test_pi_write_role_does_not_rotate_after_edits(self) -> None:
        self.workers = [_worker("pi"), _worker("agy")]
        self.results = {"pi": _result(False, error=QUOTA_ERROR)}
        with patch("agent_supervisor.target_identity", side_effect=[{"sha": "a"}, {"sha": "b"}]):
            write_res = self._run(role=IMPLEMENTER)
        self.assertFalse(write_res.success)
        self.assertEqual(self.calls, ["pi"])
        self.assertNotIn("pi", workers.load_workers_state(self.settings)["cooling"])

    def test_cancel_delegates_to_cancel_execution(self) -> None:
        with patch("agent_supervisor.cancel_execution", return_value=True) as cancel:
            ok = asyncio.run(self._executor().cancel("ex-9"))
        self.assertTrue(ok)
        cancel.assert_called_once_with(self.settings, "ex-9")


class PoolWiringTests(unittest.TestCase):
    def test_get_executor_returns_pool(self) -> None:
        settings = SimpleNamespace(workers_unsandboxed=False)
        self.assertIsInstance(ags.get_executor(settings, "pool"), pool.PoolExecutor)

    def test_pool_is_exposed_in_ui_executors(self) -> None:
        self.assertIn("pool", [name for name, _label in _UI_EXPOSED_EXECUTORS])

    def test_pool_is_listed_first_with_chinese_label_and_plain_executor_names(self) -> None:
        exposed = dict(_UI_EXPOSED_EXECUTORS)
        self.assertEqual(_UI_EXPOSED_EXECUTORS[0][0], "pool")
        self.assertEqual(exposed["pool"], "执行者池（自动轮换）")
        self.assertEqual(exposed["claude"], "Claude Code")
        self.assertEqual(exposed["antigravity"], "Antigravity")


if __name__ == "__main__":
    unittest.main()
