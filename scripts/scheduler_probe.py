#!/usr/bin/env python3
"""scheduler_probe.py — one-command probe for reminder delivery.

Default mode is safe: it uses a temporary codex_memory_root and runs the
scheduler in --dry-run mode, so it does not touch production reminders and
does not send Telegram messages.

Live mode creates one due reminder in the production personal_tools.db,
runs one real scheduler tick, and expects a Telegram message to be sent to
TELEGRAM_ALLOWED_USER_ID. Use live mode only on the VPS with a real .env.

Examples:
  .venv/bin/python scripts/scheduler_probe.py
  .venv/bin/python scripts/scheduler_probe.py --live
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings, load_settings  # noqa: E402
from personal_tools.store import PersonalToolsStore, ReminderRow, db_path  # noqa: E402
from redaction import redact_text  # noqa: E402
from scripts import scheduler_tick  # noqa: E402


def _make_due_reminder(settings: Settings, text: str) -> ReminderRow:
    operator_id = str(settings.telegram_allowed_user_id)
    chat_id = str(settings.telegram_allowed_user_id)
    due_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    store = PersonalToolsStore(settings)
    return store.create_reminder(
        operator_id,
        text,
        due_at,
        channel="telegram",
        chat_id=chat_id,
    )


def _find_reminder(settings: Settings, reminder_id: int) -> ReminderRow | None:
    store = PersonalToolsStore(settings)
    operator_id = str(settings.telegram_allowed_user_id)
    for row in store.list_reminders(operator_id, limit=100):
        if row.id == reminder_id:
            return row
    return None


def _patch_scheduler_settings(settings: Settings):
    original = scheduler_tick.load_settings
    scheduler_tick.load_settings = lambda: settings  # type: ignore[assignment]
    return original


def run_safe_probe(base_settings: Settings, text: str) -> int:
    """Run a no-network probe with a temporary DB."""
    with tempfile.TemporaryDirectory(prefix="conveyor-scheduler-probe-") as td:
        settings = replace(base_settings, codex_memory_root=Path(td))
        row = _make_due_reminder(settings, text)
        original = _patch_scheduler_settings(settings)
        try:
            delivered, failed = scheduler_tick.run_tick(dry_run=True)
        finally:
            scheduler_tick.load_settings = original  # type: ignore[assignment]

        after = _find_reminder(settings, row.id)
        ok = (
            delivered == 1
            and failed == 0
            and after is not None
            and after.status == "pending"
            and after.delivery_status == "pending"
        )
        print("Scheduler safe probe")
        print(f"  db: {db_path(settings)}")
        print(f"  reminder: #{row.id} {redact_text(row.text)}")
        print(f"  run_tick(dry_run=True): delivered={delivered} failed={failed}")
        print(
            "  DB after dry-run: "
            f"status={getattr(after, 'status', None)} "
            f"delivery_status={getattr(after, 'delivery_status', None)}"
        )
        print("  result: OK" if ok else "  result: FAILED")
        return 0 if ok else 1


def run_live_probe(settings: Settings, text: str) -> int:
    """Run a real one-shot Telegram delivery probe against production DB."""
    row = _make_due_reminder(settings, text)
    delivered, failed = scheduler_tick.run_tick(dry_run=False)
    after = _find_reminder(settings, row.id)
    ok = (
        delivered >= 1
        and failed == 0
        and after is not None
        and after.status == "done"
        and after.delivery_status == "delivered"
    )
    print("Scheduler LIVE probe")
    print(f"  db: {db_path(settings)}")
    print(f"  reminder: #{row.id} {redact_text(row.text)}")
    print(f"  run_tick(dry_run=False): delivered={delivered} failed={failed}")
    print(
        "  DB after live tick: "
        f"status={getattr(after, 'status', None)} "
        f"delivery_status={getattr(after, 'delivery_status', None)} "
        f"retry_count={getattr(after, 'retry_count', None)} "
        f"error={redact_text(str(getattr(after, 'delivery_error', '') or ''))}"
    )
    print("  result: OK — check Telegram for the reminder message." if ok else "  result: FAILED")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Conveyor reminder scheduler delivery")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Send a real Telegram reminder using production .env and DB",
    )
    parser.add_argument(
        "--text",
        default="Conveyor scheduler probe",
        help="Reminder text to create for the probe",
    )
    args = parser.parse_args()

    settings = load_settings()
    if args.live:
        return run_live_probe(settings, args.text)
    return run_safe_probe(settings, args.text)


if __name__ == "__main__":
    raise SystemExit(main())
