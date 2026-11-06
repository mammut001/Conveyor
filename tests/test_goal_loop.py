import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import goal_loop
from goal_loop import (
    CheckResult, GoalEngine, GoalState, GoalStore, IterationResult,
    build_prompt, handle_goal_command, parse_goal_command, parse_verdict,
)


def _verdict(status, nxt="next step"):
    return f"did things\nGOAL_STATUS: {status}\nNEXT: {nxt}"


class FakeDriver:
    def __init__(self, results):
        self.results = list(results)
        self.prompts = []
        self.notes = []
        self.cancelled = 0

    async def run_iteration(self, state, prompt):
        self.prompts.append(prompt)
        item = self.results.pop(0) if self.results else IterationResult(True, _verdict("continue"), "/wt")
        return item(state) if callable(item) else item

    async def cancel_current(self):
        self.cancelled += 1

    async def notify(self, text):
        self.notes.append(text)


def make_state(store, **kw):
    now = datetime.now(timezone.utc)
    state = GoalState(
        id=kw.pop("id", "a" * 12), scope="web:chat", objective="make tests green",
        created_at=now.isoformat(timespec="seconds"),
        deadline=(now + timedelta(hours=kw.pop("hours", 8))).isoformat(timespec="seconds"),
        **kw)
    store.save(state)
    return state


async def no_sleep(_s):
    return None


def engine_for(store, driver, checks_fn=None, fingerprints=None, now=None):
    async def verifier(state, worktree):
        return checks_fn(state) if checks_fn else []

    seq = iter(fingerprints or [])

    async def fingerprint(_wt, _results):
        return next(seq, "same")

    kwargs = {"now": now} if now else {}
    return GoalEngine(store, driver, verifier=verifier, fingerprint=fingerprint, sleep=no_sleep, **kwargs)


class ParseTests(unittest.TestCase):
    def test_non_goal_text_is_ignored(self):
        self.assertIsNone(parse_goal_command("hello"))
        self.assertIsNone(parse_goal_command("/goals"))
        self.assertFalse(goal_loop.is_goal_command("fix the goal"))

    def test_subcommands(self):
        self.assertEqual(parse_goal_command("/goal").action, "help")
        self.assertEqual(parse_goal_command("/goal status").action, "status")
        self.assertEqual(parse_goal_command("/goal@MyBot stop").action, "stop")
        self.assertEqual(parse_goal_command("/goal 停止").action, "stop")

    def test_start_with_options(self):
        cmd = parse_goal_command('/goal 修复手机端 UI --hours 6 --max 30 --check "pytest -q" --check \'npm run test:chat\'')
        self.assertEqual(cmd.action, "start")
        self.assertEqual(cmd.objective, "修复手机端 UI")
        self.assertEqual(cmd.checks, ("pytest -q", "npm run test:chat"))
        self.assertEqual((cmd.hours, cmd.max_iterations), (6.0, 30))

    def test_limits_are_clamped_and_validated(self):
        cmd = parse_goal_command("/goal improve everything --hours 999 --max 9999")
        self.assertEqual((cmd.hours, cmd.max_iterations), (goal_loop.MAX_HOURS, goal_loop.MAX_ITERATIONS_CAP))
        self.assertEqual(parse_goal_command("/goal ab").action, "invalid")
        many = " ".join(f'--check "c{i}"' for i in range(6))
        self.assertEqual(parse_goal_command(f"/goal do the thing {many}").action, "invalid")

    def test_verdict(self):
        self.assertEqual(parse_verdict(_verdict("done", "all good")), ("done", "all good"))
        self.assertEqual(parse_verdict("no marker")[0], "continue")
        self.assertEqual(parse_verdict("GOAL_STATUS: continue\nGOAL_STATUS: blocked\nNEXT: need key")[0], "blocked")


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = GoalStore(Path(self.tmp.name))

    async def test_done_when_checks_pass_and_agent_claims_done(self):
        state = make_state(self.store, checks=["pytest"])
        driver = FakeDriver([IterationResult(True, _verdict("continue"), "/wt"),
                             IterationResult(True, _verdict("done", "shipped"), "/wt")])
        calls = iter([[CheckResult("pytest", False, 1, "boom")], [CheckResult("pytest", True, 0)]])
        engine = engine_for(self.store, driver, lambda s: next(calls), fingerprints=["a", "b"])
        result = await engine.run(state)
        self.assertEqual((result.status, result.iteration), ("done", 2))
        self.assertIn("boom", driver.prompts[1])  # failure fed back into the next prompt
        self.assertEqual(self.store.load(state.id).status, "done")
        self.assertIn("目标已完成", driver.notes[-1])

    async def test_agent_claim_is_not_trusted_when_checks_fail(self):
        state = make_state(self.store, checks=["pytest"], max_iterations=3)
        driver = FakeDriver([IterationResult(True, _verdict("done"), "/wt")] * 3)
        engine = engine_for(self.store, driver, lambda s: [CheckResult("pytest", False, 1, "x")],
                            fingerprints=["a", "b", "c"])
        self.assertEqual((await engine.run(state)).status, "max_iterations")

    async def test_without_checks_done_needs_consecutive_confirmations(self):
        state = make_state(self.store)
        driver = FakeDriver([IterationResult(True, _verdict("done"), "/wt"),
                             IterationResult(True, _verdict("continue"), "/wt"),
                             IterationResult(True, _verdict("done"), "/wt"),
                             IterationResult(True, _verdict("done"), "/wt")])
        engine = engine_for(self.store, driver, fingerprints=["a", "b", "c", "d"])
        result = await engine.run(state)
        self.assertEqual((result.status, result.iteration), ("done", 4))

    async def test_stalls_after_no_progress(self):
        state = make_state(self.store)
        driver = FakeDriver([])
        engine = engine_for(self.store, driver, fingerprints=["a"] + ["same"] * 10)
        result = await engine.run(state)
        self.assertEqual(result.status, "stalled")
        self.assertEqual(result.iteration, 2 + goal_loop.NO_PROGRESS_LIMIT)  # first two fingerprints are progress
        self.assertGreaterEqual(result.no_progress, goal_loop.NO_PROGRESS_LIMIT)
        self.assertIn("没有进展", driver.notes[-1])

    async def test_progress_resets_stall_counter(self):
        state = make_state(self.store, max_iterations=6)
        driver = FakeDriver([])
        engine = engine_for(self.store, driver, fingerprints=list("abcdef"))
        self.assertEqual((await engine.run(state)).status, "max_iterations")

    async def test_consecutive_failures_stop_but_success_resets(self):
        state = make_state(self.store)
        fail = IterationResult(False, error="provider crashed")
        driver = FakeDriver([fail, fail, IterationResult(True, _verdict("continue"), "/wt"), fail, fail, fail])
        engine = engine_for(self.store, driver, fingerprints=["a"])
        result = await engine.run(state)
        self.assertEqual((result.status, result.iteration), ("failed", 6))

    async def test_blocked_stops_with_reason(self):
        state = make_state(self.store)
        driver = FakeDriver([IterationResult(True, _verdict("blocked", "need the API key"), "/wt")])
        result = await engine_for(self.store, driver).run(state)
        self.assertEqual((result.status, result.detail), ("blocked", "need the API key"))

    async def test_deadline(self):
        state = make_state(self.store, hours=1)
        later = datetime.now(timezone.utc) + timedelta(hours=2)
        driver = FakeDriver([])
        result = await engine_for(self.store, driver, now=lambda: later).run(state)
        self.assertEqual((result.status, result.iteration), ("timeout", 0))
        self.assertEqual(driver.prompts, [])

    async def test_transient_does_not_consume_an_iteration(self):
        state = make_state(self.store, max_iterations=1)
        driver = FakeDriver([IterationResult(False, error="rate limit", transient=True),
                             IterationResult(True, _verdict("continue"), "/wt")])
        result = await engine_for(self.store, driver, fingerprints=["a"]).run(state)
        self.assertEqual((result.status, result.iteration), ("max_iterations", 1))
        self.assertEqual(result.failures, 0)

    async def test_stop_during_iteration_is_not_a_failure(self):
        state = make_state(self.store)

        def stop_then_fail(s):
            stopped = self.store.load(s.id)
            stopped.status = "stopping"
            self.store.save(stopped)
            return IterationResult(False, error="cancelled")

        driver = FakeDriver([stop_then_fail])
        result = await engine_for(self.store, driver).run(state)
        self.assertEqual((result.status, result.failures), ("stopped", 0))

    async def test_crash_is_reported_as_failed(self):
        state = make_state(self.store)

        async def boom(_s, _w):
            raise RuntimeError("verifier exploded")

        engine = GoalEngine(self.store, FakeDriver([]), verifier=boom, sleep=no_sleep)
        result = await engine.run(state)
        self.assertEqual(result.status, "failed")
        self.assertIn("verifier exploded", result.detail)

    async def test_cancellation_keeps_state_resumable(self):
        state = make_state(self.store)

        async def hang(_s):
            await asyncio.sleep(60)

        class Hanging(FakeDriver):
            async def run_iteration(self, s, p):
                await asyncio.sleep(60)

        task = asyncio.create_task(engine_for(self.store, Hanging([])).run(state))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.load(state.id).status, "running")
        self.assertEqual(self.store.load(state.id).iteration, 1)

    def test_prompt_contains_rules_and_feedback(self):
        state = make_state(self.store, checks=["pytest -q"])
        state.iteration, state.last_check_summary, state.no_progress = 3, "- `pytest -q` → 失败", 2
        text = build_prompt(state)
        for needle in ("make tests green", "pytest -q", state.branch, "GOAL_STATUS:", "不要 push", "已连续 2 轮"):
            self.assertIn(needle, text)


class CheckRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_checks_run_only_inside_task_worktrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wt = root / "worktrees" / "job1"
            wt.mkdir(parents=True)
            settings = SimpleNamespace(codex_task_root=root)
            state = GoalState(id="b" * 12, scope="s", objective="x" * 8, checks=["test -f marker", "exit 3"])
            (wt / "marker").write_text("1")
            results = await goal_loop.run_checks(settings, state, str(wt))
            self.assertEqual([r.ok for r in results], [True, False])
            self.assertEqual(results[1].code, 3)
            outside = await goal_loop.run_checks(settings, state, str(root))
            self.assertTrue(all(not r.ok for r in outside))
            self.assertIn("not run", outside[0].output)
            self.assertEqual(await goal_loop.run_checks(settings, GoalState(id="c" * 12, scope="s", objective="x" * 8), str(wt)), [])

    async def test_check_timeout_is_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = await goal_loop._run("sleep 5", tmp, {"PATH": "/bin:/usr/bin"}, 0.2)
            self.assertIsNone(code)
            self.assertIn("timeout", out)

    async def test_fingerprint_changes_with_worktree_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            await goal_loop._run("git init -q && git config user.email a@b && git config user.name n && git commit -q --allow-empty -m i", tmp, {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}, 30)
            first = await goal_loop.worktree_fingerprint(tmp, [])
            self.assertEqual(first, await goal_loop.worktree_fingerprint(tmp, []))
            (Path(tmp) / "f.txt").write_text("x")
            self.assertNotEqual(first, await goal_loop.worktree_fingerprint(tmp, []))


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = SimpleNamespace(codex_memory_root=Path(self.tmp.name), codex_task_root=Path(self.tmp.name))
        self.msg = SimpleNamespace(channel="web", chat_id="topic-1", operator_id="web-console", thread_id=None)

    async def asyncTearDown(self):
        for task in list(goal_loop._TASKS.values()):
            task.cancel()
        await asyncio.sleep(0)

    async def test_start_status_stop_lifecycle(self):
        gate = asyncio.Event()

        class Slow(FakeDriver):
            async def run_iteration(self, state, prompt):
                await gate.wait()
                return IterationResult(False, error="cancelled")

        driver = Slow([])
        started = await handle_goal_command(
            self.msg, self.settings, parse_goal_command("/goal ship the feature --check 'true'"), lambda s: driver)
        self.assertIn("目标模式已开启", started)
        await asyncio.sleep(0.05)
        status = await handle_goal_command(self.msg, self.settings, parse_goal_command("/goal status"), lambda s: driver)
        self.assertIn("进行中", status)
        again = await handle_goal_command(self.msg, self.settings, parse_goal_command("/goal another one"), lambda s: driver)
        self.assertIn("已有进行中的目标", again)
        stop = await handle_goal_command(self.msg, self.settings, parse_goal_command("/goal stop"), lambda s: driver)
        self.assertIn("正在停止", stop)
        self.assertEqual(driver.cancelled, 1)
        gate.set()
        await asyncio.wait_for(asyncio.gather(*goal_loop._TASKS.values()), 5)
        final = goal_loop._store(self.settings).latest_for(goal_loop.scope_of(self.msg))
        self.assertEqual(final.status, "stopped")
        self.assertIn("已按你的要求停止", driver.notes[-1])

    async def test_other_chat_is_not_affected_and_empty_stop(self):
        other = SimpleNamespace(channel="web", chat_id="topic-2", operator_id="web-console", thread_id=None)
        self.assertIn("没有进行中", await handle_goal_command(other, self.settings, parse_goal_command("/goal stop"), lambda s: None))
        self.assertIn("没有目标", await handle_goal_command(other, self.settings, parse_goal_command("/goal status"), lambda s: None))

    async def test_resume_restarts_running_goal_once(self):
        store = goal_loop._store(self.settings)
        state = make_state(store, id="d" * 12)
        state.ctx = {"channel": "web", "chat_id": "topic-1"}
        store.save(state)
        done = make_state(store, id="e" * 12, status="done")
        driver = FakeDriver([IterationResult(True, _verdict("done"), "/wt"),
                             IterationResult(True, _verdict("done"), "/wt")])
        self.assertEqual(goal_loop.resume_all(self.settings, lambda s: driver), 1)
        self.assertEqual(goal_loop.resume_all(self.settings, lambda s: driver), 0)  # already running
        await asyncio.wait_for(asyncio.gather(*goal_loop._TASKS.values()), 5)
        self.assertEqual(store.load("d" * 12).status, "done")
        self.assertEqual(store.load(done.id).status, "done")


class QueueDriverTests(unittest.IsolatedAsyncioTestCase):
    """The real driver against a faked queue: polling, result mapping, cancellation."""

    def _driver(self, snapshots, submit_result=None, timeout=60.0):
        from unittest.mock import AsyncMock, MagicMock, patch
        queue = MagicMock()
        queue.job_snapshot.side_effect = list(snapshots)
        queue.cancel = AsyncMock(return_value=(True, "cancelled"))
        submit = AsyncMock(return_value=submit_result or (True, "queued", SimpleNamespace(id="q1")))
        self.patches = [patch("handlers.job_queue.get_job_queue", return_value=queue),
                        patch("handlers.jobs.submit_codex_job", submit)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        inner = SimpleNamespace()
        sent = []

        async def reply(msg, text):
            sent.append(text)

        inner.reply = reply
        inner.send_new = reply
        msg = SimpleNamespace(channel="web", chat_id="c", operator_id="o", message_id="m", text="", thread_id=None)
        import dataclasses
        from channel.types import InboundMessage
        msg = InboundMessage(channel="web", operator_id="o", chat_id="c", message_id="m", text="", chat_type="p2p")

        async def notify(_t):
            return None

        driver = goal_loop.QueueDriver(msg, object(), SimpleNamespace(), lambda p: inner, notify,
                                       poll_seconds=0.01, iteration_timeout=timeout)
        return driver, queue, submit, inner

    async def test_completed_job_returns_text_and_worktree(self):
        driver, queue, submit, inner = self._driver([
            {"state": "running"},
            {"state": "completed", "metadata": {"worktree_path": "/w/1"}}])

        async def fake_submit(msg, port, runner, mode=None, prompt=None, wait=False):
            await port.send_new(msg, "summary text\nGOAL_STATUS: done")
            return True, "queued", SimpleNamespace(id="q1")

        submit.side_effect = fake_submit
        state = GoalState(id="f" * 12, scope="s", objective="x" * 8)
        result = await driver.run_iteration(state, "do it")
        self.assertTrue(result.ok)
        self.assertEqual((result.worktree, result.job_id), ("/w/1", "q1"))
        self.assertIn("GOAL_STATUS: done", result.text)
        mode = submit.await_args.kwargs["mode"]
        self.assertEqual(mode.value, "fix")  # always an isolated worktree job

    async def test_failed_job_reports_error(self):
        driver, *_ = self._driver([{"state": "failed", "error": "provider down", "metadata": {}}])
        result = await driver.run_iteration(GoalState(id="f" * 12, scope="s", objective="x" * 8), "p")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "provider down")

    async def test_rate_limit_is_transient(self):
        driver, *_ = self._driver([], submit_result=(False, "rate limit", None))
        result = await driver.run_iteration(GoalState(id="f" * 12, scope="s", objective="x" * 8), "p")
        self.assertTrue(result.transient)

    async def test_iteration_timeout_cancels_the_job(self):
        driver, queue, *_ = self._driver([{"state": "running"}] * 1000, timeout=0.05)
        result = await driver.run_iteration(GoalState(id="f" * 12, scope="s", objective="x" * 8), "p")
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)
        queue.cancel.assert_awaited_once_with("q1")


class WebChatRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_goal_command_is_handled_without_a_codex_job(self):
        import queue
        from unittest.mock import AsyncMock, patch
        from channel.types import InboundMessage
        from tests.test_pi_backend import _make_settings
        from web_chat import WebChatPort, run_web_chat
        with tempfile.TemporaryDirectory() as root:
            settings = _make_settings(Path(root))
            events = queue.Queue()
            text = "/goal status"
            msg = InboundMessage(channel="web", operator_id="web-console", chat_id="goal-ui", message_id="m1", text=text)
            port = WebChatPort(events, settings, "web:web-console:goal-ui", prompt=text)
            with patch("handlers.jobs.handle_codex_job", new_callable=AsyncMock) as job:
                await run_web_chat(msg, port, settings, SimpleNamespace(name="pi"), text)
            job.assert_not_awaited()
            emitted = list(events.queue)
            self.assertTrue(any(n == "message" and "没有目标" in d.get("text", "") for n, d in emitted))
            self.assertEqual(emitted[-1], ("done", {"outcome": "answered"}))


if __name__ == "__main__":
    unittest.main()
