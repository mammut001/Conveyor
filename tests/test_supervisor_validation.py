"""tests/test_supervisor_validation.py — per-repository auto-detected validation.

Covers:
- ``detect_candidates`` for every detector (node default vs real test, pnpm/yarn
  lockfiles, pytest config variants, tests/ unittest fallback, Makefile test
  target, go.mod, Cargo.toml, garbage package.json).
- Storage: set with unknown id -> 400 ``unknown_check``; set/get round trip;
  empty list clears.
- Env precedence: env specs win over repo selection.
- ``agent_supervisor.load_validation_commands(settings, repo_path)`` returns the
  stored per-repo specs.
- ``run_supervisor_flow`` uses per-repo checks (completed) and, when none are
  selected, honestly reaches ``waiting_user`` / ``needs_validation``.
- HTTP GET/POST of the validation route.

No real model API or CLI is ever called; operator-validation commands are real
but trivial (``python -c pass``) subprocesses.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_supervisor as ags
import supervisor_service as svc
import supervisor_validation as val
from config import Settings

_ENV_KEYS_TO_CLEAR = (
    "CONVEYOR_AGENT_SUPERVISOR_ENABLED",
    "CONVEYOR_SUPERVISOR_ENABLED",
    "CONVEYOR_AGENT_SUPERVISOR_ANALYST_EXECUTOR",
    "CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTER_EXECUTOR",
    "CONVEYOR_AGENT_SUPERVISOR_REVIEWER_EXECUTOR",
    "CONVEYOR_AGENT_SUPERVISOR_COORDINATOR_EXECUTOR",
    "CONVEYOR_AGENT_SUPERVISOR_ANALYST_MODEL",
    "CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTER_MODEL",
    "CONVEYOR_AGENT_SUPERVISOR_REVIEWER_MODEL",
    "CONVEYOR_AGENT_SUPERVISOR_ANALYST_LOCATION",
    "CONVEYOR_AGENT_SUPERVISOR_IMPLEMENTER_LOCATION",
    "CONVEYOR_AGENT_SUPERVISOR_REVIEWER_LOCATION",
)


def _make_settings(
    root: Path,
    *,
    validation_commands: tuple[tuple[str, tuple[str, ...]], ...] = (),
    impl_executor: str = "fake",
    supervisor_enabled: bool = True,
) -> Settings:
    workspace = root / "workspace"
    task_root = root / "tasks"
    mem_root = root / "memory"
    for d in (workspace, task_root, mem_root):
        d.mkdir(parents=True, exist_ok=True)
    return Settings(
        telegram_bot_token="",
        telegram_allowed_user_id=0,
        codex_workspace_root=workspace,
        codex_bin="/bin/echo",
        codex_task_root=task_root,
        codex_model=None,
        codex_timeout_seconds=30,
        telegram_progress_seconds=3,
        codex_retry_429_delays_seconds=(),
        codex_memory_root=mem_root,
        user_timezone="UTC",
        chat_mode="off",
        chat_base_url=None,
        chat_api_key=None,
        chat_model=None,
        chat_tools_enabled=False,
        agent_supervisor_enabled=supervisor_enabled,
        agent_supervisor_implementation_executor=impl_executor,
        agent_supervisor_max_repair_rounds=1,
        agent_supervisor_task_timeout_seconds=25,
        agent_supervisor_validation_commands=validation_commands,
        agent_supervisor_validation_timeout_seconds=15,
    )


def _git(*argv: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *argv], cwd=str(cwd), capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {argv} failed: {result.stderr[:300]}")
    return (result.stdout or "").strip()


def _init_repo(repo_dir: Path) -> str:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=repo_dir)
    _git("config", "user.email", "val@test.local", cwd=repo_dir)
    _git("config", "user.name", "Val Test", cwd=repo_dir)
    (repo_dir / "README.md").write_text("# repo\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo_dir)
    _git("commit", "-qm", "initial", cwd=repo_dir)
    return _git("rev-parse", "HEAD", cwd=repo_dir)


def _ids(candidates: list[dict[str, Any]]) -> list[str]:
    return [c["id"] for c in candidates]


def _by_id(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in candidates}


class _FakeHandler:
    """Minimal WebConsoleHandler stand-in for direct route dispatch tests."""

    def __init__(self, settings: Any) -> None:
        self.server = types.SimpleNamespace(
            control=types.SimpleNamespace(settings=settings),
            loop=None,
        )
        self.response: tuple[int, dict[str, Any]] | None = None

    def _json(self, status: Any, payload: dict[str, Any]) -> None:
        self.response = (int(status), payload)


# ---------------------------------------------------------------------------
# detect_candidates
# ---------------------------------------------------------------------------

class DetectCandidatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sup-val-detect-")
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _repo(self, name: str) -> Path:
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_missing_path_returns_empty(self) -> None:
        self.assertEqual(val.detect_candidates(""), [])
        self.assertEqual(val.detect_candidates(str(self.root / "nope")), [])

    def test_node_default_test_is_skipped_but_lint_build_detected(self) -> None:
        d = self._repo("npm-default")
        (d / "package.json").write_text(
            json.dumps({
                "scripts": {
                    "test": 'echo "Error: no test specified" && exit 1',
                    "lint": "eslint .",
                    "build": "tsc",
                }
            }),
            encoding="utf-8",
        )
        cands = _by_id(val.detect_candidates(str(d)))
        self.assertNotIn("npm-test", cands)
        self.assertIn("npm-lint", cands)
        self.assertIn("npm-build", cands)
        self.assertEqual(cands["npm-lint"]["argv"], ["npm", "run", "lint"])
        self.assertEqual(cands["npm-build"]["argv"], ["npm", "run", "build"])
        # Every candidate carries the full schema.
        for cand in cands.values():
            self.assertIn("label", cand)
            self.assertIn("reason", cand)
            self.assertIsInstance(cand["available"], bool)
            self.assertIsInstance(cand["unavailable_reason"], str)

    def test_node_real_test_detected(self) -> None:
        d = self._repo("npm-real")
        (d / "package.json").write_text(
            json.dumps({"scripts": {"test": "jest --ci"}}), encoding="utf-8"
        )
        cands = _by_id(val.detect_candidates(str(d)))
        self.assertIn("npm-test", cands)
        self.assertEqual(cands["npm-test"]["argv"], ["npm", "test", "--silent"])
        self.assertEqual(cands["npm-test"]["reason"], "项目里配好了测试命令")

    def test_pnpm_lockfile_uses_pnpm_ids(self) -> None:
        d = self._repo("pnpm")
        (d / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest", "lint": "eslint ."}}),
            encoding="utf-8",
        )
        (d / "pnpm-lock.yaml").write_text("lockfileVersion: 6\n", encoding="utf-8")
        cands = _by_id(val.detect_candidates(str(d)))
        self.assertIn("pnpm-test", cands)
        self.assertEqual(cands["pnpm-test"]["argv"], ["pnpm", "test", "--silent"])
        self.assertIn("pnpm-lint", cands)
        self.assertNotIn("npm-test", cands)

    def test_yarn_lockfile_uses_yarn_ids(self) -> None:
        d = self._repo("yarn")
        (d / "package.json").write_text(
            json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8"
        )
        (d / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
        cands = _by_id(val.detect_candidates(str(d)))
        self.assertIn("yarn-test", cands)
        self.assertEqual(cands["yarn-test"]["argv"], ["yarn", "test", "--silent"])

    def test_garbage_package_json_is_skipped(self) -> None:
        d = self._repo("garbage")
        (d / "package.json").write_text("{ this is : not json ]", encoding="utf-8")
        self.assertEqual(val.detect_candidates(str(d)), [])
        # package.json that is valid JSON but not an object is also skipped.
        (d / "package.json").write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(val.detect_candidates(str(d)), [])

    def test_pytest_ini(self) -> None:
        d = self._repo("pytest-ini")
        (d / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        cands = _by_id(val.detect_candidates(str(d)))
        self.assertEqual(_ids(list(cands.values())), ["pytest"])
        self.assertEqual(cands["pytest"]["argv"], ["python3", "-m", "pytest", "-q"])
        self.assertEqual(cands["pytest"]["reason"], "pytest.ini")

    def test_pyproject_pytest_section(self) -> None:
        d = self._repo("pyproject")
        (d / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\nminversion = \"6.0\"\n", encoding="utf-8"
        )
        cands = _by_id(val.detect_candidates(str(d)))
        self.assertIn("pytest", cands)
        self.assertIn("pyproject.toml", cands["pytest"]["reason"])

    def test_setup_cfg_pytest_section(self) -> None:
        d = self._repo("setupcfg")
        (d / "setup.cfg").write_text(
            "[tool:pytest]\ntestpaths = tests\n", encoding="utf-8"
        )
        cands = _by_id(val.detect_candidates(str(d)))
        self.assertIn("pytest", cands)
        self.assertIn("setup.cfg", cands["pytest"]["reason"])

    def test_conftest_at_root(self) -> None:
        d = self._repo("conftest")
        (d / "conftest.py").write_text("# pytest conftest\n", encoding="utf-8")
        cands = _by_id(val.detect_candidates(str(d)))
        self.assertEqual(cands["pytest"]["reason"], "conftest.py")

    def test_tests_dir_unittest_fallback(self) -> None:
        d = self._repo("unittest")
        (d / "tests").mkdir()
        (d / "tests" / "test_thing.py").write_text(
            "import unittest\n", encoding="utf-8"
        )
        cands = _by_id(val.detect_candidates(str(d)))
        self.assertEqual(_ids(list(cands.values())), ["unittest"])
        self.assertEqual(cands["unittest"]["label"], "运行测试")
        self.assertEqual(
            cands["unittest"]["argv"],
            ["python3", "-m", "unittest", "discover", "-s", "tests"],
        )
        self.assertNotIn("test_*.py", cands["unittest"]["reason"])

    def test_pytest_config_wins_over_unittest_fallback(self) -> None:
        d = self._repo("pytest-and-tests")
        (d / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (d / "tests").mkdir()
        (d / "tests" / "test_thing.py").write_text("import unittest\n", encoding="utf-8")
        cands = _by_id(val.detect_candidates(str(d)))
        self.assertIn("pytest", cands)
        self.assertNotIn("unittest", cands)

    def test_makefile_test_target(self) -> None:
        d = self._repo("make")
        (d / "Makefile").write_text("test:\n\t@true\n", encoding="utf-8")
        cands = _by_id(val.detect_candidates(str(d)))
        self.assertEqual(cands["make-test"]["argv"], ["make", "test"])
        # A target like `mytest:` must NOT match.
        d2 = self._repo("make-no")
        (d2 / "Makefile").write_text("build:\n\t@true\nmytest:\n\t@true\n", encoding="utf-8")
        self.assertNotIn("make-test", _by_id(val.detect_candidates(str(d2))))

    def test_go_mod(self) -> None:
        d = self._repo("go")
        (d / "go.mod").write_text("module example.com/m\n\ngo 1.21\n", encoding="utf-8")
        cands = _by_id(val.detect_candidates(str(d)))
        self.assertEqual(cands["go-test"]["argv"], ["go", "test", "./..."])

    def test_cargo_toml(self) -> None:
        d = self._repo("rust")
        (d / "Cargo.toml").write_text("[package]\nname = \"m\"\n", encoding="utf-8")
        cands = _by_id(val.detect_candidates(str(d)))
        self.assertEqual(cands["cargo-test"]["argv"], ["cargo", "test", "--quiet"])

    def test_multiple_ecosystems_coexist(self) -> None:
        d = self._repo("polyglot")
        (d / "package.json").write_text(
            json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8"
        )
        (d / "go.mod").write_text("module m\n", encoding="utf-8")
        ids = set(_ids(val.detect_candidates(str(d))))
        self.assertEqual(ids, {"npm-test", "go-test"})


# ---------------------------------------------------------------------------
# Storage: set/get round trip, unknown id, clear, env precedence
# ---------------------------------------------------------------------------

class RepoValidationStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        for key in _ENV_KEYS_TO_CLEAR:
            os.environ.pop(key, None)
        self._tmp = tempfile.TemporaryDirectory(prefix="sup-val-store-")
        self.root = Path(self._tmp.name).resolve()
        self.settings = _make_settings(self.root)
        self.repo_dir = self.root / "repo"
        _init_repo(self.repo_dir)
        (self.repo_dir / "Makefile").write_text("test:\n\t@true\n", encoding="utf-8")
        _git("add", "Makefile", cwd=self.repo_dir)
        _git("commit", "-qm", "add makefile", cwd=self.repo_dir)
        repo, _created = svc.register_repository(self.settings, path=str(self.repo_dir))
        self.repo_id = str(repo["id"])
        self.canon_repo = str(repo["path"])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_set_get_round_trip(self) -> None:
        payload = val.set_repo_validation(self.settings, self.repo_id, ["make-test"])
        self.assertEqual(payload["selected"], ["make-test"])
        self.assertEqual(payload["source"], "repo")
        self.assertEqual(payload["env_commands"], [])
        self.assertEqual(payload["effective"], [{"name": "make-test", "argv": ["make", "test"]}])
        self.assertIn("make-test", _by_id(payload["candidates"]))

        fetched = val.get_repo_validation(self.settings, self.repo_id)
        self.assertEqual(fetched["selected"], ["make-test"])
        self.assertEqual(fetched["source"], "repo")

    def test_set_unknown_id_is_400_unknown_check(self) -> None:
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            val.set_repo_validation(self.settings, self.repo_id, ["make-test", "bogus-check"])
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "unknown_check")
        # Nothing was persisted by the failed call.
        fetched = val.get_repo_validation(self.settings, self.repo_id)
        self.assertEqual(fetched["selected"], [])

    def test_empty_list_clears(self) -> None:
        val.set_repo_validation(self.settings, self.repo_id, ["make-test"])
        payload = val.set_repo_validation(self.settings, self.repo_id, [])
        self.assertEqual(payload["selected"], [])
        self.assertEqual(payload["source"], "none")
        self.assertEqual(payload["effective"], [])

    def test_duplicate_ids_are_deduped(self) -> None:
        payload = val.set_repo_validation(self.settings, self.repo_id, ["make-test", "make-test"])
        self.assertEqual(payload["selected"], ["make-test"])

    def test_non_string_id_is_rejected(self) -> None:
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            val.set_repo_validation(self.settings, self.repo_id, [123])
        self.assertEqual(ctx.exception.status, 400)

    def test_non_list_selected_is_rejected(self) -> None:
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            val.set_repo_validation(self.settings, self.repo_id, "make-test")
        self.assertEqual(ctx.exception.status, 400)

    def test_get_unknown_repo_is_404(self) -> None:
        with self.assertRaises(svc.SupervisorServiceError) as ctx:
            val.get_repo_validation(self.settings, "repo-doesnotexist")
        self.assertEqual(ctx.exception.status, 404)


# ---------------------------------------------------------------------------
# load_validation_commands integration + env precedence
# ---------------------------------------------------------------------------

class LoadValidationCommandsTests(unittest.TestCase):
    def setUp(self) -> None:
        for key in _ENV_KEYS_TO_CLEAR:
            os.environ.pop(key, None)
        self._tmp = tempfile.TemporaryDirectory(prefix="sup-val-load-")
        self.root = Path(self._tmp.name).resolve()
        self.repo_dir = self.root / "repo"
        _init_repo(self.repo_dir)
        (self.repo_dir / "Makefile").write_text("test:\n\t@true\n", encoding="utf-8")
        _git("add", "Makefile", cwd=self.repo_dir)
        _git("commit", "-qm", "add makefile", cwd=self.repo_dir)
        # Settings WITHOUT env validation commands.
        self.settings = _make_settings(self.root)
        repo, _ = svc.register_repository(self.settings, path=str(self.repo_dir))
        self.repo_id = str(repo["id"])
        self.canon_repo = str(repo["path"])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_returns_stored_specs_for_repo_path(self) -> None:
        val.set_repo_validation(self.settings, self.repo_id, ["make-test"])
        specs = ags.load_validation_commands(self.settings, repo_path=self.canon_repo)
        self.assertEqual(specs, [("make-test", ("make", "test"))])

    def test_empty_when_no_selection(self) -> None:
        self.assertEqual(
            ags.load_validation_commands(self.settings, repo_path=self.canon_repo), []
        )

    def test_empty_when_repo_not_registered(self) -> None:
        val.set_repo_validation(self.settings, self.repo_id, ["make-test"])
        # A path that is not a registered repository yields no specs.
        self.assertEqual(
            ags.load_validation_commands(self.settings, repo_path=str(self.root / "other")),
            [],
        )

    def test_env_specs_win_over_repo_selection(self) -> None:
        val.set_repo_validation(self.settings, self.repo_id, ["make-test"])
        env_settings = dataclasses.replace(
            self.settings,
            agent_supervisor_validation_commands=(("envcheck", ("echo", "hi")),),
        )
        # load_validation_commands returns the env specs unchanged.
        specs = ags.load_validation_commands(env_settings, repo_path=self.canon_repo)
        self.assertEqual(specs, [("envcheck", ("echo", "hi"))])
        # And the reported source/effective reflect env precedence.
        payload = val.get_repo_validation(env_settings, self.repo_id)
        self.assertEqual(payload["source"], "env")
        self.assertEqual(payload["env_commands"], ["envcheck"])
        self.assertEqual(payload["effective"], [{"name": "envcheck", "argv": ["echo", "hi"]}])
        # The repo selection is still stored and visible to the UI.
        self.assertEqual(payload["selected"], ["make-test"])

    def test_no_repo_path_returns_env_only(self) -> None:
        # Without repo_path and without env specs -> empty.
        self.assertEqual(ags.load_validation_commands(self.settings), [])
        env_settings = dataclasses.replace(
            self.settings,
            agent_supervisor_validation_commands=(("envcheck", ("echo", "hi")),),
        )
        self.assertEqual(
            ags.load_validation_commands(env_settings), [("envcheck", ("echo", "hi"))]
        )

    def test_metadata_cannot_supply_argv(self) -> None:
        # Defence in depth: task metadata still refuses operator-only keys.
        with self.assertRaises(ags.SupervisorStateError):
            ags.create_task(
                self.settings,
                goal="g",
                repo_path=self.canon_repo,
                metadata={"validation_commands": [["rm", ["-rf", "/"]]]},
            )


# ---------------------------------------------------------------------------
# run_supervisor_flow uses per-repo validation
# ---------------------------------------------------------------------------

class _FakeExecutor:
    name = "fake"

    def __init__(self, responder: Any) -> None:
        self._responder = responder

    def available(self) -> tuple[bool, str]:
        return True, "ok"

    async def run(self, *, role: Any, prompt: str, worktree_path: str,
                  timeout_seconds: int, task_id: str = "", context: Any = None) -> ags.ExecutionResult:
        return ags.ExecutionResult(
            success=True,
            output=self._responder(prompt),
            started_at=ags._utc_now(),
            finished_at=ags._utc_now(),
        )

    async def cancel(self, execution_id: str) -> bool:
        return False


class _UnavailableExecutor:
    name = "chat"

    def available(self) -> tuple[bool, str]:
        return False, "chat disabled in test"

    async def run(self, **kwargs: Any) -> ags.ExecutionResult:  # pragma: no cover
        raise AssertionError("chat executor must not run in this test")

    async def cancel(self, execution_id: str) -> bool:  # pragma: no cover
        return False


def _reviewer_responder(prompt: str) -> str:
    if "INDEPENDENT code reviewer" in prompt:
        sha = fp = ""
        for line in prompt.splitlines():
            if line.startswith("Current SHA: "):
                sha = line[len("Current SHA: "):].strip()
            elif line.startswith("Target fingerprint: "):
                fp = line[len("Target fingerprint: "):].strip()
        return json.dumps({
            "passed": True,
            "target_sha": sha,
            "target_fingerprint": fp,
            "issues": [],
            "checks": [{"name": "inspect", "command": "read", "passed": True, "output": "ok"}],
            "summary": "verified read-only",
        })
    if "You are the implementer." in prompt:
        return "implemented (no file changes in fake)"
    if "You are the analyst." in prompt:
        return "1. inspect and verify."
    return "ok"


class RunSupervisorFlowValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        for key in _ENV_KEYS_TO_CLEAR:
            os.environ.pop(key, None)
        os.environ.pop("CONVEYOR_MAX_WORKTREES_BYTES", None)
        self._tmp = tempfile.TemporaryDirectory(prefix="sup-val-flow-")
        self.root = Path(self._tmp.name).resolve()
        self.settings = _make_settings(self.root)
        self.repo_dir = self.root / "repo"
        _init_repo(self.repo_dir)
        self.fake = _FakeExecutor(_reviewer_responder)
        self.unavailable = _UnavailableExecutor()

        def _get_executor(_settings: Any, name: str) -> Any:
            if name == "fake":
                return self.fake
            return self.unavailable

        self._exec_patch = patch.object(ags, "get_executor", _get_executor)
        self._exec_patch.start()
        self.addCleanup(self._exec_patch.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_flow(self, *, repo_specs: list[tuple[str, tuple[str, ...]]], max_repairs: int | None = None) -> dict[str, Any]:
        # No explicit worktree_path: queue_task creates a managed worktree under
        # the task root (canonicalized), which validate_worktree accepts.
        task = ags.create_task(
            self.settings,
            goal="do the thing",
            repo_path=str(self.repo_dir),
        )
        ags.queue_task(self.settings, task["id"])
        ags.claim_task(self.settings)
        with patch.object(
            val, "load_repo_validation_specs", return_value=repo_specs
        ):
            return asyncio.run(
                ags.run_supervisor_flow(self.settings, task["id"], max_repairs=max_repairs)
            )

    def test_per_repo_specs_complete_the_task(self) -> None:
        result = self._run_flow(repo_specs=[("repo-check", (sys.executable, "-c", "pass"))])
        self.assertEqual(result["state"], "completed", result)
        acceptance = ags.latest_acceptance(self.settings, result["id"])
        self.assertIsNotNone(acceptance)
        self.assertTrue(acceptance["passed"])
        self.assertTrue(acceptance["provenance_verified"])
        self.assertTrue(any(c.get("name") == "repo-check" for c in acceptance["executed_checks"]))

    def test_no_specs_reaches_needs_validation(self) -> None:
        result = self._run_flow(repo_specs=[])
        self.assertEqual(result["state"], "waiting_user", result)
        self.assertEqual(result["stage"], "needs_validation")
        self.assertEqual(result["metadata"].get("validation_status"), "needs_validation")

    def test_failing_per_repo_spec_fails_the_task(self) -> None:
        result = self._run_flow(
            repo_specs=[("boom", (sys.executable, "-c", "import sys; sys.exit(1)"))],
            max_repairs=0,
        )
        self.assertEqual(result["state"], "failed", result)
        acceptance = ags.latest_acceptance(self.settings, result["id"])
        self.assertIsNotNone(acceptance)
        self.assertFalse(acceptance["passed"])
        self.assertTrue(any("boom" in issue for issue in acceptance["issues"]))


# ---------------------------------------------------------------------------
# HTTP GET/POST
# ---------------------------------------------------------------------------

class ValidationHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        for key in _ENV_KEYS_TO_CLEAR:
            os.environ.pop(key, None)
        self._tmp = tempfile.TemporaryDirectory(prefix="sup-val-http-")
        self.root = Path(self._tmp.name).resolve()
        self.settings = _make_settings(self.root)
        self.repo_dir = self.root / "repo"
        _init_repo(self.repo_dir)
        (self.repo_dir / "Makefile").write_text("test:\n\t@true\n", encoding="utf-8")
        _git("add", "Makefile", cwd=self.repo_dir)
        _git("commit", "-qm", "add makefile", cwd=self.repo_dir)
        repo, _ = svc.register_repository(self.settings, path=str(self.repo_dir))
        self.repo_id = str(repo["id"])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _get(self, path: str, settings: Any = None) -> tuple[int, dict[str, Any]]:
        from web_supervisor_api import handle_get

        handler = _FakeHandler(settings or self.settings)
        handle_get(handler, path, path.strip("/").split("/"), {})
        assert handler.response is not None
        return handler.response

    def _post(self, path: str, body: dict[str, Any], settings: Any = None) -> tuple[int, dict[str, Any]]:
        from web_supervisor_api import handle_post

        handler = _FakeHandler(settings or self.settings)
        handle_post(handler, path, path.strip("/").split("/"), body)
        assert handler.response is not None
        return handler.response

    def test_get_validation_payload(self) -> None:
        code, body = self._get(f"/api/supervisor/repositories/{self.repo_id}/validation")
        self.assertEqual(code, 200, body)
        for key in ("candidates", "selected", "source", "env_commands", "effective"):
            self.assertIn(key, body)
        self.assertEqual(body["selected"], [])
        self.assertEqual(body["source"], "none")
        self.assertIn("make-test", _by_id(body["candidates"]))

    def test_post_set_and_get_round_trip(self) -> None:
        code, body = self._post(
            f"/api/supervisor/repositories/{self.repo_id}/validation",
            {"selected": ["make-test"]},
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(body["selected"], ["make-test"])
        self.assertEqual(body["source"], "repo")
        self.assertEqual(body["effective"], [{"name": "make-test", "argv": ["make", "test"]}])

        code_get, got = self._get(f"/api/supervisor/repositories/{self.repo_id}/validation")
        self.assertEqual(code_get, 200)
        self.assertEqual(got["selected"], ["make-test"])

    def test_post_unknown_check_is_400(self) -> None:
        code, body = self._post(
            f"/api/supervisor/repositories/{self.repo_id}/validation",
            {"selected": ["not-a-real-check"]},
        )
        self.assertEqual(code, 400, body)
        self.assertEqual(body.get("code"), "unknown_check")

    def test_post_empty_selection_clears(self) -> None:
        self._post(
            f"/api/supervisor/repositories/{self.repo_id}/validation",
            {"selected": ["make-test"]},
        )
        code, body = self._post(
            f"/api/supervisor/repositories/{self.repo_id}/validation",
            {"selected": []},
        )
        self.assertEqual(code, 200, body)
        self.assertEqual(body["selected"], [])
        self.assertEqual(body["source"], "none")

    def test_get_unknown_repo_is_404(self) -> None:
        code, body = self._get("/api/supervisor/repositories/repo-nope/validation")
        self.assertEqual(code, 404, body)

    def test_repository_payload_reports_validation_source(self) -> None:
        code, body = self._get("/api/supervisor/repositories")
        self.assertEqual(code, 200, body)
        repo = next(r for r in body["repositories"] if r["id"] == self.repo_id)
        self.assertEqual(repo["validation_source"], "none")
        self._post(
            f"/api/supervisor/repositories/{self.repo_id}/validation",
            {"selected": ["make-test"]},
        )
        code, body = self._get("/api/supervisor/repositories")
        repo = next(r for r in body["repositories"] if r["id"] == self.repo_id)
        self.assertEqual(repo["validation_source"], "repo")

    def test_feature_disabled_is_409(self) -> None:
        off = dataclasses.replace(self.settings, agent_supervisor_enabled=False)
        code, body = self._get(
            f"/api/supervisor/repositories/{self.repo_id}/validation", settings=off
        )
        self.assertEqual(code, 409, body)
        self.assertEqual(body.get("code"), "agent_supervisor_disabled")


if __name__ == "__main__":
    unittest.main()
