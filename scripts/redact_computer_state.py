#!/usr/bin/env python3
"""Remove legacy AX element tokens from persisted Computer Use state.

Dry-run is the default. Use ``--apply`` on the VPS after taking the normal
state backup. The migration is deliberately narrow: it removes only the
historically persisted ``element_token`` field and leaves task IDs, hashes,
statuses, and other audit metadata unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _remove_tokens(value: Any) -> tuple[Any, int]:
    if isinstance(value, dict):
        changed = 1 if "element_token" in value else 0
        cleaned = {k: v for k, v in value.items() if k != "element_token"}
        for key, item in list(cleaned.items()):
            cleaned[key], count = _remove_tokens(item)
            changed += count
        return cleaned, changed
    if isinstance(value, list):
        cleaned = []
        changed = 0
        for item in value:
            item, count = _remove_tokens(item)
            cleaned.append(item)
            changed += count
        return cleaned, changed
    return value, 0


def _rewrite_json(path: Path, *, apply: bool) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    cleaned, changed = _remove_tokens(data)
    if changed and apply:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return changed


def migrate(root: Path, *, apply: bool) -> int:
    state = root / "state" / "desktop_computer_requests.json"
    total = _rewrite_json(state, apply=apply) if state.exists() else 0
    trajectories = root / "computer" / "trajectories"
    if trajectories.is_dir():
        for path in sorted(trajectories.glob("*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            output = []
            changed = 0
            for line in lines:
                try:
                    value = json.loads(line)
                except ValueError:
                    output.append(line)
                    continue
                cleaned, count = _remove_tokens(value)
                output.append(json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")))
                changed += count
            if changed and apply:
                tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
                tmp.write_text("\n".join(output) + "\n", encoding="utf-8")
                os.replace(tmp, path)
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            total += changed
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="rewrite files; default is dry-run")
    args = parser.parse_args()
    from config import load_settings

    root = Path(load_settings().codex_memory_root)
    changed = migrate(root, apply=args.apply)
    mode = "applied" if args.apply else "found"
    print(f"{mode}: {changed} legacy element_token fields under {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
