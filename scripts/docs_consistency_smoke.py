#!/usr/bin/env python3
"""docs_consistency_smoke.py — anchor-only cross-doc check.

Pure file-content check. No network, no env, no Telegram.
Verifies that the two architecture docs and the two READMEs exist
and mention the same key concepts. Brittle on prose on purpose —
this is an anchor list, not a style check.

Run: .venv/bin/python scripts/docs_consistency_smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.harness_common import CheckResult, print_results  # noqa: E402

ARCH_ZH = REPO / "docs" / "architecture.md"
ARCH_EN = REPO / "docs" / "architecture.en.md"
README_ZH = REPO / "README.zh.md"
README_EN = REPO / "README.md"

# Anchors the arch docs must each contain.
ARCH_ANCHORS = (
    "Agent tool layer",
    "/diagnose",
    "/restart",
    "/audit_tools",
    "telegram_live_smoke.py",
    "telegram_live_helpers_smoke.py",
)

# Anchors the READMEs must each reference.
README_EN_REF = "docs/architecture.en.md"
README_ZH_REF = "docs/architecture.md"


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


def main() -> int:
    results: list[CheckResult] = [
        _exists(ARCH_ZH, "docs/architecture.md"),
        _exists(ARCH_EN, "docs/architecture.en.md"),
    ]
    results += _contains_all(ARCH_ZH, list(ARCH_ANCHORS), "architecture.md")
    results += _contains_all(ARCH_EN, list(ARCH_ANCHORS), "architecture.en.md")
    results.append(_has_reference(README_EN, README_EN_REF, "README.md"))
    results.append(_has_reference(README_ZH, README_ZH_REF, "README.zh.md"))
    print_results(results)
    ok = all(r.ok for r in results)
    print("docs consistency smoke ok" if ok else "docs consistency smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())