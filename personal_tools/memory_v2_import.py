"""Explicit, idempotent importer for existing Conveyor memory data.

Nothing imports automatically on startup. A dry run neither creates nor updates
the v2 database. Journals and conversations are imported as untrusted daily log
data, never as operator profile/facts. Secrets are filtered by the v2 validator.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from personal_tools import memory_v2


def _legacy(settings: Any) -> Iterator[tuple[str, str, str, str, str, float]]:
    path = Path(settings.codex_memory_root) / "long_term_memory.db"
    if not path.exists():
        return
    with sqlite3.connect(path) as db:
        found = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'"
        ).fetchone()
        if not found:
            return
        # Original v1 rows include operator_id, kind, source_channel, created_at.
        for row in db.execute(
            "SELECT id,operator_id,kind,text,created_at FROM memories ORDER BY id"
        ).fetchall():
            ident, who, kind, text, created = row
            tier = "profile" if kind == "profile" else "facts"
            yield (f"legacy:{ident}", who, tier, text, "legacy-explicit", float(created))


def _markdown_lines(settings: Any) -> Iterator[tuple[str, str, str, str, str, float]]:
    root = Path(settings.codex_memory_root)
    paths = sorted((root / "JOURNAL").glob("*.md")) if (root / "JOURNAL").exists() else []
    worktrees = Path(settings.codex_task_root) / "worktrees"
    if worktrees.exists():
        paths.extend(sorted(worktrees.glob("day-*/MEMORY.md")))
    for path in paths[:2000]:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            ts = path.stat().st_mtime
        except OSError:
            continue
        for number, raw in enumerate(content.splitlines(), 1):
            text = raw.strip().lstrip("- ").strip()
            if not text or text.startswith("#") or text.startswith("worktree:"):
                continue
            # Journal material, unlike explicit legacy remembers, is untrusted
            # contextual history and must never enter the always-injected profile.
            yield (f"journal:{path}:{number}:{raw}", "owner", "log", text,
                   "untrusted", ts)


def _conversations(settings: Any) -> Iterator[tuple[str, str, str, str, str, float]]:
    root = Path(settings.codex_memory_root)
    chat_db = root / "chat_memory.db"
    if chat_db.exists():
        with sqlite3.connect(chat_db) as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_turns'"
            ).fetchone()
            if table:
                for ident, key, role, text, ts in db.execute(
                    "SELECT id,chat_key,role,content,created_at FROM chat_turns ORDER BY id LIMIT 50000"
                ):
                    # Raw conversation text does not become globally visible facts.
                    yield (f"chat:{ident}", f"import:chat:{key[:100]}", "log",
                           f"{role}: {text}", "untrusted", float(ts))
    for path in sorted((root / "session").glob("*.jsonl"))[:1000]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[:10000]
            ts = path.stat().st_mtime
        except OSError:
            continue
        for line_no, line in enumerate(lines, 1):
            try:
                obj = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            value = obj.get("content") or obj.get("text")
            if not isinstance(value, str):
                continue
            yield (f"session:{path}:{line_no}:{line}", f"import:session:{path.stem}",
                   "log", value, "untrusted", ts)


def import_existing(
    settings: Any, *, dry_run: bool = False, include_conversations: bool = False,
) -> dict[str, int]:
    if not memory_v2.enabled(settings):
        raise ValueError("set CONVEYOR_MEMORY_V2=1 before importing")
    sources = [_legacy(settings), _markdown_lines(settings)]
    if include_conversations:
        sources.append(_conversations(settings))
    totals = {"candidates": 0, "imported": 0, "already_present": 0, "skipped": 0}
    for stream in sources:
        for key, who, tier, text, source, ts in stream:
            totals["candidates"] += 1
            try:
                value = memory_v2._validate(text, tier, source)
            except ValueError:
                totals["skipped"] += 1
                continue
            if dry_run:
                totals["imported"] += 1  # eligible, not actually written
                continue
            # Re-check every source key before insertion. The unique hashed
            # import_key and a single INSERT prevent duplicate re-imports.
            from hashlib import sha256
            digest = sha256(key.encode("utf-8")).hexdigest()
            with memory_v2.connect(settings) as db:
                exists = db.execute(
                    "SELECT id FROM memory_v2_entries WHERE import_key=?", (digest,)
                ).fetchone()
            if exists:
                totals["already_present"] += 1
                continue
            try:
                memory_v2.remember_fact(
                    settings, who, value, tier=tier, source=source,
                    now=ts, import_key=key,
                )
                totals["imported"] += 1
            except ValueError:
                # A filled profile never silently demotes to facts/log.
                totals["skipped"] += 1
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=".env")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include-conversations", action="store_true")
    args = parser.parse_args()
    from config import load_settings
    result = import_existing(load_settings(args.env), dry_run=args.dry_run,
                             include_conversations=args.include_conversations)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
