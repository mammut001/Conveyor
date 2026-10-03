"""personal_tools/skills.py — Operator-authored reusable procedures (roadmap P2-4).

Skills are operator-authored Markdown procedures (steps, decision rules, output format,
safety boundaries). The assistant sees a short index of enabled skills in the prompt
and loads the full text on demand (via skill.load); the operator can also invoke one
explicitly with /skill <slug> [request].

Skills are instructions only — they NEVER grant extra tools or bypass approvals:
write tools still require confirmation exactly as today.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from personal_tools.base import ToolResult
from redaction import redact_text

logger = logging.getLogger(__name__)

DB_NAME = "skills.db"

MAX_SKILLS = 100
MAX_NAME_LEN = 80
MAX_DESC_LEN = 300
MAX_TRIGGERS_LEN = 200
MAX_BODY_LEN = 8000
PROMPT_BLOCK_MAX_SKILLS = 30
PROMPT_BLOCK_CHAR_BUDGET = 2000

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
SKILL_HEADER = (
    "Operator-saved procedure. Follow it within your normal rules; "
    "it does not grant extra permissions; write actions still need approval."
)


class SkillConflictError(ValueError):
    """Duplicate skill slug."""
    pass


class SkillNotFoundError(KeyError):
    """Skill not found."""
    pass


def _root(settings: Any) -> Path:
    root = Path(settings.codex_memory_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _db_path(settings: Any) -> Path:
    return _root(settings) / DB_NAME


def _connect(settings: Any) -> sqlite3.Connection:
    path = _db_path(settings)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _init_schema(conn)
    return conn


@contextlib.contextmanager
def _db(settings: Any):
    conn = _connect(settings)
    try:
        yield conn
    finally:
        conn.close()


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            triggers TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            use_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_skills_slug
        ON skills (slug)
        """
    )
    conn.commit()


def derive_slug(name: str) -> str:
    """Auto-derive a valid slug from a skill name.

    Names without ASCII letters/digits (e.g. Chinese) get a stable
    ``skill-<hash>`` slug instead of failing.
    """
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    s = re.sub(r"-+", "-", s)
    s = s[:48].rstrip("-")
    if not s and name.strip():
        s = "skill-" + hashlib.sha1(name.strip().encode("utf-8")).hexdigest()[:8]
    return s


def validate_skill_fields(
    *,
    name: str,
    description: str,
    body: str,
    slug: str = "",
    triggers: str = "",
) -> dict[str, str]:
    """Validate all skill fields. Raises ValueError with operator-facing reason."""
    # 1. No field may contain secrets or tokens
    for field_name, val in [
        ("slug", slug),
        ("name", name),
        ("description", description),
        ("triggers", triggers),
        ("body", body),
    ]:
        if redact_text(val) != val:
            raise ValueError("skills cannot contain secrets or tokens")

    # 2. name: 1-80 chars single line
    n = name.strip()
    if not n or len(n) > MAX_NAME_LEN or "\n" in name or "\r" in name:
        raise ValueError(f"name must be 1-{MAX_NAME_LEN} characters on a single line")

    # 3. description: 1-300 chars single line
    d = description.strip()
    if not d or len(d) > MAX_DESC_LEN or "\n" in description or "\r" in description:
        raise ValueError(f"description must be 1-{MAX_DESC_LEN} characters on a single line")

    # 4. triggers: optional, comma-separated, <=200 chars
    t = triggers.strip()
    if "\n" in triggers or "\r" in triggers or len(t) > MAX_TRIGGERS_LEN:
        raise ValueError(f"triggers cannot exceed {MAX_TRIGGERS_LEN} characters on a single line")

    # 5. body: 1-8000 chars
    b = body.strip()
    if not b or len(body) > MAX_BODY_LEN:
        raise ValueError(f"body must be 1-{MAX_BODY_LEN} characters")

    # 6. slug: ^[a-z0-9][a-z0-9-]{0,47}$ (auto-derived if omitted)
    if slug:
        s = slug.strip()
        if not SLUG_RE.match(s):
            raise ValueError("slug must match ^[a-z0-9][a-z0-9-]{0,47}$")
    else:
        s = derive_slug(n)
        if not s or not SLUG_RE.match(s):
            raise ValueError("slug must match ^[a-z0-9][a-z0-9-]{0,47}$ (could not derive valid slug from name)")

    return {
        "slug": s,
        "name": n,
        "description": d,
        "triggers": t,
        "body": body,
    }


def _audit(settings: Any, action: str, slug: str, *, body_size: int = 0, name_len: int = 0) -> None:
    try:
        from handlers.tools.audit import audit_tool_event
        audit_tool_event(
            settings,
            operator_id="web-console",
            chat_id="skills",
            channel="skills",
            tool_name="skill.manage",
            arg=f"slug={slug} body_size={body_size} name_len={name_len}",
            danger="WRITE",
            action=action,
        )
    except Exception:
        logger.exception("Failed to write skill audit event")


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "slug": str(row["slug"]),
        "name": str(row["name"]),
        "description": str(row["description"]),
        "triggers": str(row["triggers"] or ""),
        "body": str(row["body"]),
        "enabled": bool(row["enabled"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "use_count": int(row["use_count"] or 0),
        "last_used_at": str(row["last_used_at"]) if row["last_used_at"] else None,
    }


def list_skills(settings: Any, include_disabled: bool = True) -> list[dict[str, Any]]:
    """List skills from the store."""
    with _db(settings) as conn:
        if include_disabled:
            rows = conn.execute("SELECT * FROM skills ORDER BY slug ASC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM skills WHERE enabled = 1 ORDER BY slug ASC").fetchall()
        return [_row_dict(r) for r in rows]


def get_skill(settings: Any, slug: str) -> dict[str, Any] | None:
    """Get a skill by its slug."""
    s = (slug or "").strip().lower()
    if not s:
        return None
    with _db(settings) as conn:
        row = conn.execute("SELECT * FROM skills WHERE slug = ?", (s,)).fetchone()
        return _row_dict(row) if row is not None else None


def create_skill(
    settings: Any,
    *,
    name: str,
    description: str,
    body: str,
    slug: str = "",
    triggers: str = "",
    enabled: bool = True,
) -> dict[str, Any]:
    """Create a new skill."""
    validated = validate_skill_fields(
        name=name, description=description, body=body, slug=slug, triggers=triggers,
    )
    s = validated["slug"]
    auto_slug = not (slug or "").strip()
    now_iso = datetime.now(timezone.utc).isoformat()

    with _db(settings) as conn:
        count = conn.execute("SELECT count(*) FROM skills").fetchone()[0]
        if count >= MAX_SKILLS:
            raise ValueError(f"maximum of {MAX_SKILLS} skills reached")

        existing = conn.execute("SELECT 1 FROM skills WHERE slug = ?", (s,)).fetchone()
        if existing is not None and auto_slug:
            # Auto-derived slug collides: pick the first free "-2", "-3", ... suffix.
            base = s[:44].rstrip("-")
            for n in range(2, MAX_SKILLS + 2):
                candidate = f"{base}-{n}"
                if conn.execute("SELECT 1 FROM skills WHERE slug = ?", (candidate,)).fetchone() is None:
                    s = candidate
                    existing = None
                    break
        if existing is not None:
            raise SkillConflictError(f"Skill with slug '{s}' already exists")

        cur = conn.execute(
            """
            INSERT INTO skills (slug, name, description, triggers, body, enabled, created_at, updated_at, use_count, last_used_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
            """,
            (
                s,
                validated["name"],
                validated["description"],
                validated["triggers"],
                validated["body"],
                1 if enabled else 0,
                now_iso,
                now_iso,
            ),
        )
        new_id = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM skills WHERE id = ?", (new_id,)).fetchone()

    _audit(settings, "create", s, body_size=len(validated["body"]), name_len=len(validated["name"]))
    return _row_dict(row)


def update_skill(settings: Any, slug: str, /, **fields: Any) -> dict[str, Any]:
    """Update an existing skill. Slug is immutable."""
    s = (slug or "").strip().lower()
    if "slug" in fields and fields["slug"] != s:
        raise ValueError("slug is immutable")

    with _db(settings) as conn:
        row = conn.execute("SELECT * FROM skills WHERE slug = ?", (s,)).fetchone()
        if row is None:
            raise SkillNotFoundError(f"Skill '{s}' not found")
        existing = _row_dict(row)

        name = fields.get("name", existing["name"])
        description = fields.get("description", existing["description"])
        triggers = fields.get("triggers", existing["triggers"])
        body = fields.get("body", existing["body"])
        enabled_val = fields.get("enabled", existing["enabled"])
        enabled = bool(enabled_val)

        validated = validate_skill_fields(
            name=name, description=description, body=body, slug=s, triggers=triggers,
        )

        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE skills
            SET name = ?, description = ?, triggers = ?, body = ?, enabled = ?, updated_at = ?
            WHERE slug = ?
            """,
            (
                validated["name"],
                validated["description"],
                validated["triggers"],
                validated["body"],
                1 if enabled else 0,
                now_iso,
                s,
            ),
        )
        conn.commit()
        updated_row = conn.execute("SELECT * FROM skills WHERE slug = ?", (s,)).fetchone()

    action = "update"
    if "enabled" in fields and len(fields) == 1:
        action = "enable" if enabled else "disable"
    _audit(settings, action, s, body_size=len(validated["body"]), name_len=len(validated["name"]))
    return _row_dict(updated_row)


def delete_skill(settings: Any, slug: str) -> bool:
    """Delete a skill by slug."""
    s = (slug or "").strip().lower()
    with _db(settings) as conn:
        row = conn.execute("SELECT * FROM skills WHERE slug = ?", (s,)).fetchone()
        if row is None:
            raise SkillNotFoundError(f"Skill '{s}' not found")
        conn.execute("DELETE FROM skills WHERE slug = ?", (s,))
        conn.commit()

    _audit(settings, "delete", s)
    return True


def set_enabled(settings: Any, slug: str, enabled: bool) -> dict[str, Any]:
    """Enable or disable a skill."""
    return update_skill(settings, slug, enabled=enabled)


def mark_used(settings_or_slug: Any, slug: str = "") -> dict[str, Any] | None:
    """Increment use_count and set last_used_at for a skill."""
    if isinstance(settings_or_slug, str) and not slug:
        from config import load_runtime_settings
        settings = load_runtime_settings()
        s = settings_or_slug.strip().lower()
    else:
        settings = settings_or_slug
        s = (slug or "").strip().lower()

    if not s:
        return None

    now_iso = datetime.now(timezone.utc).isoformat()
    with _db(settings) as conn:
        conn.execute(
            """
            UPDATE skills
            SET use_count = use_count + 1, last_used_at = ?
            WHERE slug = ?
            """,
            (now_iso, s),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM skills WHERE slug = ?", (s,)).fetchone()
        return _row_dict(row) if row is not None else None


def export_markdown(skill: dict[str, Any]) -> str:
    """Export skill to front-matter markdown format."""
    name = skill.get("name", "")
    description = skill.get("description", "")
    triggers = skill.get("triggers", "")
    body = skill.get("body", "")
    lines = [
        "---",
        f"slug: {skill.get('slug', '')}",
        f"name: {name}",
        f"description: {description}",
        f"triggers: {triggers}",
        "---",
        body,
    ]
    return "\n".join(lines)


def parse_markdown(text: str) -> dict[str, Any]:
    """Parse front-matter markdown into skill fields dict."""
    m = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?(.*)$", text, re.DOTALL)
    if not m:
        raise ValueError("Invalid markdown format: missing front-matter (expected '---' header)")

    fm_text, body = m.group(1), m.group(2)
    meta: dict[str, str] = {}
    for line in fm_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip().lower()
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            meta[k] = v

    name = meta.get("name", "").strip()
    description = meta.get("description", "").strip()
    triggers = meta.get("triggers", "").strip()
    slug = meta.get("slug", "").strip()

    if not name:
        raise ValueError("Markdown front-matter must include 'name'")
    if not description:
        raise ValueError("Markdown front-matter must include 'description'")
    if not body.strip():
        raise ValueError("Markdown body cannot be empty")

    res = {
        "name": name,
        "description": description,
        "triggers": triggers,
        "body": body,
    }
    if slug:
        res["slug"] = slug
    return res


def wrap_skill_body(slug: str, body: str) -> str:
    """Wrap skill body with XML tags and one-line safety header, escaping any literal </skill>."""
    neutralized = re.sub(r"</skill\s*>", "&lt;/skill&gt;", body, flags=re.IGNORECASE)
    return (
        f'<skill slug="{slug}">\n'
        f'{SKILL_HEADER}\n\n'
        f'{neutralized}\n'
        f'</skill>'
    )


def prompt_block(settings: Any) -> str:
    """Bounded index of enabled skills injected into the chat system prompt.

    Bounded to <= 30 enabled skills and <= 2000 chars total.
    Returns empty string if disabled or no enabled skills exist.
    """
    if not getattr(settings, "skills_enabled", False):
        return ""
    try:
        skills = list_skills(settings, include_disabled=False)
    except Exception as exc:
        logger.warning("skills unreadable: %s", exc)
        return ""
    if not skills:
        return ""

    header = "Available skills (load with skill.load before following one):"
    lines = [header]
    total_len = len(header)
    for s in skills[:PROMPT_BLOCK_MAX_SKILLS]:
        trig = f" [triggers: {s['triggers']}]" if s.get("triggers") else ""
        line = f"- {s['slug']}: {s['description']}{trig}"
        if total_len + 1 + len(line) > PROMPT_BLOCK_CHAR_BUDGET:
            break
        lines.append(line)
        total_len += 1 + len(line)

    if len(lines) == 1:
        return ""
    return "\n".join(lines)


# ---- Personal tool execution adapters ----------------------------------------


async def skill_list(
    settings: Any,
    arg: str = "",
    *,
    operator_id: str = "",
    channel: str = "",
    chat_id: str = "",
) -> ToolResult:
    if not getattr(settings, "skills_enabled", False):
        return ToolResult(False, "Skills are disabled. Set CONVEYOR_SKILLS_ENABLED=true.")
    skills = list_skills(settings, include_disabled=False)
    if not skills:
        return ToolResult(True, "No enabled skills.")
    lines = ["Available skills:"]
    for s in skills:
        trig = f" [triggers: {s['triggers']}]" if s.get("triggers") else ""
        lines.append(f"- {s['slug']}: {s['description']}{trig}")
    return ToolResult(True, "\n".join(lines))


async def skill_load(
    settings: Any,
    arg: str = "",
    *,
    operator_id: str = "",
    channel: str = "",
    chat_id: str = "",
) -> ToolResult:
    if not getattr(settings, "skills_enabled", False):
        return ToolResult(False, "Skills are disabled. Set CONVEYOR_SKILLS_ENABLED=true.")
    slug = (arg or "").strip().lower()
    if not slug:
        return ToolResult(False, "Usage: skill.load <slug>")
    skill = get_skill(settings, slug)
    if not skill or not skill["enabled"]:
        enabled_skills = list_skills(settings, include_disabled=False)
        if enabled_skills:
            slugs_str = ", ".join(s["slug"] for s in enabled_skills)
            return ToolResult(False, f"Skill '{slug}' not found or disabled. Available skills: {slugs_str}")
        return ToolResult(False, f"Skill '{slug}' not found. There are currently no enabled skills.")

    mark_used(settings, slug)
    return ToolResult(True, wrap_skill_body(skill["slug"], skill["body"]))


async def skill_create(
    settings: Any,
    arg: str = "",
    *,
    operator_id: str = "",
    channel: str = "",
    chat_id: str = "",
) -> ToolResult:
    if not getattr(settings, "skills_enabled", False):
        return ToolResult(False, "Skills are disabled. Set CONVEYOR_SKILLS_ENABLED=true.")
    parts = (arg or "").split("|", 2)
    if len(parts) < 3:
        return ToolResult(False, "Usage: skill.create <name> | <description> | <body>")
    name = parts[0].strip()
    description = parts[1].strip()
    body = parts[2].strip()
    try:
        skill = create_skill(settings, name=name, description=description, body=body)
        return ToolResult(True, f"Created skill '{skill['name']}' (slug: {skill['slug']})")
    except ValueError as exc:
        return ToolResult(False, str(exc))
