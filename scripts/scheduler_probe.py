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
import logging
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings, load_settings  # noqa: E402
from personal_tools.store import PersonalToolsStore, ReminderRow, _connect, db_path  # noqa: E402
from redaction import redact_text, truncate  # noqa: E402
from scripts import scheduler_tick  # noqa: E402

logger = logging.getLogger("scheduler_probe")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _run_cmd(args: list[str], timeout: float = 5.0) -> str:
    """Run a command, return stdout stripped. Empty on failure."""
    try:
        result = subprocess.run(
            args, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False,
        )
        return (result.stdout or "").strip()
    except Exception:
        return ""


def _systemd_status(unit: str) -> dict[str, str]:
    """Return {active, enabled, last_run_status, journal_tail} for a unit."""
    info: dict[str, str] = {}
    if not shutil.which("systemctl"):
        info["error"] = "systemctl 不可用"
        return info
    info["active"] = _run_cmd(["systemctl", "is-active", unit]) or "unknown"
    info["enabled"] = _run_cmd(["systemctl", "is-enabled", unit]) or "unknown"
    info["last_run_status"] = _run_cmd(
        ["systemctl", "show", unit, "--property=Result", "--value"]
    ) or "unknown"
    journal = _run_cmd(
        ["journalctl", "-u", unit, "-n", "8", "--no-pager", "-o", "short-iso"],
        timeout=8.0,
    )
    info["journal_tail"] = truncate(journal, 1500) if journal else "(无日志)"
    return info


def _reminder_counts(settings: Settings) -> dict[str, int]:
    """Count reminders by status from personal_tools.db."""
    counts: dict[str, int] = {"pending": 0, "delivered": 0, "failed": 0, "cancelled": 0, "done": 0}
    try:
        with _connect(settings) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM reminders GROUP BY status"
            ).fetchall()
            for row in rows:
                status = str(row["status"]) if "status" in row.keys() else str(row[0])
                cnt = int(row["cnt"]) if "cnt" in row.keys() else int(row[1])
                if status in counts:
                    counts[status] = cnt
    except Exception:
        pass
    delivery_counts: dict[str, int] = {"pending": 0, "delivered": 0, "failed": 0}
    try:
        with _connect(settings) as conn:
            rows = conn.execute(
                "SELECT delivery_status, COUNT(*) as cnt FROM reminders GROUP BY delivery_status"
            ).fetchall()
            for row in rows:
                ds = str(row["delivery_status"]) if "delivery_status" in row.keys() else str(row[0])
                cnt = int(row["cnt"]) if "cnt" in row.keys() else int(row[1])
                if ds in delivery_counts:
                    delivery_counts[ds] = cnt
    except Exception:
        pass
    counts["delivery_pending"] = delivery_counts["pending"]
    counts["delivery_delivered"] = delivery_counts["delivered"]
    counts["delivery_failed"] = delivery_counts["failed"]
    return counts


# ---------------------------------------------------------------------------
# Tool-friendly functions (called by handlers/tools/executors.py)
# ---------------------------------------------------------------------------

def scheduler_status_report() -> str:
    """Full read-only scheduler status report. Safe for READ tool."""
    settings = load_settings()
    lines = ["📊 Scheduler 状态报告", ""]

    # 1. Timer status
    timer_info = _systemd_status("conveyor-scheduler.timer")
    lines.append("── Timer ──")
    if "error" in timer_info:
        lines.append(f"  ⚠️ {timer_info['error']}")
    else:
        lines.append(f"  active: {timer_info.get('active', '?')}")
        lines.append(f"  enabled: {timer_info.get('enabled', '?')}")
    lines.append("")

    # 2. Service last run
    svc_info = _systemd_status("conveyor-scheduler.service")
    lines.append("── Service (last run) ──")
    if "error" in svc_info:
        lines.append(f"  ⚠️ {svc_info['error']}")
    else:
        lines.append(f"  last result: {svc_info.get('last_run_status', '?')}")
        lines.append("  journal tail:")
        for jline in svc_info.get("journal_tail", "").splitlines()[:6]:
            lines.append(f"    {redact_text(jline)}")
    lines.append("")

    # 3. Reminder counts
    counts = _reminder_counts(settings)
    lines.append("── 提醒统计 ──")
    lines.append(f"  pending: {counts['pending']}")
    lines.append(f"  delivered (done): {counts['done']}")
    lines.append(f"  failed: {counts['failed']}")
    lines.append(f"  cancelled: {counts['cancelled']}")
    lines.append(f"  delivery_pending: {counts['delivery_pending']}")
    lines.append(f"  delivery_delivered: {counts['delivery_delivered']}")
    lines.append(f"  delivery_failed: {counts['delivery_failed']}")
    lines.append("")

    # 4. Channel support
    lines.append("── 投递通道 ──")
    lines.append("  Telegram: ✅ 已实现")
    lines.append("  Feishu: ⚠️ 未实现 (skip with log)")
    lines.append("")

    # 5. Probe guidance
    lines.append("── 探针 ──")
    lines.append("  /scheduler_probe — dry-run 探测 (不发消息)")
    lines.append("  /scheduler_probe_live — 实际投递测试 (需确认)")
    lines.append("")
    lines.append(f"  时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    return truncate("\n".join(lines))


def scheduler_probe_dry_run() -> str:
    """Synthetic dry-run probe: create temp DB with due reminder, verify scan path.

    This is a true synthetic probe — it creates a temporary codex_memory_root
    with one due telegram reminder, patches scheduler_tick to use it, runs
    run_tick(dry_run=True), and asserts the reminder is found but NOT written
    (dry-run must not mutate DB).
    """
    base_settings = load_settings()
    with tempfile.TemporaryDirectory(prefix="conveyor-scheduler-probe-") as td:
        settings = replace(base_settings, codex_memory_root=Path(td))
        store = PersonalToolsStore(settings)
        now = datetime.now(timezone.utc)
        operator_id = str(settings.telegram_allowed_user_id)
        test_text = f"[synthetic-probe] dry-run @ {now.strftime('%H:%M:%S UTC')}"
        row = store.create_reminder(
            operator_id=operator_id,
            text=test_text,
            due_at=now - timedelta(seconds=5),
            channel="telegram",
            chat_id=operator_id,
        )

        original = _patch_scheduler_settings(settings)
        try:
            delivered, failed = scheduler_tick.run_tick(dry_run=True)
        finally:
            scheduler_tick.load_settings = original  # type: ignore[assignment]

        # Verify DB row remains unchanged (dry-run must not write)
        after = _find_reminder(settings, row.id)
        ok = (
            delivered == 1
            and failed == 0
            and after is not None
            and after.status == "pending"
            and after.delivery_status == "pending"
        )

        lines = ["🔍 Scheduler 探针 (dry-run 合成模式)", ""]
        lines.append(f"  创建临时测试提醒 #{row.id}: {test_text}")
        lines.append(f"  run_tick(dry_run=True): delivered={delivered} failed={failed}")
        lines.append("  dry-run 验证:")
        lines.append(f"    ✓ 扫描到 1 条到期提醒: {delivered == 1}")
        lines.append(f"    ✓ 投递计数为 0 (dry-run): {failed == 0}")
        lines.append(f"    ✓ DB 行保持 pending: {after.status == 'pending' if after else False}")
        lines.append(f"    ✓ delivery_status 未变: {after.delivery_status == 'pending' if after else False}")
        lines.append("")
        if ok:
            lines.append("  ✅ 合成 dry-run 探针通过！扫描路径验证成功，未发送消息，未写入 DB。")
        else:
            lines.append("  ❌ 合成 dry-run 探针失败！")
        lines.append("")
        lines.append(f"  时间: {now.strftime('%Y-%m-%d %H:%M UTC')}")
        return truncate("\n".join(lines))


def scheduler_probe_live() -> str:
    """Live probe: create a test reminder and deliver via Telegram. Requires WRITE confirmation."""
    settings = load_settings()
    store = PersonalToolsStore(settings)

    now = datetime.now(timezone.utc)
    test_text = f"[probe] 投递探针测试 @ {now.strftime('%H:%M:%S UTC')}"
    row = store.create_reminder(
        operator_id="probe",
        text=test_text,
        due_at=now,
        channel="telegram",
        chat_id=str(settings.telegram_allowed_user_id),
    )

    lines = ["🚀 Scheduler 实时探针", ""]
    lines.append(f"  创建测试提醒 #{row.id}: {test_text}")
    lines.append(f"  目标: telegram:{settings.telegram_allowed_user_id}")
    lines.append("")

    delivered, failed = scheduler_tick.run_tick(dry_run=False)

    lines.append(f"  投递结果: {delivered} 成功, {failed} 失败")
    lines.append("")

    with _connect(settings) as conn:
        r = conn.execute(
            "SELECT delivery_status, delivered_at FROM reminders WHERE id = ?",
            (row.id,),
        ).fetchone()
    if r:
        ds = str(r["delivery_status"]) if "delivery_status" in r.keys() else str(r[0])
        da = str(r["delivered_at"]) if "delivered_at" in r.keys() else str(r[1])
        lines.append(f"  DB 状态: delivery_status={ds}, delivered_at={da}")
        if ds == "delivered":
            lines.append("  ✅ 实时探针通过！Telegram 投递成功。")
        else:
            lines.append(f"  ⚠️ 投递状态异常: {ds}")
    else:
        lines.append("  ❌ 未找到测试提醒记录")

    lines.append("")
    lines.append(f"  时间: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    return truncate("\n".join(lines))


# ---------------------------------------------------------------------------
# Legacy probe functions (used by CLI --safe / --live)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

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
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    settings = load_settings()
    if args.live:
        return run_live_probe(settings, args.text)
    return run_safe_probe(settings, args.text)


if __name__ == "__main__":
    raise SystemExit(main())
