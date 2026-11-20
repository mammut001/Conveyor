"""Background tasks are held until they finish (the loop only keeps weak references)."""
from __future__ import annotations

import asyncio
import gc
import unittest

from runner.operators import run


class SpawnBackgroundTests(unittest.TestCase):
    def test_task_is_held_while_running_and_released_when_done(self) -> None:
        async def scenario() -> None:
            gate = asyncio.Event()
            finished = []

            async def work() -> None:
                await gate.wait()
                finished.append(True)

            task = run.spawn_background(work())
            task_id = id(task)
            del task
            gc.collect()
            self.assertTrue(any(id(t) == task_id for t in run._BACKGROUND_TASKS))
            gate.set()
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertEqual(finished, [True])
            self.assertFalse(any(id(t) == task_id for t in run._BACKGROUND_TASKS))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
