#!/usr/bin/env python3
"""scripts/sentry_tick.py — Always-On Teammate & Proactive Sentry runner.

Can be run via cron / timer or invoked manually by the operator to perform
immediate host, service, log, git, and CI health patrols.

Usage:
    python3 scripts/sentry_tick.py [--check] [--dry-run] [--force] [--json]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from logging_setup import configure_logging
from personal_tools.sentry import (
    deliver_sentry_alerts,
    run_sentry_patrol,
    teammate_status_text,
)

logger = logging.getLogger("sentry_tick")


def main() -> int:
    parser = argparse.ArgumentParser(description="Conveyor Always-On Teammate sentry runner")
    parser.add_argument("--check", action="store_true", help="Run immediate patrol and print health report")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate checkers without delivering alerts or mutating state")
    parser.add_argument("--force", action="store_true", help="Force execution ignoring pause state and cooldown")
    parser.add_argument("--status", action="store_true", help="Display sentry status card and exit")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug logging")
    args = parser.parse_args()

    configure_logging(
        service_name="sentry_tick",
        level=logging.DEBUG if args.verbose else logging.INFO,
        fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    settings = load_settings()

    if args.status:
        text = teammate_status_text(settings)
        print(text)
        return 0

    to_deliver, suppressed = run_sentry_patrol(
        settings,
        force=args.force or args.check,
        dry_run=args.dry_run,
        check_due=not (args.force or args.check),
    )

    if args.json:
        data = {
            "alerts": [asdict(a) for a in to_deliver],
            "suppressed": [asdict(a) for a in suppressed],
            "delivered_count": 0,
        }
        if not args.dry_run and not args.check and to_deliver:
            sent = deliver_sentry_alerts(settings, to_deliver, dry_run=False)
            data["delivered_count"] = sent
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    if args.check:
        print("🛡️ Always-On Teammate Live Patrol:")
        if not to_deliver and not suppressed:
            print("✅ All systems healthy. No anomalies detected.")
        else:
            if to_deliver:
                print(f"🚨 Found {len(to_deliver)} actionable alert(s):")
                for a in to_deliver:
                    print(f"  • [{a.severity.upper()}] {a.title}: {a.summary}")
            if suppressed:
                print(f"ℹ️ {len(suppressed)} alert(s) suppressed by cooldown or mute rules.")
        return 0

    # Normal tick mode
    if to_deliver:
        sent = deliver_sentry_alerts(settings, to_deliver, dry_run=args.dry_run)
        logger.info("Delivered %d sentry alert(s)", sent)
    else:
        logger.info("Sentry patrol completed: no new alerts to deliver")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
