"""R12: detect missing Pi code-search tools (rg, fd) on the child PATH."""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conveyor.agents import agent_supervisor as ags
from conveyor.agents import supervisor_service as svc


def _exe(directory: Path, name: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class MissingSearchToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        ags._MISSING_TOOLS_CACHE = (0.0, [])
        self.addCleanup(setattr, ags, "_MISSING_TOOLS_CACHE", (0.0, []))
        self.tmp = Path(tempfile.mkdtemp())

    def _check(self, *names: str) -> list[str]:
        for name in names:
            _exe(self.tmp, name)
        ags._MISSING_TOOLS_CACHE = (0.0, [])
        with patch.object(ags, "_child_env", return_value={"PATH": str(self.tmp)}):
            return ags.missing_search_tools(object())

    def test_both_present(self) -> None:
        self.assertEqual(self._check("rg", "fd"), [])

    def test_fdfind_counts_as_fd(self) -> None:
        self.assertEqual(self._check("rg", "fdfind"), [])

    def test_rg_missing(self) -> None:
        self.assertEqual(self._check("fd"), ["rg"])

    def test_both_missing(self) -> None:
        self.assertEqual(self._check(), ["rg", "fd"])

    def test_result_is_cached(self) -> None:
        self.assertEqual(self._check(), ["rg", "fd"])
        _exe(self.tmp, "rg")
        _exe(self.tmp, "fd")
        with patch.object(ags, "_child_env", return_value={"PATH": str(self.tmp)}):
            self.assertEqual(ags.missing_search_tools(object()), ["rg", "fd"])

    def test_child_env_failure_falls_back_to_process_path(self) -> None:
        _exe(self.tmp, "rg")
        _exe(self.tmp, "fdfind")
        with patch.object(ags, "_child_env", side_effect=RuntimeError("boom")), \
                patch.dict(os.environ, {"PATH": str(self.tmp)}):
            self.assertEqual(ags.missing_search_tools(object()), [])


class CapabilitiesFieldTests(unittest.TestCase):
    def test_capabilities_source_includes_missing_search_tools(self) -> None:
        import inspect
        self.assertIn('"missing_search_tools"', inspect.getsource(svc.get_capabilities))


if __name__ == "__main__":
    unittest.main()
