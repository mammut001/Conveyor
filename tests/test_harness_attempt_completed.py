"""Regression tests for scripts.harness_common.attempt_completed.

The Pi backend (runner/pi_agent.py) writes attempt log records as
``{"kind": "assistant.completed", ...}`` and never emits the Codex CLI
``{"type": "turn.completed"}`` event. attempt_completed must recognise both so
the doctor "latest completed" check keeps passing after the switch to Pi.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from harness_common import attempt_completed  # noqa: E402


def _write_attempt(records: list[str]) -> Path:
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    )
    try:
        for record in records:
            tmp.write(record + "\n")
    finally:
        tmp.close()
    return Path(tmp.name)


class AttemptCompletedTests(unittest.TestCase):
    def _run(self, records: list[str]) -> bool:
        path = _write_attempt(records)
        try:
            return attempt_completed(path)
        finally:
            path.unlink(missing_ok=True)

    def test_codex_turn_completed(self) -> None:
        records = [
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "tool.call"}),
            json.dumps({"type": "turn.completed"}),
        ]
        self.assertTrue(self._run(records))

    def test_pi_assistant_completed(self) -> None:
        records = [
            json.dumps({"kind": "tool.started", "payload": {"name": "bash", "status": "running"}}),
            json.dumps({"kind": "assistant.completed", "payload": {"text": "done"}}),
        ]
        self.assertTrue(self._run(records))

    def test_only_tool_events_is_false(self) -> None:
        records = [
            json.dumps({"kind": "tool.started", "payload": {"name": "read"}}),
            json.dumps({"kind": "tool.completed", "payload": {"status": "completed"}}),
            json.dumps({"type": "message_update"}),
        ]
        self.assertFalse(self._run(records))

    def test_agent_error_is_false(self) -> None:
        records = [
            json.dumps({"kind": "tool.started", "payload": {"name": "bash"}}),
            json.dumps({"kind": "agent.error", "payload": {"text": "boom"}}),
        ]
        self.assertFalse(self._run(records))

    def test_garbage_and_non_dict_lines_are_tolerated(self) -> None:
        records = [
            "not json at all {{{",
            json.dumps(["a", "list", "record"]),
            json.dumps("just a string"),
            json.dumps(42),
            "",
            json.dumps({"kind": "assistant.completed", "payload": {"text": "ok"}}),
        ]
        self.assertTrue(self._run(records))

    def test_garbage_only_is_false(self) -> None:
        records = ["garbage", json.dumps(["only", "lists"]), json.dumps("strings")]
        self.assertFalse(self._run(records))


if __name__ == "__main__":
    unittest.main()
