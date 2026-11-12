"""Server-managed empty projects: real git temp dirs, no caller-chosen paths."""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

from conveyor.agents.supervisor_service import (
    SupervisorServiceError,
    _project_git_env,
    create_managed_project,
    list_scoped_tasks,
)
from conveyor.web.web_supervisor_api import project_create_block_reason
from tests.test_supervisor_api import TOKEN, _HttpHarness, _make_settings


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "--no-pager", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _git_quiet(repo: Path, *args: str) -> None:
    """Git for a fixture repo. Ignore the hook traps installed in the test environment."""
    env = os.environ.copy()
    env.pop("GIT_CONFIG_GLOBAL", None)
    env.pop("GIT_TEMPLATE_DIR", None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    subprocess.run(
        ["git", "--no-pager", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )


class ManagedProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-sup-project-")
        self.root = Path(self._tmp.name)
        self.settings = _make_settings(self.root / "env")
        self.task_root = Path(self.settings.codex_task_root)
        self._env = os.environ.copy()
        self.home = self.root / "home"
        self.home.mkdir()
        self.gitconfig = self.home / ".gitconfig"
        self.gitconfig_bytes = b"# canary-global\n"
        self.gitconfig.write_bytes(self.gitconfig_bytes)
        self.marker = self.root / "hook-ran"
        template_hooks = self.root / "evil-template" / "hooks"
        template_hooks.mkdir(parents=True)
        template_hook = template_hooks / "post-commit"
        template_hook.write_text(f"#!/bin/sh\necho template >> {self.marker}\n", encoding="utf-8")
        template_hook.chmod(0o755)
        global_hooks = self.root / "evil-hooks"
        global_hooks.mkdir()
        global_hook = global_hooks / "post-commit"
        global_hook.write_text(f"#!/bin/sh\necho global >> {self.marker}\n", encoding="utf-8")
        global_hook.chmod(0o755)
        self.global_config = self.root / "gitconfig-global"
        self.global_config_bytes = f"[core]\n\thooksPath = {global_hooks}\n".encode()
        self.global_config.write_bytes(self.global_config_bytes)
        os.environ["HOME"] = str(self.home)
        os.environ["GIT_CONFIG_GLOBAL"] = str(self.global_config)
        os.environ["GIT_TEMPLATE_DIR"] = str(self.root / "evil-template")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def _assert_untouched_git_config(self) -> None:
        self.assertEqual(self.gitconfig.read_bytes(), self.gitconfig_bytes)
        self.assertEqual(self.global_config.read_bytes(), self.global_config_bytes)
        self.assertFalse(self.marker.exists())

    def test_creates_empty_repo_and_isolated_worktree(self) -> None:
        for name in ("门户修复", "运维 门户"):
            with self.subTest(name=name):
                created = create_managed_project(self.settings, name=name)
                dest = (self.task_root / "projects" / name).resolve()
                self.assertEqual(Path(created["repository"]["path"]).resolve(), dest)
                self.assertEqual(created["repository"]["name"], name)
                self.assertTrue(created["created"])
                self.assertEqual(_git(dest, "branch", "--show-current").stdout.strip(), "main")
                self.assertEqual(_git(dest, "log", "-1", "--format=%s").stdout.strip(), "Initial commit")
                self.assertEqual(_git(dest, "log", "-1", "--format=%an").stdout.strip(), "Conveyor")
                self.assertEqual(_git(dest, "rev-list", "--count", "HEAD").stdout.strip(), "1")
                self.assertNotEqual(_git(dest, "config", "--local", "--get", "user.name", check=False).returncode, 0)
                self.assertNotEqual(_git(dest, "config", "--local", "--get", "core.hooksPath", check=False).returncode, 0)
                self.assertFalse((dest / ".git" / "hooks" / "post-commit").exists())
                worktree = created["worktree"]
                wt_path = Path(worktree["path"]).resolve()
                self.assertTrue(worktree["managed"])
                self.assertFalse(worktree["is_main"])
                self.assertNotEqual(wt_path, dest)
                self.assertEqual(wt_path.parent, (self.task_root / "worktrees" / "supervisor").resolve())
                self.assertEqual(_git(wt_path, "status", "--porcelain").returncode, 0)
                self.assertEqual(_git(wt_path, "rev-parse", "--is-inside-work-tree").stdout.strip(), "true")
                self.assertEqual(list_scoped_tasks(self.settings, repo_id=created["repository"]["id"]), [])
        self._assert_untouched_git_config()

    def test_duplicate_and_concurrent_names_keep_the_first_repository(self) -> None:
        created = create_managed_project(self.settings, name="门户修复")
        dest = Path(created["repository"]["path"])
        with self.assertRaises(SupervisorServiceError) as dup:
            create_managed_project(self.settings, name="门户修复")
        self.assertEqual(dup.exception.status, 409)
        self.assertEqual(dup.exception.code, "project_exists")
        self.assertEqual(_git(dest, "rev-list", "--count", "HEAD").stdout.strip(), "1")

        barrier = threading.Barrier(2)
        outcomes: list[tuple] = []
        lock = threading.Lock()

        def _race() -> None:
            barrier.wait()
            try:
                result = create_managed_project(self.settings, name="并发项目")
            except SupervisorServiceError as exc:
                with lock:
                    outcomes.append(("err", exc.code, exc.status))
                return
            with lock:
                outcomes.append(("ok", result))

        threads = [threading.Thread(target=_race) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wins = [item for item in outcomes if item[0] == "ok"]
        losses = [item for item in outcomes if item[0] == "err"]
        self.assertEqual(len(wins), 1, outcomes)
        self.assertEqual(len(losses), 1, outcomes)
        self.assertEqual(losses[0][1], "project_exists")
        self.assertEqual(losses[0][2], 409)
        self.assertEqual(_git(Path(wins[0][1]["repository"]["path"]), "rev-list", "--count", "HEAD").stdout.strip(), "1")
        self._assert_untouched_git_config()

    def test_rejects_traversal_options_and_bad_types(self) -> None:
        outside = self.root / "outside"
        bad_names = [
            "../out",
            "..",
            ".",
            "a/../../out",
            "a/b",
            "a\\b",
            "-c",
            "--upload-pack=evil",
            "ok\nname",
            "x" * 65,
            "",
            "   ",
            None,
            12,
            ["运维"],
            ".hidden",
            "con..fig",
        ]
        for name in bad_names:
            with self.subTest(name=repr(name)):
                with self.assertRaises(SupervisorServiceError) as ctx:
                    create_managed_project(self.settings, name=name)
                self.assertEqual(ctx.exception.code, "invalid_name")
                self.assertEqual(ctx.exception.status, HTTPStatus.BAD_REQUEST)
        self.assertFalse((self.task_root / "projects").exists())
        self.assertFalse((self.task_root / "out").exists())
        self.assertFalse(outside.exists())
        self.assertFalse((self.root / "out").exists())
        self._assert_untouched_git_config()

    def test_symlink_name_is_not_followed(self) -> None:
        outside = self.root / "linked-target"
        outside.mkdir()
        (outside / "SECRET").write_text("keep", encoding="utf-8")
        projects = self.task_root / "projects"
        projects.mkdir()
        link = projects / "alias"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(SupervisorServiceError) as ctx:
            create_managed_project(self.settings, name="alias")
        self.assertEqual(ctx.exception.code, "project_exists")
        self.assertTrue(link.is_symlink())
        self.assertEqual((outside / "SECRET").read_text(encoding="utf-8"), "keep")
        self.assertFalse((outside / ".git").exists())
        self._assert_untouched_git_config()

    def test_projects_root_symlink_is_refused(self) -> None:
        outside = self.root / "elsewhere"
        outside.mkdir()
        (self.task_root / "projects").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(SupervisorServiceError) as ctx:
            create_managed_project(self.settings, name="门户")
        self.assertEqual(ctx.exception.code, "projects_root_unsafe")
        self.assertEqual(list(outside.iterdir()), [])
        self._assert_untouched_git_config()

    def test_existing_directory_and_repo_are_not_mutated(self) -> None:
        projects = self.task_root / "projects"
        plain = projects / "已有目录"
        plain.mkdir(parents=True)
        (plain / "KEEP").write_text("data", encoding="utf-8")
        with self.assertRaises(SupervisorServiceError) as plain_err:
            create_managed_project(self.settings, name="已有目录")
        self.assertEqual(plain_err.exception.code, "project_exists")
        self.assertEqual((plain / "KEEP").read_text(encoding="utf-8"), "data")
        self.assertFalse((plain / ".git").exists())

        repo = projects / "已有仓库"
        repo.mkdir()
        _git_quiet(repo, "init", "-b", "main")
        _git_quiet(repo, "config", "user.email", "pre@example.com")
        _git_quiet(repo, "config", "user.name", "Preexisting")
        (repo / "KEEP").write_text("original", encoding="utf-8")
        _git_quiet(repo, "add", "KEEP")
        _git_quiet(repo, "commit", "-m", "preexisting")
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        with self.assertRaises(SupervisorServiceError) as repo_err:
            create_managed_project(self.settings, name="已有仓库")
        self.assertEqual(repo_err.exception.code, "project_exists")
        self.assertEqual(_git(repo, "rev-parse", "HEAD").stdout.strip(), head)
        self.assertEqual(_git(repo, "log", "-1", "--format=%s").stdout.strip(), "preexisting")
        self.assertEqual((repo / "KEEP").read_text(encoding="utf-8"), "original")
        self._assert_untouched_git_config()

    def test_git_failure_removes_only_the_new_directory(self) -> None:
        projects = self.task_root / "projects"
        projects.mkdir()
        sibling = projects / "sibling"
        sibling.mkdir()
        (sibling / "KEEP").write_text("stay", encoding="utf-8")

        def _fail(settings, argv, timeout=20.0):
            return subprocess.CompletedProcess(argv, 1, "", "forced failure")

        with patch("conveyor.agents.supervisor_service._run_project_git", _fail):
            with self.assertRaises(SupervisorServiceError) as ctx:
                create_managed_project(self.settings, name="broken")
        self.assertEqual(ctx.exception.code, "project_create_failed")
        self.assertFalse((projects / "broken").exists())
        self.assertEqual((sibling / "KEEP").read_text(encoding="utf-8"), "stay")
        self._assert_untouched_git_config()

    def test_inode_swap_during_failure_is_not_deleted(self) -> None:
        def _swap(settings, dest: Path) -> None:
            os.rmdir(dest)
            dest.mkdir()
            (dest / "KEEP").write_text("survivor", encoding="utf-8")
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                "sabotaged",
                code="project_create_failed",
            )

        with patch("conveyor.agents.supervisor_service._init_empty_git_repository", _swap):
            with self.assertRaises(SupervisorServiceError):
                create_managed_project(self.settings, name="换成别的")
        kept = self.task_root / "projects" / "换成别的" / "KEEP"
        self.assertEqual(kept.read_text(encoding="utf-8"), "survivor")
        self._assert_untouched_git_config()

    def test_worktree_failure_keeps_the_registered_repository(self) -> None:
        def _boom(settings, repo_id, branch=None, base_ref=None):
            raise SupervisorServiceError(
                HTTPStatus.BAD_REQUEST,
                "worktree failed",
                code="worktree_create_failed",
            )

        with patch("conveyor.agents.supervisor_service.create_managed_worktree", _boom):
            with self.assertRaises(SupervisorServiceError) as ctx:
                create_managed_project(self.settings, name="保留仓库")
        self.assertEqual(ctx.exception.code, "worktree_create_failed")
        dest = self.task_root / "projects" / "保留仓库"
        self.assertTrue((dest / ".git").is_dir())
        self.assertEqual(_git(dest, "log", "-1", "--format=%s").stdout.strip(), "Initial commit")
        self.assertEqual(list_scoped_tasks(self.settings), [])
        self._assert_untouched_git_config()

    def test_project_git_env_omits_process_secrets_when_utility_env_is_empty(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-project-secret", "PATH": "/usr/bin:/bin"}, clear=False):
            with patch("conveyor.agents.supervisor_service.utility_env", return_value=None):
                env = _project_git_env(self.settings)
            with patch("conveyor.agents.supervisor_service.utility_env", return_value={}):
                empty = _project_git_env(self.settings)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("sk-project-secret", " ".join(env.values()))
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertNotIn("OPENAI_API_KEY", empty)
        self.assertEqual(empty["GIT_CONFIG_GLOBAL"], os.devnull)


class _HeaderMap:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = {key.lower(): value for key, value in values.items()}

    def get(self, name: str, default: str | None = None) -> str | None:
        return self._values.get(name.lower(), default)


class ManagedProjectHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="conveyor-sup-project-http-")
        self.root = Path(self._tmp.name)
        self._harnesses: list[_HttpHarness] = []

    def tearDown(self) -> None:
        for harness in reversed(self._harnesses):
            harness.close()
        self._tmp.cleanup()

    def _post_project(
        self,
        harness: _HttpHarness,
        body: dict[str, object],
        extra: dict[str, str] | None = None,
        *,
        authorized: bool = True,
        content_type: str | None = "application/json",
    ) -> tuple[int, dict[str, object]]:
        payload = json.dumps(body).encode("utf-8")
        headers: dict[str, str] = {"Content-Length": str(len(payload))}
        if content_type is not None:
            headers["Content-Type"] = content_type
        if authorized:
            headers["Authorization"] = f"Bearer {TOKEN}"
        if extra:
            headers.update(extra)
        conn = http.client.HTTPConnection("127.0.0.1", harness.port, timeout=10)
        try:
            conn.request("POST", "/api/supervisor/projects", body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
            return resp.status, data
        finally:
            conn.close()

    def test_authenticated_create_ignores_client_path_and_dispatches_nothing(self) -> None:
        settings = _make_settings(self.root / "http")
        harness = _HttpHarness(settings)
        self._harnesses.append(harness)
        status, body = self._post_project(
            harness,
            {
                "name": "运维门户",
                "path": "/tmp/not-allowed",
                "branch": "--upload-pack=evil",
            },
            {"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(status, 201, body)
        self.assertTrue(body["created"])
        self.assertTrue(str(body["repository"]["path"]).endswith("projects/运维门户"))
        self.assertNotIn("not-allowed", body["repository"]["path"])
        self.assertNotIn("upload-pack", body["repository"]["path"])
        self.assertTrue(body["worktree"]["managed"])
        self.assertFalse(body["worktree"]["is_main"])
        self.assertTrue(str(body["worktree"].get("chat_session_id") or "").startswith("web:web-console:supwt-"))
        task_status, tasks = harness.request_json("GET", "/api/supervisor/tasks")
        self.assertEqual(task_status, 200)
        self.assertEqual(tasks.get("tasks"), [])
        self.assertNotIn(TOKEN, str(body))

    def test_create_requires_auth(self) -> None:
        settings = _make_settings(self.root / "http-auth")
        harness = _HttpHarness(settings)
        self._harnesses.append(harness)
        status, body = harness.request_json(
            "POST",
            "/api/supervisor/projects",
            {"name": "另一个"},
            authorized=False,
        )
        self.assertEqual(status, 401)
        self.assertNotIn(TOKEN, str(body))
        self.assertFalse((Path(settings.codex_task_root) / "projects").exists())

    def test_create_csrf_rejects_before_any_directory_is_created(self) -> None:
        settings = _make_settings(self.root / "http-csrf")
        harness = _HttpHarness(settings)
        self._harnesses.append(harness)
        projects = Path(settings.codex_task_root) / "projects"
        host = f"127.0.0.1:{harness.port}"
        origin = f"http://{host}"
        denied = [
            {},
            {"X-Requested-With": "XMLHttpRequest", "Content-Type": "text/plain"},
            {"Origin": "http://[", "X-Requested-With": "XMLHttpRequest"},
            {"Origin": "http://evil.example", "X-Requested-With": "XMLHttpRequest", "Sec-Fetch-Site": "cross-site"},
            {"Origin": origin, "X-Requested-With": "XMLHttpRequest", "Sec-Fetch-Site": "same-site"},
            {
                "Origin": "http://evil.example",
                "X-Requested-With": "XMLHttpRequest",
                "X-Forwarded-Host": host,
            },
        ]
        for extra in denied:
            content_type: str | None = "application/json"
            headers = dict(extra)
            if "Content-Type" in headers:
                content_type = headers.pop("Content-Type")
            status, body = self._post_project(
                harness,
                {"name": "不该出现"},
                headers,
                content_type=content_type,
            )
            self.assertEqual(status, 403, body)
            self.assertNotEqual(status, 500)
            self.assertEqual(body.get("code"), "origin_or_request_denied")
            self.assertFalse(projects.exists())
        ok, created = self._post_project(
            harness,
            {"name": "站点校验通过"},
            {
                "Host": host,
                "Origin": origin,
                "X-Requested-With": "XMLHttpRequest",
                "Sec-Fetch-Site": "same-origin",
                "X-Forwarded-Host": "evil.example",
            },
        )
        self.assertEqual(ok, 201, created)
        self.assertTrue(projects.joinpath("站点校验通过").is_dir())

    def test_origin_match_includes_port_and_ipv6_and_ignores_forwarded_host(self) -> None:
        def reason(headers: dict[str, str]) -> str | None:
            base = {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            }
            base.update(headers)
            return project_create_block_reason(_HeaderMap(base))

        self.assertIsNone(reason({"Host": "[::1]:18787", "Origin": "http://[::1]:18787"}))
        self.assertEqual(reason({"Host": "[::1]:18787", "Origin": "http://[::1]:9"}), "origin")
        self.assertEqual(reason({"Host": "app.example:18787", "Origin": "http://app.example"}), "origin")
        self.assertIsNone(reason({
            "Host": "app.example:18787",
            "Origin": "http://app.example:18787",
            "X-Forwarded-Host": "evil.example",
        }))
        self.assertEqual(reason({
            "Host": "app.example:18787",
            "Origin": "http://evil.example",
            "X-Forwarded-Host": "app.example:18787",
        }), "origin")
        self.assertEqual(reason({"Host": "app.example", "Origin": "not a url"}), "origin")
        self.assertIsNone(reason({}))


if __name__ == "__main__":
    unittest.main()
