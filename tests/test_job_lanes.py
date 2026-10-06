"""Lanes: jobs of different agents may overlap (phase 5)."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import agents
import job_lanes
from agents import AgentStore
from channel.types import InboundMessage
from handlers.job_queue import JobQueue, QueueJobState
from runner import CodexRunner


def _settings(root: Path, **overrides):
    from config import Settings

    base = Settings(
        telegram_bot_token="test-token", telegram_allowed_user_id=1, codex_workspace_root=root,
        codex_bin="codex", codex_task_root=root / "tasks", codex_model=None, codex_timeout_seconds=30,
        codex_retry_429_delays_seconds=(), telegram_progress_seconds=1, codex_memory_root=root,
        user_timezone="UTC", agents_enabled=True, agent_parallel_jobs=2,
    )
    return replace(base, **overrides)


class _Port:
    async def reply(self, _msg, _text):
        return None


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = _settings(self.root)
        store = AgentStore(self.settings)
        self.a = store.create({"name": "A", "workspace_path": "/srv/a"})
        self.b = store.create({"name": "B", "workspace_path": "/srv/b"})
        self.plain = store.create({"name": "NoFolder"})
        self.store = store
        self.runner = CodexRunner(self.settings)

    def msg(self, agent=None, channel="web") -> InboundMessage:
        chat = agents.chat_id_for(agent["id"]) if agent else "42"
        return InboundMessage(channel=channel, operator_id="web-console", chat_id=chat, message_id=None, text="t")

    def queue(self, settings=None) -> JobQueue:
        queue = JobQueue(max_length=20)
        queue.configure(settings or self.settings, self.runner, recover=False)
        return queue


class LaneTests(Case):
    def test_only_agents_with_their_own_folder_get_a_lane(self) -> None:
        lane = lambda agent, channel="web": job_lanes.lane_for_chat(  # noqa: E731
            self.settings, channel, agents.chat_id_for(agent["id"]) if agent else "42")
        self.assertEqual(lane(self.a), self.a["id"])
        self.assertEqual(lane(self.b), self.b["id"])
        self.assertEqual(lane(self.plain), "default")           # shares the configured workspace
        self.assertEqual(lane(None, "telegram"), "default")
        one = _settings(self.root, agent_parallel_jobs=1)
        self.assertEqual(job_lanes.lane_for_chat(one, "web", agents.chat_id_for(self.a["id"])), "default")

    def test_each_lane_has_its_own_runner(self) -> None:
        mine = job_lanes.runner_for_lane(self.runner, self.a["id"])
        self.assertIsNot(mine, self.runner)
        self.assertEqual((mine.lane, self.runner.lane), (self.a["id"], "default"))
        self.assertIs(job_lanes.runner_for_lane(self.runner, self.a["id"]), mine)       # stable
        self.assertIs(job_lanes.runner_for_lane(mine, "default"), self.runner)           # from any lane
        self.assertIs(job_lanes.runner_for_lane(mine, self.b["id"]), job_lanes.runner_for_lane(self.runner, self.b["id"]))
        self.assertEqual(set(job_lanes.lane_runners(mine)), {"default", self.a["id"], self.b["id"]})

    def test_a_test_double_stays_in_one_lane(self) -> None:
        double = mock.Mock()
        self.assertIs(job_lanes.runner_for_lane(double, "default"), double)
        self.assertIs(job_lanes.runner_for_lane(double, "x1"), double)
        self.assertIs(job_lanes.runner_for_chat(double, mock.Mock(), "web", "agent-x1"), double)

    def test_running_job_is_found_on_its_lane(self) -> None:
        mine = job_lanes.runner_for_lane(self.runner, self.a["id"])
        mine.current_job = mock.Mock(external_id="q7")
        self.assertIs(job_lanes.runner_of_job(self.runner, "q7"), mine)
        self.assertIsNone(job_lanes.runner_of_job(self.runner, "q8"))


class QueueTests(Case):
    def scenario(self, settings=None):
        queue = self.queue(settings)
        started: list[str] = []

        async def run():
            ids = {}
            for name, agent in (("a1", self.a), ("a2", self.a), ("b1", self.b), ("d1", None)):
                _, _, job = await queue.enqueue("run", name, self.msg(agent), _Port(), self.runner)
                ids[name] = job.id
            return ids

        ids = asyncio.run(run())
        names = {value: key for key, value in ids.items()}

        async def start(job):
            started.append(names[job.id])

        queue.set_start_callback(start)
        return queue, ids, names, started

    def test_two_agents_run_side_by_side_but_one_agent_does_not_overlap_itself(self) -> None:
        queue, ids, names, started = self.scenario()

        async def run():
            first = await queue.dequeue(require_idle=True)
            second = await queue.dequeue(require_idle=True)
            third = await queue.dequeue(require_idle=True)
            order = [names[first.id], names[second.id], third]
            # a2 is next in line but its lane is busy; it must not start.
            self.assertFalse(queue.can_start(ids["a2"]))
            await queue.on_job_completed(queue_job_id=ids["a1"])      # lane A frees up
            await queue.on_job_completed(queue_job_id=ids["b1"])      # lane B frees up
            return order

        order = asyncio.run(run())
        self.assertEqual(order, ["a1", "b1", None])                    # limit of 2 reached
        self.assertEqual(started, ["a2", "d1"])                        # freed lanes are refilled in order

    def test_with_a_limit_of_one_everything_is_strictly_one_at_a_time(self) -> None:
        queue, ids, names, started = self.scenario(_settings(self.root, agent_parallel_jobs=1))

        async def run():
            first = await queue.dequeue(require_idle=True)
            second = await queue.dequeue(require_idle=True)
            await queue.on_job_completed(queue_job_id=first.id)
            return names[first.id], second

        self.assertEqual(asyncio.run(run()), ("a1", None))
        self.assertEqual(started, ["a2"])

    def test_a_failed_start_marks_that_job_not_whichever_is_running(self) -> None:
        queue, ids, names, started = self.scenario()

        async def run():
            await queue.dequeue(require_idle=True)        # a1
            await queue.dequeue(require_idle=True)        # b1
            await queue.mark_running_failed("boom", ids["b1"])
            return (await queue.get_job(ids["a1"])).state, (await queue.get_job(ids["b1"])).state

        self.assertEqual(asyncio.run(run()), (QueueJobState.RUNNING, QueueJobState.FAILED))


class RecordTests(Case):
    def write(self, job_id: str, lane: str | None, state: str = "completed") -> None:
        logs = self.settings.codex_task_root / "logs" / job_id
        logs.mkdir(parents=True)
        meta = {"id": job_id, "state": state, "mode": "run"}
        if lane is not None:
            meta["lane"] = lane
        (logs / "job.json").write_text(json.dumps(meta), encoding="utf-8")

    def test_each_runner_sees_its_own_lanes_jobs_as_last_and_running(self) -> None:
        self.write("20260101-000001-aaaaaaaa", None)                       # written before lanes existed
        self.write("20260101-000002-bbbbbbbb", self.a["id"], state="running")
        mine = job_lanes.runner_for_lane(self.runner, self.a["id"])
        other = job_lanes.runner_for_lane(self.runner, self.b["id"])
        self.assertEqual([r.id for r in mine.job_records(10, lane=mine.lane)], ["20260101-000002-bbbbbbbb"])
        self.assertEqual([r.id for r in self.runner.job_records(10, lane="default")], ["20260101-000001-aaaaaaaa"])
        self.assertEqual(other.job_records(10, lane=other.lane), [])
        # Cleanup and audits still see everything.
        self.assertEqual(len(self.runner.job_records(10)), 2)
        self.assertEqual(mine._last_job_id(), "20260101-000002-bbbbbbbb")
        self.assertEqual(self.runner._last_job_id(), "20260101-000001-aaaaaaaa")


if __name__ == "__main__":
    unittest.main()
