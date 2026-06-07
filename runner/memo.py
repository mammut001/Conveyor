"""runner/memo.py — split out of runner.py.

The original runner.py was 2005 lines and 5 big
responsibilities. This file is one slice.

runner/core.py attaches each function on this module
to the CodexRunner class as a method at import time,
so callers see the same public surface.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from config import Settings, load_settings
from redaction import redact_text, safe_json, truncate
from scripts.job_metadata import job_sort_time, load_job_metadata, metadata_text
_DEDUP_TIMESTAMP_PREFIX_RE = re.compile(r"^\s*\[[^\]]*\]\s*")


def _normalize_for_dedup(line: str) -> str:
    """Reduce a memo bullet line to a comparable form.

    Strips the leading bullet ("- " / "* "), the optional
    "[YYYY-MM-DD HH:MM] " timestamp prefix, then lowercases and collapses
    internal whitespace. Returns "" for empty / bullet-only input.
    """
    if not line:
        return ""
    s = line.strip()
    if s.startswith(("- ", "* ")):
        s = s[2:]
    elif s in ("-", "*"):
        return ""
    s = _DEDUP_TIMESTAMP_PREFIX_RE.sub("", s)
    s = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _find_duplicate_line(existing_text: str, normalized_new: str) -> str | None:
    """Return the first existing bullet line in `existing_text` whose
    normalized form equals `normalized_new`. Scans all sections, not just
    the target one — that is the whole point. Returns None if no
    duplicate is found.
    """
    if not normalized_new:
        return None
    for line in existing_text.splitlines():
        if not line.lstrip().startswith("-"):
            continue
        if _normalize_for_dedup(line) == normalized_new:
            return line
    return None


def _ensure_section(self, text: str, heading: str) -> str:
    marker = f"## {heading}"
    if marker in text:
        return text
    suffix = "" if text.endswith("\n") or not text else "\n"
    return f"{text}{suffix}\n{marker}\n"


def _extract_section(self, text: str, heading: str) -> str:
    if not text:
        return ""
    marker = f"## {heading}"
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.strip() == marker:
            start = idx + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for idx in range(start, len(lines)):
        if lines[idx].startswith("## "):
            end = idx
            break
    return "\n".join(lines[start:end]).strip()


def _insert_line_in_section(self, text: str, heading: str, line: str) -> str:
    """Append a markdown list line at the end of section `## heading`.

    Preserves the existing blank line that separates this section from
    the next `## ` heading. If `heading` is missing, returns text
    unchanged (callers should call _ensure_section first). Insertion
    goes at the *end of the section*, not at the end of the file, so
    a new fact lands under `## fact` even when later sections exist.
    """
    marker = f"## {heading}"
    lines = text.splitlines()
    if marker not in lines:
        return text
    start = lines.index(marker) + 1
    end = len(lines)
    for idx in range(start, len(lines)):
        if lines[idx].startswith("## "):
            end = idx
            break
    body = lines[start:end]
    # Drop trailing blank lines so the new line slots in flush after
    # the last existing entry; we'll re-emit one blank to keep the
    # section separator intact.
    while body and not body[-1].strip():
        body.pop()
    body.append(line)
    if end < len(lines):
        # Mid-file section: keep the blank-line separator before the
        # next heading.
        new_lines = lines[:start] + body + [""] + lines[end:]
    else:
        # Last section: no separator needed.
        new_lines = lines[:start] + body
    return "\n".join(new_lines) + "\n"


async def append_memo(
    self,
    category: str,
    content: str,
    *,
    auto_timestamp: bool = False,
) -> str:
    if category not in self.MEMO_CATEGORIES:
        raise ValueError(f"Unknown memo category: {category!r}")
    content = (content or "").strip()
    if not content:
        raise ValueError("Memo content is empty.")
    worktree = await self._ensure_today_worktree()
    memory_path = self._memory_path(worktree)
    existing = ""
    if memory_path.exists():
        try:
            existing = memory_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
    if not existing.strip():
        stamp = self._user_today().strftime(self.DAILY_WORKTREE_FORMAT)
        existing = f"# MEMORY.md — {stamp}\n\n"
    existing = self._ensure_section(existing, category)
    line = f"- {content}"
    if auto_timestamp and category == "fact":
        line = f"- [{self._now_local_str()}] {content}"
    # Dedup across sections: skip if a normalized version of this
    # content already lives anywhere in MEMORY.md. Catches the
    # "model re-memorizes the same thing N times" failure mode that
    # produced today's messy file (4x TSLA, 3x smoke, etc.).
    existing_duplicate = _find_duplicate_line(existing, _normalize_for_dedup(line))
    if existing_duplicate is not None:
        preview = truncate(content, 60)
        return f"已存在: {category} · {preview} (跳过重复)"
    new_text = self._insert_line_in_section(existing, category, line)
    tmp_path = memory_path.with_name(memory_path.name + ".tmp")
    tmp_path.write_text(new_text, encoding="utf-8")
    tmp_path.replace(memory_path)
    preview = truncate(content, 60)
    return f"记下了: {category} · {preview}"


def read_memory(self, category: str | None = None) -> str:
    text = self.today_memory_text()
    if not text:
        return ""
    if category is None:
        return text
    if category not in self.MEMO_CATEGORIES:
        return ""
    section = self._extract_section(text, category)
    if not section:
        return ""
    return f"## {category}\n{section}\n"


def read_journal(self, date_str: str, category: str | None = None) -> str:
    # date_str is YYYY-MM-DD; the curator writes one file per day to
    # ~/.codex/JOURNAL/ during the 12pm gate.
    path = self.settings.codex_memory_root / "JOURNAL" / f"{date_str}.md"
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if category is None:
        return text.strip()
    if category not in self.MEMO_CATEGORIES:
        return ""
    section = self._extract_section(text, category)
    if not section:
        return ""
    return f"## {category}\n{section}\n"


async def reclassify_unfiled(self, content: str) -> tuple[str, int]:
    """Re-classify every line in the ## unfiled section of MEMORY.md content.

    For each "- ..." line under "## unfiled", re-call classify_memo. If the
    classifier returns a real category (preference / fact / tool-quirk /
    convention), move the line into that section. Lines the classifier
    still can't place stay in ## unfiled. Never raises — classify_memo
    itself swallows errors and returns "unfiled" on any failure.

    Returns (new_content, reclassified_count). If the input has no
    ## unfiled section, or no movable lines, the content is returned
    unchanged and the count is 0.
    """
    if not content or "## unfiled" not in content:
        return content, 0

    # Walk the file once, splitting into preamble + per-section bodies in
    # order of appearance. Unknown headings (e.g. user-written "## notes")
    # are preserved under their original name; we only mutate the five
    # MEMO_CATEGORIES sections.
    lines = content.splitlines()
    preamble: list[str] = []
    sections: dict[str, list[str]] = {}
    other_headings: list[tuple[str, list[str]]] = []
    current_heading: str | None = None
    current_body: list[str] | None = None

    for line in lines:
        if line.startswith("## "):
            if current_heading is not None and current_body is not None:
                if current_heading in self.MEMO_CATEGORIES:
                    sections[current_heading] = current_body
                else:
                    other_headings.append((current_heading, current_body))
            current_heading = line[3:].strip()
            current_body = []
        elif current_body is None:
            preamble.append(line)
        else:
            current_body.append(line)
    if current_heading is not None and current_body is not None:
        if current_heading in self.MEMO_CATEGORIES:
            sections[current_heading] = current_body
        else:
            other_headings.append((current_heading, current_body))

    unfiled_body = sections.get("unfiled", [])
    if not unfiled_body:
        return content, 0

    moved_count = 0
    new_unfiled: list[str] = []
    for raw_line in unfiled_body:
        stripped = raw_line.strip()
        if not stripped.startswith("- "):
            new_unfiled.append(raw_line)
            continue
        text = stripped[2:].strip()
        if not text:
            new_unfiled.append(raw_line)
            continue
        new_category = await self.classify_memo(text)
        if new_category in self.MEMO_CATEGORIES and new_category != "unfiled":
            sections.setdefault(new_category, []).append(raw_line)
            moved_count += 1
        else:
            new_unfiled.append(raw_line)
    sections["unfiled"] = new_unfiled

    if moved_count == 0:
        return content, 0

    # Reassemble in canonical order: preamble, then known categories in
    # MEMO_CATEGORIES order, then unknown headings (preserved), then
    # "unfiled" last so the catch-all is always at the bottom.
    out: list[str] = list(preamble)
    for cat in self.MEMO_CATEGORIES:
        body = sections.get(cat, [])
        while body and not body[-1].strip():
            body = body[:-1]
        if not body:
            continue
        if out and out[-1] != "":
            out.append("")
        out.append(f"## {cat}")
        out.extend(body)
        out.append("")
    for heading, body in other_headings:
        while body and not body[-1].strip():
            body = body[:-1]
        if not body:
            continue
        if out and out[-1] != "":
            out.append("")
        out.append(f"## {heading}")
        out.extend(body)
        out.append("")

    new_content = "\n".join(out).rstrip() + "\n"
    return new_content, moved_count


async def classify_memo(self, content: str) -> str:
    # Fast, low-cost classifier. Any failure (no key, network error,
    # bad JSON, garbage output, timeout) lands in "unfiled" — the
    # curator re-classifies those at 12pm. Never raise to the caller.
    api_key = os.getenv("MINIMAX_API_KEY", "").strip()
    base_url = os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1").rstrip("/")
    if not api_key or not content.strip():
        return "unfiled"
    model = os.getenv("MINIMAX_CLASSIFY_MODEL", "minimax-text-01")
    prompt = (
        "Classify the following user note into exactly one of: "
 "preference, fact, tool-quirk, convention.\n"
        "Reply with one lowercase word, nothing else.\n\n"
        f"Note: {content.strip()}"
    )
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 8,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    def _post() -> str:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8", errors="replace")

    try:
        raw = await asyncio.wait_for(asyncio.to_thread(_post), timeout=10)
    except Exception:
        return "unfiled"
    try:
        data = json.loads(raw)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = (message.get("content") or "").strip().lower()
    except (ValueError, AttributeError, IndexError, TypeError):
        return "unfiled"
    # Token-level match — model may echo whitespace or punctuation.
    for category in self.MEMO_CATEGORIES:
        if category in text:
            if category == "unfiled":
                # "unfiled" is not a valid classifier answer; keep the
                # unclassified fallback in the same name.
                return "unfiled"
            return category
    return "unfiled"
