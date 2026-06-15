#!/usr/bin/env python3
"""docs_consistency_smoke.py — cross-doc + runtime anchor checks.

Pure file-content check. No network, no env, no Telegram.
Verifies architecture/README docs, runtime sandbox mode, and active
service naming stay aligned. Brittle on prose on purpose — anchor list,
not a style check.

Run: .venv/bin/python scripts/docs_consistency_smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from runner.types import JobMode  # noqa: E402
from scripts.harness_common import CheckResult, print_results  # noqa: E402

ARCH_ZH = REPO / "docs" / "architecture.md"
ARCH_EN = REPO / "docs" / "architecture.en.md"
README_ZH = REPO / "README.zh.md"
README_EN = REPO / "README.md"
HELP_SRC = REPO / "handlers" / "commands.py"
SECURITY = REPO / "SECURITY.md"
CONTRIBUTING = REPO / "CONTRIBUTING.md"

# Anchors the arch docs must each contain.
ARCH_ANCHORS = (
    "Agent tool layer",
    "/diagnose",
    "/restart",
    "/audit_tools",
    "danger-full-access",
    "telegram_live_smoke.py",
    "telegram_live_helpers_smoke.py",
)

# Anchors the READMEs must each reference.
README_EN_REF = "docs/architecture.en.md"
README_ZH_REF = "docs/architecture.md"

SANDBOX_DOC_PATHS = (
    README_EN,
    README_ZH,
    ARCH_ZH,
    ARCH_EN,
    SECURITY,
    CONTRIBUTING,
)

FORBIDDEN_SANDBOX_PHRASES = (
    "danger-full-access is never used",
    "danger-full-access **永远不用**",
    "`danger-full-access` is never used",
    "never used.",
)

STALE_SERVICE = "codex-telegram-bot"
ACTIVE_SERVICE_SCAN_ROOTS = (
    REPO / "handlers",
    REPO / "scripts",
)
ACTIVE_SERVICE_SCAN_FILES = (
    REPO / "scripts" / "deploy.sh",
    REPO / "scripts" / "deploy_vps.sh",
    REPO / "scripts" / "install-remote.sh",
)

# Historical changelog entries may mention old names; skip them.
STALE_SERVICE_ALLOWLIST = {
    REPO / "CHANGELOG.md",
    REPO / "scripts" / "docs_consistency_smoke.py",
}


def _exists(path: Path, label: str) -> CheckResult:
    return CheckResult(f"exists: {label}", path.exists(), str(path))


def _contains_all(path: Path, anchors: list[str], label: str) -> list[CheckResult]:
    if not path.exists():
        return [CheckResult(f"anchors: {label}", False, f"{path} missing")]
    text = path.read_text(encoding="utf-8", errors="replace")
    return [
        CheckResult(
            f"{label} contains {a!r}",
            a in text,
            f"len={len(text)}",
        )
        for a in anchors
    ]


def _has_reference(path: Path, needle: str, label: str) -> CheckResult:
    if not path.exists():
        return CheckResult(f"{label} references {needle!r}", False, f"{path} missing")
    text = path.read_text(encoding="utf-8", errors="replace")
    return CheckResult(
        f"{label} references {needle!r}",
        needle in text,
        f"len={len(text)}",
    )


def _runtime_sandbox() -> CheckResult:
    sandbox = JobMode.RUN.sandbox
    return CheckResult(
        "runtime: JobMode.RUN.sandbox",
        sandbox == "danger-full-access",
        f"got {sandbox!r}",
    )


def _help_sandbox() -> CheckResult:
    if not HELP_SRC.exists():
        return CheckResult("help mentions danger-full-access", False, f"{HELP_SRC} missing")
    text = HELP_SRC.read_text(encoding="utf-8", errors="replace")
    ok = "danger-full-access" in text and STALE_SERVICE not in text
    return CheckResult("help mentions danger-full-access", ok, f"len={len(text)}")


def _forbidden_sandbox_phrases() -> list[CheckResult]:
    results: list[CheckResult] = []
    for path in SANDBOX_DOC_PATHS:
        if not path.exists():
            results.append(CheckResult(f"sandbox docs exist: {path.name}", False, "missing"))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for phrase in FORBIDDEN_SANDBOX_PHRASES:
            if phrase in text:
                results.append(
                    CheckResult(
                        f"{path.name} forbids {phrase!r}",
                        False,
                        "stale sandbox claim",
                    )
                )
    if not results:
        results.append(CheckResult("sandbox docs: no forbidden stale claims", True, "ok"))
    return results


def _scan_stale_service(path: Path) -> list[tuple[int, str]]:
    if path in STALE_SERVICE_ALLOWLIST or not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    hits: list[tuple[int, str]] = []
    for idx, line in enumerate(lines, start=1):
        if STALE_SERVICE in line:
            hits.append((idx, line.strip()))
    return hits


def _active_service_names() -> list[CheckResult]:
    offenders: list[str] = []
    for root in ACTIVE_SERVICE_SCAN_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            for lineno, line in _scan_stale_service(path):
                rel = path.relative_to(REPO)
                offenders.append(f"{rel}:{lineno}: {line[:80]}")
    for path in ACTIVE_SERVICE_SCAN_FILES:
        for lineno, line in _scan_stale_service(path):
            rel = path.relative_to(REPO)
            offenders.append(f"{rel}:{lineno}: {line[:80]}")
    detail = "; ".join(offenders[:5])
    if len(offenders) > 5:
        detail += f"; … +{len(offenders) - 5} more"
    return [
        CheckResult(
            "active paths use conveyor-telegram-bot (not codex-telegram-bot)",
            not offenders,
            detail or "ok",
        )
    ]


def main() -> int:
    results: list[CheckResult] = [
        _exists(ARCH_ZH, "docs/architecture.md"),
        _exists(ARCH_EN, "docs/architecture.en.md"),
        _runtime_sandbox(),
        _help_sandbox(),
    ]
    results += _contains_all(ARCH_ZH, list(ARCH_ANCHORS), "architecture.md")
    results += _contains_all(ARCH_EN, list(ARCH_ANCHORS), "architecture.en.md")
    results.append(_has_reference(README_EN, README_EN_REF, "README.md"))
    results.append(_has_reference(README_ZH, README_ZH_REF, "README.zh.md"))
    for path in SANDBOX_DOC_PATHS:
        results.append(
            _has_reference(path, "danger-full-access", path.name),
        )
    results += _forbidden_sandbox_phrases()
    results += _active_service_names()
    print_results(results)
    ok = all(r.ok for r in results)
    print("docs consistency smoke ok" if ok else "docs consistency smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
