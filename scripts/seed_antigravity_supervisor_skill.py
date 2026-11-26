#!/usr/bin/env python3
"""Seed the antigravity-supervisor skill into the Conveyor skills store."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import load_runtime_settings
from personal_tools import skills

md_path = ROOT / "skills" / "agent-supervisor.md"
text = md_path.read_text(encoding="utf-8")
fields = skills.parse_markdown(text)

settings = load_runtime_settings()
try:
    existing = skills.get_skill(settings, fields.get("slug", "antigravity-supervisor"))
    if existing is None:
        item = skills.create_skill(settings, **fields)
        print(f"created skill: {item['slug']}")
    else:
        item = skills.update_skill(settings, existing["slug"], **{k: v for k, v in fields.items() if k != "slug"})
        print(f"updated skill: {item['slug']}")
except Exception as exc:
    print(f"seed failed: {exc}")
    raise
PY