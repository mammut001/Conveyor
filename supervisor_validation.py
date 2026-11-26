"""Find a conventional project test command when the operator did not supply one."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def infer_validation(worktree: str) -> tuple[list[list[str]], str] | None:
    """Return a conservative baseline, or None when the project is ambiguous.

    The source is shown in task handoffs: automatic discovery is evidence of a
    command choice, not proof that the command covers the requested behavior.
    """
    root = Path(worktree)
    package = root / "package.json"
    if package.is_file():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        scripts = data.get("scripts") if isinstance(data, dict) else None
        script = scripts.get("test") if isinstance(scripts, dict) else None
        if isinstance(script, str) and script.strip():
            # npm's generated placeholder explicitly reports that no tests exist.
            if re.search(r"no test specified|no tests? found", script, re.IGNORECASE):
                return None
            package_manager = str(data.get("packageManager") or "").split("@", 1)[0]
            if package_manager not in ("npm", "pnpm", "yarn", "bun"):
                if (root / "pnpm-lock.yaml").is_file():
                    package_manager = "pnpm"
                elif (root / "yarn.lock").is_file():
                    package_manager = "yarn"
                elif (root / "bun.lock").is_file() or (root / "bun.lockb").is_file():
                    package_manager = "bun"
                else:
                    package_manager = "npm"
            return [[package_manager, "run", "test"]], "package.json scripts.test"

    pytest_config = (root / "pytest.ini").is_file() or (root / ".pytest.ini").is_file()
    for filename, marker in (("pyproject.toml", "[tool.pytest"), ("setup.cfg", "[tool:pytest]"),
                             ("tox.ini", "[pytest]")):
        try:
            if marker in (root / filename).read_text(encoding="utf-8"):
                pytest_config = True
        except (OSError, UnicodeError):
            pass
    if pytest_config:
        return [[sys.executable, "-m", "pytest"]], "pytest project configuration"

    tests = root / "tests"
    if tests.is_dir():
        # Only choose unittest when at least one test file explicitly imports it;
        # plain pytest functions are not reliably discoverable by unittest.
        try:
            for path in tests.rglob("test*.py"):
                if re.search(r"(?m)^\s*(?:import unittest\b|from unittest\b)",
                             path.read_text(encoding="utf-8", errors="replace")):
                    return [[sys.executable, "-m", "unittest", "discover", "-s", "tests"]], "unittest tests directory"
        except OSError:
            return None
    return None
