"""contacts_google.py — Google Contacts tools for Conveyor personal tools.

Uses Google People API via OAuth credentials.
contacts.search is READ.

Dependencies: google-auth, google-api-python-client.
"""
from __future__ import annotations

import logging
from typing import Any

from config import Settings
from personal_tools.base import ToolResult
from personal_tools.google_oauth import build_google_service
from redaction import redact_text, truncate

logger = logging.getLogger(__name__)


def _format_contact(person: dict) -> str:
    """Format a Google People API person for display."""
    names = person.get("names", [])
    name = names[0].get("displayName", "?") if names else "(无名)"

    phones = person.get("phoneNumbers", [])
    phone_str = ""
    if phones:
        phone_str = f" 📱{phones[0].get('value', '')}"

    emails = person.get("emailAddresses", [])
    email_str = ""
    if emails:
        email_str = f" ✉️{emails[0].get('value', '')}"

    orgs = person.get("organizations", [])
    org_str = ""
    if orgs:
        org_name = orgs[0].get("name", "")
        if org_name:
            org_str = f" 🏢{org_name}"

    return f"  • {name}{phone_str}{email_str}{org_str}"


def contacts_search(settings: Settings, query: str) -> ToolResult:
    """Search Google Contacts by name, email, or phone."""
    if not query.strip():
        return ToolResult(ok=False, text="⚠️ 搜索关键词不能为空")

    service, err = build_google_service(settings, "people", "v1")
    if err:
        return ToolResult(ok=False, text=f"⚠️ {err}")

    try:
        result = service.people().searchContacts(
            query=query.strip(),
            readMask="names,phoneNumbers,emailAddresses,organizations",
            pageSize=10,
        ).execute()

        results = result.get("results", [])
        if not results:
            return ToolResult(ok=True, text=f"🔍 未找到匹配 '{query}' 的联系人")

        lines = [f"🔍 搜索 '{query}' 找到 {len(results)} 个联系人:", ""]
        for r in results:
            person = r.get("person", {})
            lines.append(_format_contact(person))

        return ToolResult(ok=True, text=truncate("\n".join(lines)))
    except Exception as exc:
        return ToolResult(ok=False, text=f"⚠️ 搜索失败: {redact_text(str(exc))}")


async def contacts_search_adapter(settings: Settings, arg: str, **_kw) -> ToolResult:
    return contacts_search(settings, arg.strip())
