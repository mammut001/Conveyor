"""Read-only human alerts for G3 submissions with uncertain remote outcomes."""
from __future__ import annotations
import base64

from workflow_browser_g3_store import list_reconciliation


def inbox_items(settings):
    items = []
    for entry in list_reconciliation(settings):
        preview = entry["preview"]
        items.append({
            "id": "g3-reconcile-" + entry["token"],
            "kind": "reconciliation",
            "source": "browser",
            "tool_name": "browser.g3_dispatch",
            "summary": "G3 needs_reconciliation — remote submission may have succeeded; check the site manually. NEVER retry this request.",
            "danger": "write",
            "editable": False,
            "arg": "Read-only incident. No approve/retry action is available.",
            "draft": None,
            "created_at": entry["created_at"],
            "g3_preview": preview,
            "g3_hash": entry["hash"],
            "g3_screenshot": "data:image/png;base64," + base64.b64encode(entry["screenshot"]).decode("ascii"),
        })
    return items
