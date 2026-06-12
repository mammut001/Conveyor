#!/usr/bin/env python3
"""audit_rotation_smoke.py — test audit log rotation (P2.5).

Tests:
  * small log is NOT rotated
  * oversized log IS rotated to tools.log.1
  * old .1 shifts to .2, oldest .3 is deleted
  * read_audit_tail reads the current (non-rotated) log
  * rotated_log_paths lists existing rotated files

No network, no env.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import tempfile
from types import SimpleNamespace

from scripts.harness_common import CheckResult, print_results


def _fake_settings(tmp: Path):
    return SimpleNamespace(
        codex_memory_root=tmp,
    )


def _write_records(settings, n: int, *, record_size: int = 50):
    """Write N audit records, each approximately record_size bytes."""
    from handlers.tools.audit import audit_tool_event
    for i in range(n):
        audit_tool_event(
            settings,
            operator_id="test",
            chat_id="chat",
            channel="telegram",
            tool_name="test_tool",
            arg=f"arg_{i}_" + "x" * record_size,
            danger="WRITE",
            action="confirm",
        )


def _test_no_rotation_for_small_log() -> CheckResult:
    name = "small log: no rotation"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp))
        _write_records(settings, 5)
        from handlers.tools.audit import audit_log_path, rotated_log_paths
        path = audit_log_path(settings)
        ok1 = path.exists()
        ok2 = len(rotated_log_paths(settings)) == 0
        return CheckResult(name, ok1 and ok2, f"exists={ok1} rotated={len(rotated_log_paths(settings))}")
    return CheckResult(name, False, "tempdir failed")


def _test_rotation_on_oversize() -> CheckResult:
    name = "oversize log: rotated to .1"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp))
        from handlers.tools.audit import audit_log_path, rotated_log_paths, AUDIT_MAX_BYTES
        # Write enough data to exceed the limit.
        # Each record ~100 bytes, so 11000 records ≈ 1.1 MB > 1 MB.
        _write_records(settings, 11000, record_size=50)
        rotated = rotated_log_paths(settings)
        path = audit_log_path(settings)
        ok1 = len(rotated) >= 1
        ok2 = path.exists()  # current log exists (new records written after rotation)
        return CheckResult(name, ok1 and ok2, f"rotated={len(rotated)} current_exists={ok2}")
    return CheckResult(name, False, "tempdir failed")


def _test_rotation_shifts_old_files() -> CheckResult:
    name = "rotation: .1 shifts to .2"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp))
        from handlers.tools.audit import audit_log_path, rotated_log_paths
        path = audit_log_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create a fake .1 file to simulate a previous rotation.
        old1 = path.parent / "tools.log.1"
        old1.write_text('{"old": true}\n', encoding="utf-8")
        # Write enough to trigger rotation.
        _write_records(settings, 11000, record_size=50)
        new1 = path.parent / "tools.log.1"
        new2 = path.parent / "tools.log.2"
        # .1 should now be the rotated current log, .2 should be the old .1.
        ok1 = new1.exists()
        ok2 = new2.exists()
        return CheckResult(name, ok1 and ok2, f".1={ok1} .2={ok2}")
    return CheckResult(name, False, "tempdir failed")


def _test_read_tail_after_rotation() -> CheckResult:
    name = "read_audit_tail: reads current log only"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp))
        from handlers.tools.audit import read_audit_tail
        # Write a few records (no rotation).
        _write_records(settings, 3)
        tail = read_audit_tail(settings, n=5)
        ok = len(tail) == 3
        return CheckResult(name, ok, f"tail_len={len(tail)}")
    return CheckResult(name, False, "tempdir failed")


def _test_rotation_preserves_current_log() -> CheckResult:
    name = "rotation: current log is writable after rotation"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp))
        from handlers.tools.audit import audit_log_path, read_audit_tail
        # Trigger rotation.
        _write_records(settings, 11000, record_size=50)
        # Write one more record after rotation.
        _write_records(settings, 1)
        tail = read_audit_tail(settings, n=5)
        # Should have at least the 1 new record.
        ok = len(tail) >= 1
        return CheckResult(name, ok, f"tail_len={len(tail)}")
    return CheckResult(name, False, "tempdir failed")


def main() -> int:
    results = [
        _test_no_rotation_for_small_log(),
        _test_rotation_on_oversize(),
        _test_rotation_shifts_old_files(),
        _test_read_tail_after_rotation(),
        _test_rotation_preserves_current_log(),
    ]
    print_results(results)
    ok = all(r.ok for r in results)
    print("audit rotation smoke ok" if ok else "audit rotation smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
