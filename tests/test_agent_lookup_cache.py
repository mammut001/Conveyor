"""lookup_cache() lets one request reuse agent rows instead of re-querying."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from conveyor.agents import agents
from conveyor.agents.agents import AgentStore, lookup_cache
from tests.test_agents import _settings


class LookupCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = _settings(Path(tmp.name))
        self.store = AgentStore(self.settings)
        self.store.list()  # creates the default agent

    def count_connects(self):
        return mock.patch.object(AgentStore, "_connect", autospec=True, side_effect=AgentStore._connect)

    def test_outside_the_cache_every_get_queries(self) -> None:
        with self.count_connects() as connect:
            self.store.get("default")
            self.store.get("default")
        self.assertEqual(connect.call_count, 2)

    def test_inside_the_cache_repeat_gets_reuse_the_row(self) -> None:
        with self.count_connects() as connect, lookup_cache():
            first = self.store.get("default")
            second = AgentStore(self.settings).get("default")
            self.assertIsNone(self.store.get("abcdef1234"))
            self.assertIsNone(self.store.get("abcdef1234"))
        self.assertEqual(connect.call_count, 2)  # default once, missing once
        self.assertEqual(first, second)
        self.assertEqual(first["id"], "default")

    def test_cached_rows_are_copies(self) -> None:
        with lookup_cache():
            row = self.store.get("default")
            row["name"] = "changed by caller"
            self.assertNotEqual(self.store.get("default")["name"], "changed by caller")

    def test_cache_ends_with_the_block_and_nests(self) -> None:
        with lookup_cache():
            with lookup_cache():
                self.store.get("default")
            self.assertIsNotNone(agents._GET_CACHE.get())
        self.assertIsNone(agents._GET_CACHE.get())


if __name__ == "__main__":
    unittest.main()
