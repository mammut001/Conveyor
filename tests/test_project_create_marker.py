"""R19: the `.conveyor-project-create` rollback marker must not stay in the owner's repo."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conveyor.agents import supervisor_service as svc
from tests.test_supervisor_api import _make_settings

MARKER = svc._PROJECT_CREATE_MARKER


class ProjectCreateMarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-marker-")
        self.root = Path(self._tmp.name)
        self.settings = _make_settings(self.root / "env")
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _project_dir(self, name: str) -> Path:
        return Path(self.settings.codex_task_root) / "projects" / name

    def test_successful_create_leaves_no_marker(self) -> None:
        result = svc.create_managed_project(self.settings, name="marker-ok")
        path = Path(result["repository"]["path"])
        self.assertTrue(path.is_dir())
        self.assertFalse((path / MARKER).exists())

    def test_failed_create_still_rolls_back(self) -> None:
        with patch.object(svc, "register_repository", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                svc.create_managed_project(self.settings, name="marker-fail")
        self.assertFalse(self._project_dir("marker-fail").exists())

    def _registered_with_marker(self, name: str, content: str | None = None, symlink: bool = False) -> Path:
        result = svc.create_managed_project(self.settings, name=name)
        path = Path(result["repository"]["path"])
        st = os.lstat(path)
        marker = path / MARKER
        if symlink:
            target = self.root / f"{name}-target"
            target.write_text(svc._project_create_marker_text((st.st_dev, st.st_ino)), encoding="utf-8")
            os.symlink(target, marker)
        else:
            marker.write_text(content if content is not None else svc._project_create_marker_text((st.st_dev, st.st_ino)), encoding="utf-8")
        return marker

    def test_cleanup_removes_only_matching_regular_markers(self) -> None:
        matching = self._registered_with_marker("old-one")
        wrong = self._registered_with_marker("old-two", content="1:2\n")
        linked = self._registered_with_marker("old-three", symlink=True)
        unregistered = self._project_dir("loose")
        unregistered.mkdir()
        st = os.lstat(unregistered)
        (unregistered / MARKER).write_text(svc._project_create_marker_text((st.st_dev, st.st_ino)), encoding="utf-8")

        self.assertEqual(svc.clear_stale_project_create_markers(self.settings), 1)
        self.assertFalse(matching.exists())
        self.assertTrue(wrong.exists())
        self.assertTrue(os.path.islink(linked))
        self.assertTrue((unregistered / MARKER).exists())

    def test_cleanup_never_raises_and_never_creates_projects_root(self) -> None:
        with patch.object(svc, "list_repositories", side_effect=RuntimeError("db down")):
            self.assertEqual(svc.clear_stale_project_create_markers(self.settings), 0)
        fresh = _make_settings(self.root / "fresh")
        self.assertEqual(svc.clear_stale_project_create_markers(fresh), 0)
        self.assertFalse((Path(fresh.codex_task_root) / "projects").exists())

    def test_startup_recovery_survives_a_cleanup_failure(self) -> None:
        from conveyor.agents import agent_supervisor as ags
        with patch.object(svc, "clear_stale_project_create_markers", side_effect=RuntimeError("boom")):
            self.assertIsInstance(ags.recover_on_startup(self.settings), list)


if __name__ == "__main__":
    unittest.main()
