#!/usr/bin/env python3
"""import_boundary_smoke.py — static AST check for the layer rules.

Pure file-content check. No network, no env. Verifies that:
  * `handlers/` files do not import the Telegram SDK or lark_oapi.
  * `channel/telegram.py` does not import lark_oapi.
  * `channel/feishu.py` does not import the Telegram SDK.
  * `channel/*.py` does not import `runner` directly.

Uses a simple regex-based string scan over the `import ...` /
`from ... import ...` lines so we do not depend on `ast` parsing
quirks. This is intentionally lightweight: the rule is "the bare
module name must not appear in an import statement", which is enough
to catch a developer accidentally adding a channel SDK to a handler
or a runner import into a channel adapter.

Run: .venv/bin/python scripts/import_boundary_smoke.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.harness_common import CheckResult, print_results  # noqa: E402

# Patterns we scan each file for in import statements.
_TELEGRAM_MODULES = {"telegram", "telegram.ext", "telegram.ext.filters"}
_FEISHU_MODULES = {"lark_oapi", "lark_oapi.channel"}
_RUNNER_MODULE = "runner"

_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([\w.]+)|import\s+([\w.]+))",
    re.MULTILINE,
)


def _scan(path: Path) -> set[str]:
    """Return the set of top-level package names that appear in
    `import X` or `from X import Y` statements in `path`."""
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8", errors="replace")
    names: set[str] = set()
    for match in _IMPORT_RE.finditer(text):
        head = match.group(1) or match.group(2)
        if not head:
            continue
        top = head.split(".")[0]
        names.add(top)
        # also record dotted forms for exact checks
        if "." in head:
            names.add(head)
    return names


def _check_no_imports(
    label: str, paths: list[Path], forbidden: set[str]
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for path in paths:
        names = _scan(path)
        hits = sorted(names & forbidden)
        results.append(
            CheckResult(
                f"{label} ({path.relative_to(REPO)}) avoids {sorted(forbidden)}",
                not hits,
                f"hits={hits}" if hits else "ok",
            )
        )
    return results


def main() -> int:
    handlers_dir = REPO / "handlers"
    handler_files = sorted(handlers_dir.rglob("*.py"))
    channel_telegram = REPO / "channel" / "telegram.py"
    channel_feishu = REPO / "channel" / "feishu.py"
    channel_files = [channel_telegram, channel_feishu]

    results: list[CheckResult] = []
    # Handlers must not import Telegram or Feishu SDKs.
    results += _check_no_imports(
        "handlers/ Telegram/Feishu SDK",
        handler_files,
        _TELEGRAM_MODULES | _FEISHU_MODULES,
    )
    # channel/telegram.py must not import lark_oapi.
    results += _check_no_imports(
        "channel/telegram.py: no Feishu SDK",
        [channel_telegram],
        _FEISHU_MODULES,
    )
    # channel/feishu.py must not import Telegram SDK.
    results += _check_no_imports(
        "channel/feishu.py: no Telegram SDK",
        [channel_feishu],
        _TELEGRAM_MODULES,
    )
    # channel/*.py must not import runner.
    results += _check_no_imports(
        "channel/*.py: no runner import",
        channel_files,
        {_RUNNER_MODULE},
    )
    print_results(results)
    ok = all(r.ok for r in results)
    print("import boundary smoke ok" if ok else "import boundary smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())