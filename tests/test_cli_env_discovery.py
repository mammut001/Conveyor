"""runner.cli._find_env_file walked into a NameError (MEMO_ENV_SKIP_DIRS)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from runner.cli import _find_env_file


class CliEnvDiscoveryTest(unittest.TestCase):
    def test_stops_at_a_virtualenv_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".env").write_text("X=1\n")
            inner = root / ".venv" / "lib"
            inner.mkdir(parents=True)
            self.assertIsNone(_find_env_file(inner))
            self.assertEqual(_find_env_file(root), root / ".env")

    def test_finds_env_in_a_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".env").write_text("X=1\n")
            (root / "a" / "b").mkdir(parents=True)
            self.assertEqual(_find_env_file(root / "a" / "b"), root / ".env")


if __name__ == "__main__":
    unittest.main()
