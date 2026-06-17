#!/usr/bin/env python3
"""scheduler_tick.py — deliver due reminders.

Run by conveyor-scheduler.timer every 60 seconds.
Supports --dry-run for smoke testing (no network, no DB writes).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from personal_tools.store import PersonalToolsStore
from redaction import redact_text

logger = logging.getLogger("scheduler_tick")


def _send_telegram(settings, chat_id: str, text: str) -> None:
    from scripts.telegram_api import send_message
    send_message(settings, text, chat_id=int(chat_id))


def _deliver_one(settings, reminder, *, dry_run: bool) -> tuple[bool, str]:
    """Attempt to deliver a single reminder. Returns (ok, error_or_ok)."""
    channel = reminder.channel
    chat_id = reminder.chat_id
    text = f"⏰ 提醒 #{reminder.id}\n{reminder.text}"

    if dry_run:
        logger.info("[dry-run] would deliver #%d to %s:%s: %s",
                     reminder.id, channel, chat_id, redact_text(reminder.text))
        return True, "dry-run"

    if channel == "telegram":
        try:
            _send_telegram(settings, chat_id, text)
            return True, "ok"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
    elif channel == "feishu":
        logger.warning("Feishu delivery not yet implemented for reminder #%d", reminder.id)
        return False, "feishu_not_implemented"
    else:
        return False, f"unknown_channel:{channel}"


def run_tick(*, dry_run: bool = False) -> tuple[int, int]:
    """Run one scheduler tick. Returns (delivered, failed).

    Also checks and sends daily briefings if enabled and due.
    """
    settings = load_settings()
    store = PersonalToolsStore(settings)
    due = store.list_due_deliverable_reminders()

    # P3.5 Daily Briefing: check and send if enabled and due
    briefing_sent = 0
    if not dry_run:
        try:
            from personal_tools.briefing import briefing_check_and_send
            briefing_sent = briefing_check_and_send(settings)
            if briefing_sent > 0:
                logger.info("Sent %d daily briefing(s)", briefing_sent)
        except Exception as exc:
            logger.error("Briefing check failed: %s", exc)

    if not due:
        logger.debug("No due deliverable reminders.")
        return 0, 0

    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    delivered = 0
    failed = 0

    for r in due:
        ok, err = _deliver_one(settings, r, dry_run=dry_run)
        if ok:
            if not dry_run:
                store.mark_reminder_done(r.id, now_iso)
            delivered += 1
            logger.info("reminder #%d delivered (%s)", r.id, r.channel)
        else:
            if not dry_run:
                store.mark_reminder_failed(r.id, err, r.retry_count + 1)
            failed += 1
            logger.warning("reminder #%d failed: %s", r.id, redact_text(err))

    return delivered, failed


def main() -> int:
    parser = argparse.ArgumentParser(description="Conveyor reminder scheduler tick")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without sending or writing DB")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    delivered, failed = run_tick(dry_run=args.dry_run)
    logger.info("tick complete: %d delivered, %d failed", delivered, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
