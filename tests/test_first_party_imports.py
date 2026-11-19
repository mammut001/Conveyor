"""tests/test_first_party_imports.py — Verify every first-party `from X import Y` resolves."""
from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
_EXCLUDED_PREFIXES = ("tests/", "web/", ".venv/", "node_modules/")
_EXCLUDED_DIRS = frozenset({
    "tests",
    "web",
    ".venv",
    "venv",
    "node_modules",
    ".git",
    "__pycache__",
    "vendor",
})
_SKIPPED = object()


def _iter_python_files(repo_root: Path) -> list[str]:
    """Return repo-relative POSIX paths of Python files outside test/web trees."""
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "*.py"],
            cwd=repo_root,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        files = [
            line.strip()
            for line in out.splitlines()
            if line.strip()
            and not line.strip().startswith(_EXCLUDED_PREFIXES)
            and (repo_root / line.strip()).is_file()
        ]
        if files:
            return sorted(files)
    except (subprocess.SubprocessError, OSError):
        pass

    discovered: list[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_DIRS)
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            rel = (Path(dirpath) / name).relative_to(repo_root).as_posix()
            if not rel.startswith(_EXCLUDED_PREFIXES):
                discovered.append(rel)
    return discovered


def _is_first_party(mod: str, repo_root: Path) -> bool:
    top = mod.split(".", 1)[0]
    return (repo_root / top).exists() or (repo_root / f"{top}.py").exists()


def _is_missing_env_error(exc: BaseException) -> bool:
    # A module that refuses to load under this configuration (e.g. desktop_agent_server
    # calls sys.exit() when its token is empty with the desktop node on) is a config
    # gate, not a broken import.
    if isinstance(exc, SystemExit):
        return True
    if not isinstance(exc, RuntimeError):
        return False
    msg = str(exc).lower()
    return "missing required environment variable" in msg or "environment variable" in msg


def find_broken_first_party_imports(
    repo_root: Path,
    *,
    files: list[str] | None = None,
) -> tuple[list[str], list[str], int]:
    """Walk Python files and check every level-0 first-party `from X import Y`.

    Returns ``(problems, skipped, checked_count)``.
    """
    target_files = files if files is not None else _iter_python_files(repo_root)
    seen: dict[tuple[str, str], object] = {}
    problems: list[str] = []
    skipped: list[str] = []

    orig_sys_path = list(sys.path)
    root_str = str(repo_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    try:
        for rel in target_files:
            path = repo_root / rel
            try:
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=rel)
            except Exception as exc:
                problems.append(f"{rel}:0: parse error: {type(exc).__name__}: {exc}")
                continue

            for node in ast.walk(tree):
                if not (isinstance(node, ast.ImportFrom) and node.module and node.level == 0):
                    continue
                mod = node.module
                if not _is_first_party(mod, repo_root):
                    continue

                for alias in node.names:
                    if alias.name == "*":
                        continue
                    key = (mod, alias.name)
                    if key not in seen:
                        try:
                            imported = importlib.import_module(mod)
                        except (Exception, SystemExit) as exc:
                            if _is_missing_env_error(exc):
                                seen[key] = _SKIPPED
                            else:
                                seen[key] = (
                                    f"module import failed: {type(exc).__name__}: {str(exc)[:120]}"
                                )
                        else:
                            if hasattr(imported, alias.name):
                                seen[key] = True
                            else:
                                submod = f"{mod}.{alias.name}"
                                try:
                                    importlib.import_module(submod)
                                except (Exception, SystemExit) as exc:
                                    if _is_missing_env_error(exc):
                                        seen[key] = _SKIPPED
                                    elif isinstance(exc, ModuleNotFoundError) and exc.name == submod:
                                        seen[key] = "missing"
                                    else:
                                        seen[key] = (
                                            f"submodule import failed: {type(exc).__name__}: "
                                            f"{str(exc)[:120]}"
                                        )
                                else:
                                    seen[key] = True

                    outcome = seen[key]
                    if outcome is _SKIPPED:
                        skipped.append(f"{rel}:{node.lineno}: from {mod} import {alias.name}")
                    elif outcome is not True:
                        problems.append(
                            f"{rel}:{node.lineno}: from {mod} import {alias.name} -> {outcome}"
                        )
    finally:
        sys.path[:] = orig_sys_path

    return problems, skipped, len(seen)


class TestFirstPartyImports(unittest.TestCase):
    def test_all_first_party_from_imports_resolve(self) -> None:
        tmp_root = Path(tempfile.gettempdir()) / "conveyor-test-first-party-imports"
        for sub in ("workspace", "tasks", "memory"):
            (tmp_root / sub).mkdir(parents=True, exist_ok=True)

        env_defaults = {
            "CONVEYOR_ENV_FILE": os.devnull,
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_ALLOWED_USER_ID": "12345",
            "CODEX_WORKSPACE_ROOT": str(tmp_root / "workspace"),
            "CODEX_TASK_ROOT": str(tmp_root / "tasks"),
            "CODEX_MEMORY_ROOT": str(tmp_root / "memory"),
        }
        env_patch = {k: (os.environ.get(k) or v) for k, v in env_defaults.items()}

        jq_mod = sys.modules.get("handlers.job_queue")
        saved_jq_state = None
        if jq_mod is not None and hasattr(jq_mod, "get_job_queue"):
            jq = jq_mod.get_job_queue()
            saved_jq_state = (jq._settings, jq._runner, jq._max_length)

        try:
            with patch.dict(os.environ, env_patch):
                problems, _skipped, checked = find_broken_first_party_imports(REPO_ROOT)
        finally:
            if saved_jq_state is not None and jq_mod is not None:
                jq = jq_mod.get_job_queue()
                jq._settings, jq._runner, jq._max_length = saved_jq_state

        self.assertGreater(checked, 0)
        self.assertEqual(
            problems,
            [],
            "Broken first-party imports found:\n" + "\n".join(problems),
        )

    def test_checker_reports_missing_attr_and_skips_missing_env(self) -> None:
        with tempfile.TemporaryDirectory(prefix="conveyor-import-check-") as td:
            root = Path(td)
            pkg = root / "_sample_pkg_r50"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "good.py").write_text("VALUE = 1\n", encoding="utf-8")
            (pkg / "needs_env.py").write_text(
                'raise RuntimeError("Missing required environment variable: FAKE_SECRET")\n',
                encoding="utf-8",
            )
            (pkg / "caller.py").write_text(
                "def fn():\n"
                "    from _sample_pkg_r50.good import nonexistent_name\n"
                "    from _sample_pkg_r50.needs_env import something\n",
                encoding="utf-8",
            )
            try:
                problems, skipped, _checked = find_broken_first_party_imports(
                    root,
                    files=["_sample_pkg_r50/caller.py"],
                )
            finally:
                for name in list(sys.modules):
                    if name == "_sample_pkg_r50" or name.startswith("_sample_pkg_r50."):
                        sys.modules.pop(name, None)

        self.assertEqual(len(problems), 1)
        self.assertIn("_sample_pkg_r50/caller.py:2:", problems[0])
        self.assertIn("nonexistent_name -> missing", problems[0])
        self.assertEqual(len(skipped), 1)
        self.assertIn("_sample_pkg_r50.needs_env", skipped[0])


if __name__ == "__main__":
    unittest.main()
