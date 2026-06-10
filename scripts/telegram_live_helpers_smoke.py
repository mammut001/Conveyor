#!/usr/bin/env python3
"""telegram_live_helpers_smoke.py — pure-helper tests for the live smoke.

No Telethon, no real Telegram. Covers redact() and
validate_restart_target() so the live script's safety helpers get
some coverage in the env-free smoke suite.

Run: .venv/bin/python scripts/telegram_live_helpers_smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.harness_common import CheckResult, print_results
from scripts.telegram_live_smoke import redact, validate_restart_target


def _test_redact_bot_token() -> CheckResult:
    name = "redact: masks bot tokens (digits:alnum)"
    text = "token=123456789:AAH-abcdefghijklmnopqrstuvwxyz12345"
    out = redact(text)
    ok = "[REDACTED_TOKEN]" in out and "123456789:AAH" not in out
    return CheckResult(name, ok, f"len={len(out)} sample={out[:80]!r}")


def _test_redact_api_hash() -> CheckResult:
    name = "redact: masks 32-char hex api hashes"
    text = "hash=deadbeefdeadbeefdeadbeefdeadbeef suffix"
    out = redact(text)
    ok = "[REDACTED_HASH]" in out and "deadbeefdeadbeefdeadbeefdeadbeef" not in out
    return CheckResult(name, ok, out[:80])


def _test_redact_session_path() -> CheckResult:
    name = "redact: masks .telegram-live-smoke session paths"
    text = "session file at .telegram-live-smoke.session created"
    out = redact(text)
    ok = "[REDACTED_SESSION]" in out and ".telegram-live-smoke" not in out
    return CheckResult(name, ok, out)


def _test_redact_long_token() -> CheckResult:
    name = "redact: masks long alphanumeric tokens"
    text = "bearer ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnop12345"
    out = redact(text)
    ok = "[REDACTED_LONG_TOKEN]" in out
    return CheckResult(name, ok, out[:80])


def _test_redact_env_value() -> CheckResult:
    name = "redact: masks known env values if they leak into text"
    import os
    secret = "verylongsecrettokenvalue1234567890"
    saved = os.environ.get("TELEGRAM_API_HASH")
    os.environ["TELEGRAM_API_HASH"] = secret
    try:
        out = redact(f"leaked={secret}")
    finally:
        if saved is None:
            os.environ.pop("TELEGRAM_API_HASH", None)
        else:
            os.environ["TELEGRAM_API_HASH"] = saved
    ok = secret not in out and "***" in out
    return CheckResult(name, ok, out)


def _test_redact_empty_and_none() -> CheckResult:
    name = "redact: empty/None input is safe"
    ok = redact("") == "" and redact(None) == ""
    return CheckResult(name, ok, "")


def _test_validate_restart_target_ok() -> CheckResult:
    name = "validate_restart_target: accepts whitelist"
    out = [validate_restart_target(t) for t in ("telegram", "feishu", "maintain")]
    ok = out == ["telegram", "feishu", "maintain"]
    return CheckResult(name, ok, str(out))


def _test_validate_restart_target_normalizes() -> CheckResult:
    name = "validate_restart_target: trims and lowercases"
    out = validate_restart_target("  TELEGRAM  ")
    return CheckResult(name, out == "telegram", f"got {out!r}")


def _test_validate_restart_target_rejects_unknown() -> CheckResult:
    name = "validate_restart_target: rejects unknown"
    for bad in ("postgres", "nginx", "sshd", "conveyor-telegram-bot"):
        try:
            validate_restart_target(bad)
            return CheckResult(name, False, f"accepted {bad!r}")
        except ValueError:
            continue
    return CheckResult(name, True, "all unknown targets refused")


def main() -> int:
    results = [
        _test_redact_bot_token(),
        _test_redact_api_hash(),
        _test_redact_session_path(),
        _test_redact_long_token(),
        _test_redact_env_value(),
        _test_redact_empty_and_none(),
        _test_validate_restart_target_ok(),
        _test_validate_restart_target_normalizes(),
        _test_validate_restart_target_rejects_unknown(),
    ]
    print_results(results)
    ok = all(r.ok for r in results)
    print("telegram live helpers smoke ok" if ok else "telegram live helpers smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())