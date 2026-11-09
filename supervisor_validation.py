"""supervisor_validation.py — per-repository auto-detected validation checks.

The Agent Supervisor only accepts a task as completed after host-executed
validation commands pass (see ``agent_supervisor.load_validation_commands`` /
``run_operator_validations``). Operators can configure those checks globally via
``CONVEYOR_AGENT_SUPERVISOR_VALIDATION_COMMANDS_JSON`` (which always wins). For a
non-technical owner this module lets them pick, per repository, from checks the
SERVER detects in that repository.

Security rule (kept intact): the browser never supplies argv. It only selects
IDs of server-detected candidates; the argv is always the server-derived value
captured from the candidate at selection time.

This module never imports ``supervisor_service`` at module import time (that
module imports ``agent_supervisor``, which lazily imports this module). All
``supervisor_service`` access is done with a lazy import inside the functions.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

# Cap every repo file read at 512 KB; tolerate unreadable/garbage files silently.
_MAX_READ_BYTES = 512 * 1024

# npm's default `test` script echoes this string; such a repo has no real test.
_NPM_DEFAULT_TEST_MARKER = "no test specified"

# A Makefile rule line for the `test` target (target names are not tab-indented).
_MAKE_TEST_RE = re.compile(r"(?m)^test[ \t]*:")

# Idempotent migration for the per-repository validation table. Kept in sync with
# supervisor_service._WEB_SCHEMA_SQL; re-asserted here so an already-initialised
# database (created before this table existed) gains the table without a restart.
_VALIDATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS supervisor_repo_validation (
    repo_id TEXT PRIMARY KEY,
    selected_json TEXT NOT NULL,
    specs_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(repo_id) REFERENCES supervisor_repositories(id) ON DELETE CASCADE
);
"""


# ---------------------------------------------------------------------------
# Candidate detection (pure filesystem scan; never executes anything)
# ---------------------------------------------------------------------------

def _read_text_capped(path: Path) -> str | None:
    """Read at most ``_MAX_READ_BYTES`` bytes and decode leniently.

    Returns ``None`` for missing/unreadable files so callers can skip silently.
    """
    try:
        if not path.is_file():
            return None
        with path.open("rb") as handle:
            data = handle.read(_MAX_READ_BYTES)
    except OSError:
        return None
    return data.decode("utf-8", errors="replace")


def _load_json(path: Path) -> Any:
    text = _read_text_capped(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _candidate(*, cid: str, label: str, argv: list[str], reason: str) -> dict[str, Any]:
    available = shutil.which(argv[0]) is not None
    return {
        "id": cid,
        "label": label,
        "argv": list(argv),
        "reason": reason,
        "available": bool(available),
        "unavailable_reason": "" if available else f"未找到可执行文件：{argv[0]}",
    }


def _detect_node(root: Path) -> list[dict[str, Any]]:
    pkg = _load_json(root / "package.json")
    if not isinstance(pkg, dict):
        return []
    scripts = pkg.get("scripts")
    if not isinstance(scripts, dict):
        return []

    pm = "npm"
    if (root / "pnpm-lock.yaml").is_file():
        pm = "pnpm"
    elif (root / "yarn.lock").is_file():
        pm = "yarn"

    def _real(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    out: list[dict[str, Any]] = []
    test_script = scripts.get("test")
    if _real(test_script) and _NPM_DEFAULT_TEST_MARKER not in test_script:
        out.append(_candidate(
            cid=f"{pm}-test",
            label="运行测试",
            argv=[pm, "test", "--silent"],
            reason="项目里配好了测试命令",
        ))
    if _real(scripts.get("lint")):
        out.append(_candidate(
            cid=f"{pm}-lint",
            label="运行代码检查",
            argv=[pm, "run", "lint"],
            reason="项目里配好了检查命令",
        ))
    if _real(scripts.get("build")):
        out.append(_candidate(
            cid=f"{pm}-build",
            label="构建项目",
            argv=[pm, "run", "build"],
            reason="项目里配好了构建命令",
        ))
    return out


def _pytest_config_reason(root: Path) -> str:
    if (root / "pytest.ini").is_file():
        return "pytest.ini"
    pyproject = _read_text_capped(root / "pyproject.toml")
    if pyproject is not None and "[tool.pytest.ini_options]" in pyproject:
        return "pyproject.toml 的 [tool.pytest.ini_options]"
    setup_cfg = _read_text_capped(root / "setup.cfg")
    if setup_cfg is not None and "[tool:pytest]" in setup_cfg:
        return "setup.cfg 的 [tool:pytest]"
    if (root / "conftest.py").is_file():
        return "conftest.py"
    return ""


def _detect_python(root: Path) -> list[dict[str, Any]]:
    reason = _pytest_config_reason(root)
    if reason:
        return [_candidate(
            cid="pytest",
            label="运行测试",
            argv=["python3", "-m", "pytest", "-q"],
            reason=reason,
        )]
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        try:
            has_tests = any(tests_dir.glob("test_*.py"))
        except OSError:
            has_tests = False
        if has_tests:
            return [_candidate(
                cid="unittest",
                label="运行测试",
                argv=["python3", "-m", "unittest", "discover", "-s", "tests"],
                reason="这个仓库里有测试文件",
            )]
    return []


def _detect_makefile(root: Path) -> list[dict[str, Any]]:
    text = _read_text_capped(root / "Makefile")
    if text is None:
        return []
    if _MAKE_TEST_RE.search(text):
        return [_candidate(
            cid="make-test",
            label="运行测试",
            argv=["make", "test"],
            reason="项目里配好了测试命令",
        )]
    return []


def _detect_go(root: Path) -> list[dict[str, Any]]:
    if (root / "go.mod").is_file():
        return [_candidate(
            cid="go-test",
            label="运行测试",
            argv=["go", "test", "./..."],
            reason="这是一个 Go 项目",
        )]
    return []


def _detect_rust(root: Path) -> list[dict[str, Any]]:
    if (root / "Cargo.toml").is_file():
        return [_candidate(
            cid="cargo-test",
            label="运行测试",
            argv=["cargo", "test", "--quiet"],
            reason="这是一个 Rust 项目",
        )]
    return []


def detect_candidates(repo_path: str) -> list[dict[str, Any]]:
    """Return server-detected validation candidates for ``repo_path``.

    Scans only the repository root (plus the ``tests/`` dir contents). Never
    executes anything and tolerates unreadable/garbage files by skipping them.
    """
    if not isinstance(repo_path, str) or not repo_path.strip():
        return []
    try:
        root = Path(os.path.realpath(os.path.expanduser(repo_path.strip())))
    except (OSError, ValueError):
        return []
    if not root.is_dir():
        return []
    candidates: list[dict[str, Any]] = []
    candidates.extend(_detect_node(root))
    candidates.extend(_detect_python(root))
    candidates.extend(_detect_makefile(root))
    candidates.extend(_detect_go(root))
    candidates.extend(_detect_rust(root))
    return candidates


# ---------------------------------------------------------------------------
# Env spec parsing (mirrors agent_supervisor.load_validation_commands / config)
# ---------------------------------------------------------------------------

def _read_env_specs(settings: Any) -> list[tuple[str, tuple[str, ...]]]:
    raw = getattr(settings, "agent_supervisor_validation_commands", ()) or ()
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[tuple[str, tuple[str, ...]]] = []
    for entry in raw:
        name, argv = "", None
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            argv = entry.get("argv")
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            name = str(entry[0] or "").strip()
            argv = entry[1]
        if (
            name
            and isinstance(argv, (list, tuple))
            and argv
            and all(isinstance(a, str) and a for a in argv)
        ):
            out.append((name, tuple(argv)))
    return out


def _decode_ids(text: Any) -> list[str]:
    try:
        val = json.loads(text) if text else []
    except (ValueError, TypeError):
        return []
    if not isinstance(val, list):
        return []
    return [str(x) for x in val if isinstance(x, str) and x.strip()]


def _decode_specs(text: Any) -> list[tuple[str, tuple[str, ...]]]:
    try:
        val = json.loads(text) if text else []
    except (ValueError, TypeError):
        return []
    if not isinstance(val, list):
        return []
    out: list[tuple[str, tuple[str, ...]]] = []
    for entry in val:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        argv = entry.get("argv")
        if (
            name
            and isinstance(argv, list)
            and argv
            and all(isinstance(a, str) and a for a in argv)
        ):
            out.append((name, tuple(argv)))
    return out


# ---------------------------------------------------------------------------
# Storage (supervisor service DB)
# ---------------------------------------------------------------------------

def _ensure_schema(conn: Any) -> None:
    conn.executescript(_VALIDATION_SCHEMA_SQL)


def _load_stored(
    settings: Any, repo_id: str
) -> tuple[list[str], list[tuple[str, tuple[str, ...]]]] | None:
    import supervisor_service as svc

    conn = svc._connect(settings)
    try:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT selected_json, specs_json FROM supervisor_repo_validation WHERE repo_id = ?",
            (repo_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _decode_ids(row["selected_json"]), _decode_specs(row["specs_json"])


def _store(
    settings: Any,
    repo_id: str,
    selected: list[str],
    specs: list[tuple[str, tuple[str, ...]]],
) -> None:
    import supervisor_service as svc

    now = svc._utc_now()
    selected_json = json.dumps(selected, ensure_ascii=False)
    specs_json = json.dumps(
        [{"name": name, "argv": list(argv)} for name, argv in specs],
        ensure_ascii=False,
    )
    conn = svc._connect(settings)
    try:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT OR REPLACE INTO supervisor_repo_validation
               (repo_id, selected_json, specs_json, updated_at)
               VALUES (?, ?, ?, ?)""",
            (repo_id, selected_json, specs_json, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def compute_validation_source(settings: Any, repo_id: str) -> str:
    """Return ``"env"`` | ``"repo"`` | ``"none"`` for a repository.

    ``env`` when operator env/settings specs exist (they always win); ``repo``
    when the owner stored a non-empty per-repo selection; otherwise ``none``.
    """
    if _read_env_specs(settings):
        return "env"
    stored = _load_stored(settings, repo_id)
    if stored and stored[1]:
        return "repo"
    return "none"


def load_repo_validation_specs(
    settings: Any, repo_path: str | None
) -> list[tuple[str, tuple[str, ...]]]:
    """Return the stored per-repo specs for the repo whose canonical path equals
    ``repo_path``. Empty when no matching registered repo or no stored selection.

    Fails safe to empty (the caller then reaches ``needs_validation`` rather than
    fabricating completion) if the supervisor service DB is unavailable.
    """
    if not repo_path:
        return []
    try:
        import supervisor_service as svc

        canon = svc._canonical_internal_path(str(repo_path))
        if not canon:
            return []
        conn = svc._connect(settings)
        try:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT id FROM supervisor_repositories WHERE canonical_path = ?",
                (canon,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return []
        stored = _load_stored(settings, str(row["id"]))
        if stored and stored[1]:
            return stored[1]
    except Exception:
        return []
    return []


def get_repo_validation(settings: Any, repo_id: str) -> dict[str, Any]:
    """Return the validation payload for a repository.

    Payload keys: ``candidates`` (server-detected), ``selected`` (owner-selected
    IDs), ``source`` ("env"|"repo"|"none"), ``env_commands`` (names from env),
    ``effective`` (the specs that would actually run).
    """
    import supervisor_service as svc

    svc.require_enabled(settings)
    rid = svc._validate_id_token(repo_id, "repo_id")
    repo_row = svc._get_repo_row(settings, rid)  # 404 when missing
    canon = str(repo_row["canonical_path"])

    candidates = detect_candidates(canon)
    stored = _load_stored(settings, rid)
    selected = stored[0] if stored else []
    stored_specs = stored[1] if stored else []
    env_specs = _read_env_specs(settings)
    env_commands = [name for name, _ in env_specs]
    if env_specs:
        source = "env"
        effective_specs = env_specs
    elif stored_specs:
        source = "repo"
        effective_specs = stored_specs
    else:
        source = "none"
        effective_specs = []
    effective = [
        {"name": name, "argv": list(argv)}
        for name, argv in effective_specs
    ]
    return {
        "repo_id": rid,
        "candidates": candidates,
        "selected": selected,
        "source": source,
        "env_commands": env_commands,
        "effective": effective,
    }


def set_repo_validation(
    settings: Any, repo_id: str, selected_ids: Any
) -> dict[str, Any]:
    """Validate owner-selected IDs against the repo's CURRENTLY detected
    candidates, then store the IDs plus the server-derived specs.

    Unknown ID -> ``SupervisorServiceError`` 400 code ``unknown_check``.
    An empty list clears the selection.
    """
    import supervisor_service as svc

    svc.require_enabled(settings)
    rid = svc._validate_id_token(repo_id, "repo_id")
    repo_row = svc._get_repo_row(settings, rid)  # 404 when missing
    canon = str(repo_row["canonical_path"])

    if not isinstance(selected_ids, list):
        raise svc.SupervisorServiceError(
            svc.HTTPStatus.BAD_REQUEST,
            "请选要运行的检查项目（列表）",
            code="invalid_input",
        )

    candidates = detect_candidates(canon)
    by_id = {c["id"]: c for c in candidates}

    clean_ids: list[str] = []
    seen: set[str] = set()
    for raw in selected_ids:
        if not isinstance(raw, str):
            raise svc.SupervisorServiceError(
                svc.HTTPStatus.BAD_REQUEST,
                "检查项目的编号需要是文字",
                code="unknown_check",
            )
        cid = raw.strip()
        if cid not in by_id:
            raise svc.SupervisorServiceError(
                svc.HTTPStatus.BAD_REQUEST,
                f"没有这个检查项目：{cid}",
                code="unknown_check",
            )
        if cid in seen:
            continue
        seen.add(cid)
        clean_ids.append(cid)

    specs = [(cid, tuple(by_id[cid]["argv"])) for cid in clean_ids]
    _store(settings, rid, clean_ids, specs)
    return get_repo_validation(settings, rid)
