#!/usr/bin/env python3
"""Seed the generic Agent Supervisor skill into Conveyor personal_tools.skills store."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from personal_tools import skills


def make_seed_settings(state_dir: str | Path | None = None) -> Any:
    """Create settings instance pointing to isolated state_dir or runtime defaults."""
    if state_dir:
        resolved = Path(state_dir).expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        try:
            from conveyor.core.config import Settings

            return Settings(
                telegram_bot_token="",
                telegram_allowed_user_id=0,
                codex_workspace_root=resolved / "workspace",
                codex_bin="codex",
                codex_task_root=resolved / "tasks",
                codex_model=None,
                codex_timeout_seconds=60,
                telegram_progress_seconds=3,
                codex_retry_429_delays_seconds=(),
                codex_memory_root=resolved,
                user_timezone="UTC",
            )
        except Exception:
            class _IsolatedSettings:
                def __init__(self, root: Path) -> None:
                    self.codex_memory_root = root

            return _IsolatedSettings(resolved)

    try:
        from conveyor.core.config import load_runtime_settings

        return load_runtime_settings()
    except Exception:
        from conveyor.core.config import load_settings

        return load_settings()


def seed_skill(
    settings: Any,
    markdown_path: str | Path,
    *,
    slug: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Parse markdown skill definition and idempotently create/update in skills store."""
    md_path = Path(markdown_path).expanduser().resolve()
    if not md_path.is_file():
        raise FileNotFoundError(f"Skill markdown file not found: {md_path}")

    text = md_path.read_text(encoding="utf-8")
    fields = skills.parse_markdown(text)
    target_slug = (slug or fields.get("slug") or "agent-supervisor").strip()
    fields["slug"] = target_slug

    if dry_run:
        return {"action": "dry_run", "slug": target_slug, "fields": fields}

    existing = skills.get_skill(settings, target_slug)
    if existing is None:
        item = skills.create_skill(settings, **fields)
        action = "created"
    else:
        update_fields = {k: v for k, v in fields.items() if k != "slug"}
        item = skills.update_skill(settings, existing["slug"], **update_fields)
        action = "updated"

    return {"action": action, "slug": item["slug"], "item": item}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed the Agent Supervisor skill into Conveyor personal_tools.skills store."
    )
    parser.add_argument(
        "--state-dir",
        type=str,
        default=None,
        help="Path to isolated state directory (sets codex_memory_root without touching user state).",
    )
    parser.add_argument(
        "--markdown-path",
        type=str,
        default=str(ROOT / "skills" / "agent-supervisor.md"),
        help="Path to skill markdown file (default: skills/agent-supervisor.md).",
    )
    parser.add_argument(
        "--slug",
        type=str,
        default=None,
        help="Optional slug override (default: parsed from frontmatter).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate markdown only, do not write to database.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = make_seed_settings(args.state_dir)
    try:
        res = seed_skill(
            settings,
            args.markdown_path,
            slug=args.slug,
            dry_run=args.dry_run,
        )
        print(f"{res['action']} skill: {res['slug']}")
        return 0
    except Exception as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
