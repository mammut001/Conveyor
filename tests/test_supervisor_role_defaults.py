"""Role executor defaults pick a usable executor instead of an unavailable one.

A fresh install has no ``claude`` CLI, so defaulting every Supervisor role to
Claude Code blocks the owner with a red English error while the built-in Pi
executor sits right there. These tests pin the automatic default order and the
rule that explicit configuration always wins.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conveyor.agents import agent_supervisor as ags
from conveyor.agents import agent_supervisor_pool as pool
from conveyor.agents import supervisor_service as svc

WORKTREE_ROLES = ("analyst", "implementer", "reviewer")

# Every supervisor executor knob: cleared so the tests never inherit the host's
# configuration (an installed CLI or a set role variable must not leak in).
_SUPERVISOR_ENV_KEYS = (
    "CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTATION_EXECUTOR",
    "CONVEYOR_SUPERVISOR_IMPLEMENTATION_EXECUTOR",
    "CONVEYOR_AGENT_SUPERVISOR_ANALYST_EXECUTOR",
    "CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTER_EXECUTOR",
    "CONVEYOR_AGENT_SUPERVISOR_REVIEWER_EXECUTOR",
    "CONVEYOR_AGENT_SUPERVISOR_COORDINATOR_EXECUTOR",
)


def _write_executable(path: Path) -> Path:
    path.write_text(f"#!{sys.executable}\nimport sys; sys.exit(0)\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _make_settings(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        codex_memory_root=root,
        codex_task_root=root,
        agent_supervisor_enabled=True,
        agent_supervisor_implementation_executor=None,
        agent_supervisor_coordinator_executor="chat",
        agent_supervisor_analyst_executor=None,
        agent_supervisor_implementer_executor=None,
        agent_supervisor_reviewer_executor=None,
    )


class _NoExecutors:
    """Isolate availability: no external CLI, no pool worker, no inherited env."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def __enter__(self):
        self._saved = {key: os.environ.pop(key, None) for key in _SUPERVISOR_ENV_KEYS}
        self._patch = patch.dict(os.environ, {
            "CONVEYOR_CLAUDE_BIN": str(self.root / "no-claude-here"),
            "CONVEYOR_COMMAND_CODE_BIN": str(self.root / "no-command-code-here"),
            "CONVEYOR_ANTIGRAVITY_BIN": str(self.root / "no-agy-here"),
            "CONVEYOR_PI_BIN": str(self.root / "no-pi-here"),
        })
        self._patch.start()
        # The pool reads the workers table; patch it so the answer never depends
        # on which CLIs this machine happens to have installed.
        self._pool = patch.object(pool.PoolExecutor, "available", return_value=(False, pool.NO_WORKER_REASON))
        self._pool.start()
        return self

    def __exit__(self, *exc):
        self._pool.stop()
        self._patch.stop()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


class RoleExecutorDefaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sup-role-default-")
        self.root = Path(self._tmp.name).resolve()
        self.settings = _make_settings(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_only_pi_available_defaults_every_role_to_pi(self) -> None:
        fake_pi = _write_executable(self.root / "fake-pi")
        with _NoExecutors(self.root), patch.dict(os.environ, {"CONVEYOR_PI_BIN": str(fake_pi)}):
            roles = ags.load_roles(self.settings)
            self.assertEqual(ags.implementation_executor_name(self.settings), "pi")
        for name in WORKTREE_ROLES:
            self.assertEqual(roles[name].executor, "pi", name)

    def test_available_pool_wins_over_the_other_executors(self) -> None:
        fake_pi = _write_executable(self.root / "fake-pi")
        with _NoExecutors(self.root), \
                patch.dict(os.environ, {"CONVEYOR_PI_BIN": str(fake_pi)}), \
                patch.object(pool.PoolExecutor, "available", return_value=(True, "ok")):
            roles = ags.load_roles(self.settings)
        for name in WORKTREE_ROLES:
            self.assertEqual(roles[name].executor, "pool", name)

    def test_explicit_claude_configuration_wins_even_when_unavailable(self) -> None:
        with _NoExecutors(self.root), patch.dict(
            os.environ, {"CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTATION_EXECUTOR": "claude"}
        ):
            roles = ags.load_roles(self.settings)
            sources = ags.role_executor_sources(self.settings)
            ok, reason = ags.get_executor(self.settings, "claude").available()
        for name in WORKTREE_ROLES:
            self.assertEqual(roles[name].executor, "claude", name)
            self.assertEqual(sources[name], "explicit", name)
        self.assertFalse(ok)
        self.assertIn("没有安装", reason)

    def test_per_role_env_beats_the_automatic_default(self) -> None:
        fake_pi = _write_executable(self.root / "fake-pi")
        with _NoExecutors(self.root), patch.dict(
            os.environ,
            {"CONVEYOR_PI_BIN": str(fake_pi), "CONVEYOR_AGENT_SUPERVISOR_ANALYST_EXECUTOR": "claude"},
        ):
            roles = ags.load_roles(self.settings)
        self.assertEqual(roles["analyst"].executor, "claude")
        self.assertEqual(roles["implementer"].executor, "pi")
        self.assertEqual(roles["reviewer"].executor, "pi")

    def test_supervisor_roles_json_entry_wins(self) -> None:
        fake_pi = _write_executable(self.root / "fake-pi")
        (self.root / "supervisor_roles.json").write_text(
            json.dumps({"reviewer": {"executor": "claude"}}), encoding="utf-8"
        )
        with _NoExecutors(self.root), patch.dict(os.environ, {"CONVEYOR_PI_BIN": str(fake_pi)}):
            roles = ags.load_roles(self.settings)
        self.assertEqual(roles["reviewer"].executor, "claude")
        self.assertEqual(roles["analyst"].executor, "pi")

    def test_nothing_available_keeps_claude_with_its_reason(self) -> None:
        with _NoExecutors(self.root):
            roles = ags.load_roles(self.settings)
            ok, reason = ags.get_executor(self.settings, "claude").available()
        for name in WORKTREE_ROLES:
            self.assertEqual(roles[name].executor, "claude", name)
        self.assertFalse(ok)
        self.assertIn("没有安装 Claude Code", reason)

    def test_role_executor_sources_mark_automatic_defaults(self) -> None:
        fake_pi = _write_executable(self.root / "fake-pi")
        with _NoExecutors(self.root), patch.dict(os.environ, {"CONVEYOR_PI_BIN": str(fake_pi)}):
            sources = ags.role_executor_sources(self.settings)
        self.assertEqual(sources["analyst"], "auto")
        self.assertEqual(sources["implementer"], "auto")
        self.assertEqual(sources["reviewer"], "auto")
        # The coordinator's executor comes from settings, not the automatic default.
        self.assertEqual(sources["coordinator"], "explicit")


class CapabilityAutoSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sup-role-caps-")
        self.root = Path(self._tmp.name).resolve()
        self.settings = _make_settings(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _roles(self, caps: dict) -> dict:
        return {name: caps["roles"][name] for name in WORKTREE_ROLES}

    def test_capabilities_expose_auto_selected_roles(self) -> None:
        fake_pi = _write_executable(self.root / "fake-pi")
        with _NoExecutors(self.root), patch.dict(os.environ, {"CONVEYOR_PI_BIN": str(fake_pi)}):
            caps = svc.get_capabilities(self.settings)
        roles = self._roles(caps)
        for name, entry in roles.items():
            self.assertTrue(entry["auto_selected"], name)
            self.assertEqual(entry["executor"], "pi")
            # Binary is present but no provider/key is configured in this fixture.
            self.assertFalse(entry["available"], name)
            self.assertTrue(entry.get("reason"), name)

    def test_capabilities_do_not_claim_auto_for_explicit_roles(self) -> None:
        with _NoExecutors(self.root), patch.dict(
            os.environ, {"CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTATION_EXECUTOR": "claude"}
        ):
            caps = svc.get_capabilities(self.settings)
        for name, entry in self._roles(caps).items():
            self.assertFalse(entry["auto_selected"], name)
            self.assertEqual(entry["executor"], "claude")
            self.assertFalse(entry["available"], name)
            self.assertIn("没有安装 Claude Code", entry["reason"])

    def test_settings_executor_is_still_explicit(self) -> None:
        # An embedder that names the executor in Settings keeps it, unavailable or not.
        settings = SimpleNamespace(
            **{**vars(self.settings), "agent_supervisor_implementation_executor": "antigravity"}
        )
        with _NoExecutors(self.root):
            roles = ags.load_roles(settings)
            sources = ags.role_executor_sources(settings)
        for name in WORKTREE_ROLES:
            self.assertEqual(roles[name].executor, "antigravity", name)
            self.assertEqual(sources[name], "explicit", name)


if __name__ == "__main__":
    unittest.main()
