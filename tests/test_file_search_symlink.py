"""files.search must not quote a file a symlink points to outside the allowed roots."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from personal_tools import file_search


class FileSearchSymlinkTest(unittest.TestCase):
    def test_symlink_out_of_root_is_not_searched(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, outside = Path(raw) / "root", Path(raw) / "outside"
            root.mkdir(); outside.mkdir()
            (outside / "id_rsa.md").write_text("SECRET-KEY-MATERIAL needle\n")
            (root / "notes.md").symlink_to(outside / "id_rsa.md")
            (root / "plain.md").write_text("ordinary needle\n")
            settings = type("S", (), {
                "file_search_extensions": ".md", "file_search_max_results": 10,
                "file_search_max_file_bytes": 100_000})()
            with patch.object(file_search, "_get_allowed_roots", return_value=[root]):
                results = file_search._search_files_internal(settings, "needle")
        text = repr(results)
        self.assertNotIn("SECRET-KEY-MATERIAL", text)
        self.assertIn("plain.md", text)


if __name__ == "__main__":
    unittest.main()
