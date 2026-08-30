from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_events import AgentEvent, EventStore, SCHEMA_VERSION, emit_codex_event


class AgentEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.temp.name) / "events.sqlite3", retention_per_job=100)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_serialization_round_trip_and_schema(self) -> None:
        event = self.store.append(
            "tool.started", "job-1", {"name": "shell"},
            session_id="session-1", tool_call_id="call-1", correlation_id="corr-1",
        )
        restored = AgentEvent.from_dict(json.loads(event.to_json()))
        self.assertEqual(restored, event)
        self.assertEqual(restored.schema_version, SCHEMA_VERSION)
        self.assertEqual(restored.tool_call_id, "call-1")
        self.assertEqual(restored.correlation_id, "corr-1")

    def test_sequence_unique_ids_replay_and_dedup_contract(self) -> None:
        events = [self.store.append("assistant.delta", "job-1", {"text": str(i)}) for i in range(5)]
        self.assertEqual([item.sequence for item in events], [1, 2, 3, 4, 5])
        self.assertEqual(len({item.event_id for item in events}), 5)
        replay = self.store.list("job-1", after_sequence=2)
        self.assertEqual([item.sequence for item in replay], [3, 4, 5])
        client_dedup = {item.event_id: item for item in [*events, *replay]}
        self.assertEqual(len(client_dedup), 5)

    def test_payload_is_redacted_and_bounded(self) -> None:
        event = self.store.append(
            "tool.output", "job-1",
            {"authorization": "Bearer abcdefghijklmnopqrstuvwxyz012345", "text": "sk-" + "x" * 30},
        )
        rendered = event.to_json()
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", rendered)
        self.assertNotIn("sk-xxxxxxxx", rendered)

    def test_retention_is_bounded(self) -> None:
        for index in range(130):
            self.store.append("assistant.delta", "job-1", {"text": str(index)})
        replay = self.store.list("job-1", limit=500)
        self.assertEqual(len(replay), 100)
        self.assertEqual(replay[-1].sequence, 130)


if __name__ == "__main__":
    unittest.main()
