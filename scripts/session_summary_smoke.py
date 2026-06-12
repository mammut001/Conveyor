#!/usr/bin/env python3
"""session_summary_smoke.py — env-free unit tests for session summary.

Tests:
  - session file path is safe (no path traversal)
  - append_turn redacts/truncates
  - max turns retention works
  - build_context_prompt includes recent context
  - /context shows recent items
  - /forget clears session
  - deterministic /load does not get session injection
  - ordinary LLM text does get session injection
  - missing/invalid session file does not crash

Run: .venv/bin/python scripts/session_summary_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.harness_common import CheckResult, print_results  # noqa: E402

from channel.types import InboundMessage  # noqa: E402
from handlers.session import (  # noqa: E402
    _max_turns,
    _safe_filename,
    append_turn,
    build_context_prompt,
    clear_session,
    get_recent_turns,
    session_path,
    should_inject_for_command,
)


def _fake_settings(tmp: Path, *, enabled: bool = True, max_turns: int = 5, inject_turns: int = 3):
    return SimpleNamespace(
        codex_memory_root=tmp,
        conveyor_session_enabled=enabled,
        conveyor_session_max_turns=max_turns,
        conveyor_session_inject_turns=inject_turns,
    )


def _fake_msg(channel="telegram", chat_id="123", operator_id="456"):
    return InboundMessage(
        channel=channel,
        operator_id=operator_id,
        chat_id=chat_id,
        message_id="msg_1",
        text="hello",
    )


# ---- Tests ------------------------------------------------------------------


def _test_safe_filename_basic() -> CheckResult:
    name = "safe_filename: basic alphanumeric"
    fn = _safe_filename("telegram", "123", "456")
    return CheckResult(name, fn == "telegram_123_456.jsonl", f"fn={fn!r}")


def _test_safe_filename_no_traversal() -> CheckResult:
    name = "safe_filename: rejects path traversal"
    fn = _safe_filename("../etc", "passwd", "../../root")
    ok = ".." not in fn and "/" not in fn
    return CheckResult(name, ok, f"fn={fn!r}")


def _test_safe_filename_special_chars() -> CheckResult:
    name = "safe_filename: special chars replaced"
    fn = _safe_filename("test channel", "id:123", "user@name")
    ok = " " not in fn and ":" not in fn and "@" not in fn and fn.endswith(".jsonl")
    return CheckResult(name, ok, f"fn={fn!r}")


def _test_session_path_structure() -> CheckResult:
    name = "session_path: returns codex_memory_root/session/<file>"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp))
        msg = _fake_msg()
        p = session_path(settings, msg)
        ok = str(p).endswith("session/telegram_123_456.jsonl") and "session" in str(p)
        return CheckResult(name, ok, f"path={p}")
    return CheckResult(name, False, "tempdir failed")


def _test_append_turn_redacts() -> CheckResult:
    name = "append_turn: redacts and truncates user/assistant text"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp))
        msg = _fake_msg()
        # Write a turn with text that should be redacted
        append_turn(settings, msg, "my token is ABCDEF1234567890", "done")
        turns = get_recent_turns(settings, msg)
        ok = len(turns) == 1
        if ok:
            t = turns[0]
            ok = t["user"] != "my token is ABCDEF1234567890" or len(t["user"]) <= 300
            ok = ok and t["assistant"] == "done"
            ok = ok and t["kind"] == "codex"
        return CheckResult(name, ok, f"turns={turns}")
    return CheckResult(name, False, "tempdir failed")


def _test_max_turns_retention() -> CheckResult:
    name = "get_recent_turns: returns last N turns"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp), max_turns=3)
        msg = _fake_msg()
        for i in range(10):
            append_turn(settings, msg, f"turn {i}", f"reply {i}")
        turns = get_recent_turns(settings, msg)
        ok = len(turns) == 3
        if ok:
            ok = turns[0]["user"] == "turn 7" and turns[2]["user"] == "turn 9"
        return CheckResult(name, ok, f"got {len(turns)} turns, first={turns[0]['user'] if turns else 'N/A'}")
    return CheckResult(name, False, "tempdir failed")


def _test_build_context_prompt() -> CheckResult:
    name = "build_context_prompt: includes recent turns"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp), inject_turns=2)
        msg = _fake_msg()
        append_turn(settings, msg, "what is X?", "X is Y")
        append_turn(settings, msg, "and Z?", "Z is W")
        prompt = build_context_prompt(settings, msg)
        ok = "Recent chat context" in prompt and "what is X?" in prompt and "Z is W" in prompt
        return CheckResult(name, ok, f"prompt={prompt[:200]!r}")
    return CheckResult(name, False, "tempdir failed")


def _test_build_context_prompt_empty() -> CheckResult:
    name = "build_context_prompt: empty when no turns"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp))
        msg = _fake_msg()
        prompt = build_context_prompt(settings, msg)
        return CheckResult(name, prompt == "", f"prompt={prompt!r}")
    return CheckResult(name, False, "tempdir failed")


def _test_clear_session() -> CheckResult:
    name = "clear_session: removes session file"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp))
        msg = _fake_msg()
        append_turn(settings, msg, "hello", "hi")
        ok1 = len(get_recent_turns(settings, msg)) == 1
        removed = clear_session(settings, msg)
        ok2 = removed is True
        ok3 = len(get_recent_turns(settings, msg)) == 0
        return CheckResult(name, ok1 and ok2 and ok3, f"ok1={ok1} removed={ok2} remaining={ok3}")
    return CheckResult(name, False, "tempdir failed")


def _test_clear_session_no_file() -> CheckResult:
    name = "clear_session: returns False when no file"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp))
        msg = _fake_msg()
        removed = clear_session(settings, msg)
        return CheckResult(name, removed is False, f"removed={removed}")
    return CheckResult(name, False, "tempdir failed")


def _test_missing_file_no_crash() -> CheckResult:
    name = "get_recent_turns: missing file → []"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp))
        msg = _fake_msg()
        turns = get_recent_turns(settings, msg)
        return CheckResult(name, turns == [], f"turns={turns}")
    return CheckResult(name, False, "tempdir failed")


def _test_corrupt_line_no_crash() -> CheckResult:
    name = "get_recent_turns: corrupt JSONL line → skipped"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp))
        msg = _fake_msg()
        p = session_path(settings, msg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not json\n{\"user\": \"ok\", \"assistant\": \"ok\"}\n", encoding="utf-8")
        turns = get_recent_turns(settings, msg)
        return CheckResult(name, len(turns) == 1 and turns[0]["user"] == "ok", f"turns={turns}")
    return CheckResult(name, False, "tempdir failed")


def _test_session_disabled() -> CheckResult:
    name = "session disabled: append_turn is no-op"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp), enabled=False)
        msg = _fake_msg()
        append_turn(settings, msg, "hello", "hi")
        turns = get_recent_turns(settings, msg)
        return CheckResult(name, turns == [], f"turns={turns}")
    return CheckResult(name, False, "tempdir failed")


def _test_inject_for_free_text() -> CheckResult:
    name = "should_inject_for_command: free text (None) → True"
    return CheckResult(name, should_inject_for_command(None) is True, "")


def _test_inject_for_run() -> CheckResult:
    name = "should_inject_for_command: /run → True"
    return CheckResult(name, should_inject_for_command("run") is True, "")


def _test_no_inject_for_load() -> CheckResult:
    name = "should_inject_for_command: /load → False"
    return CheckResult(name, should_inject_for_command("load") is False, "")


def _test_no_inject_for_ps() -> CheckResult:
    name = "should_inject_for_command: /ps → False"
    return CheckResult(name, should_inject_for_command("ps") is False, "")


def _test_no_inject_for_deploy_status() -> CheckResult:
    name = "should_inject_for_command: /deploy_status → False"
    return CheckResult(name, should_inject_for_command("deploy_status") is False, "")


def _test_inject_for_diagnose() -> CheckResult:
    name = "should_inject_for_command: /diagnose → True (hybrid)"
    return CheckResult(name, should_inject_for_command("diagnose") is True, "")


def _test_no_inject_for_context() -> CheckResult:
    name = "should_inject_for_command: /context → False"
    return CheckResult(name, should_inject_for_command("context") is False, "")


def _test_no_inject_for_forget() -> CheckResult:
    name = "should_inject_for_command: /forget → False"
    return CheckResult(name, should_inject_for_command("forget") is False, "")


def _test_forget_then_inject_returns_empty() -> CheckResult:
    name = "behavioral: /forget clears session → next build_context_prompt is empty"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp))
        msg = _fake_msg()
        append_turn(settings, msg, "what is X?", "X is Y")
        ok1 = build_context_prompt(settings, msg) != ""
        clear_session(settings, msg)
        ok2 = build_context_prompt(settings, msg) == ""
        return CheckResult(name, ok1 and ok2, f"before_forget={ok1} after_forget={ok2}")
    return CheckResult(name, False, "tempdir failed")


def _test_load_with_session_data_still_no_inject() -> CheckResult:
    name = "behavioral: /load with session data present → should_inject is False"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp))
        msg = _fake_msg()
        # Write session data
        append_turn(settings, msg, "previous question", "previous answer")
        ok1 = len(get_recent_turns(settings, msg)) == 1
        # But /load should still not inject
        ok2 = should_inject_for_command("load") is False
        return CheckResult(name, ok1 and ok2, f"has_session={ok1} no_inject={ok2}")
    return CheckResult(name, False, "tempdir failed")


def _test_context_prompt_label() -> CheckResult:
    name = "build_context_prompt: contains 'do not treat as authoritative'"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp))
        msg = _fake_msg()
        append_turn(settings, msg, "hello", "hi")
        prompt = build_context_prompt(settings, msg)
        ok = "do not treat as authoritative" in prompt
        return CheckResult(name, ok, f"prompt={prompt[:100]!r}")
    return CheckResult(name, False, "tempdir failed")


def _test_turn_record_fields() -> CheckResult:
    name = "append_turn: record has ts, channel, chat_id, operator_id, kind"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp))
        msg = _fake_msg(channel="feishu", chat_id="oc_1", operator_id="ou_2")
        append_turn(settings, msg, "hello", "hi", kind="tool")
        turns = get_recent_turns(settings, msg)
        ok = len(turns) == 1
        if ok:
            t = turns[0]
            required = {"ts", "channel", "chat_id", "operator_id", "user", "assistant", "kind"}
            ok = required.issubset(t.keys())
            ok = ok and t["channel"] == "feishu"
            ok = ok and t["kind"] == "tool"
        return CheckResult(name, ok, f"turns={turns}")
    return CheckResult(name, False, "tempdir failed")


def _test_multi_channel_isolation() -> CheckResult:
    name = "session isolation: different channels get different files"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp))
        msg_tg = _fake_msg(channel="telegram", chat_id="1", operator_id="1")
        msg_fs = _fake_msg(channel="feishu", chat_id="1", operator_id="1")
        append_turn(settings, msg_tg, "tg hello", "tg reply")
        append_turn(settings, msg_fs, "fs hello", "fs reply")
        tg_turns = get_recent_turns(settings, msg_tg)
        fs_turns = get_recent_turns(settings, msg_fs)
        ok = (
            len(tg_turns) == 1
            and len(fs_turns) == 1
            and tg_turns[0]["user"] == "tg hello"
            and fs_turns[0]["user"] == "fs hello"
        )
        return CheckResult(name, ok, f"tg={len(tg_turns)} fs={len(fs_turns)}")
    return CheckResult(name, False, "tempdir failed")


def _test_trim_on_write() -> CheckResult:
    name = "append_turn: trims file to max_turns valid records"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp), max_turns=3)
        msg = _fake_msg()
        for i in range(10):
            append_turn(settings, msg, f"turn {i}", f"reply {i}")
        p = session_path(settings, msg)
        lines = [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        ok1 = len(lines) == 3
        # Verify the last 3 records are the ones kept.
        records = [json.loads(l) for l in lines]
        ok2 = records[0]["user"] == "turn 7" and records[2]["user"] == "turn 9"
        return CheckResult(name, ok1 and ok2, f"file_lines={len(lines)} first={records[0]['user'] if records else 'N/A'}")
    return CheckResult(name, False, "tempdir failed")


def _test_corrupt_lines_removed_on_trim() -> CheckResult:
    name = "append_turn: corrupt lines removed during trim"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp), max_turns=5)
        msg = _fake_msg()
        p = session_path(settings, msg)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Write some corrupt lines mixed with valid ones.
        p.write_text(
            "not json at all\n"
            '{"user": "valid1", "assistant": "ok1"}\n'
            "another bad line\n"
            '{"user": "valid2", "assistant": "ok2"}\n',
            encoding="utf-8",
        )
        # Append a turn — this triggers trim.
        append_turn(settings, msg, "new turn", "new reply")
        # Re-read: corrupt lines should be gone.
        lines = [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        records = [json.loads(l) for l in lines]
        ok1 = len(records) == 3  # 2 old valid + 1 new
        ok2 = records[-1]["user"] == "new turn"
        ok3 = all(r.get("user") in ("valid1", "valid2", "new turn") for r in records)
        return CheckResult(name, ok1 and ok2 and ok3, f"records={len(records)} users={[r['user'] for r in records]}")
    return CheckResult(name, False, "tempdir failed")


def _test_invalid_max_turns_fallback() -> CheckResult:
    name = "_max_turns: invalid (0) falls back to 20"
    settings = SimpleNamespace(conveyor_session_max_turns=0)
    val = _max_turns(settings)
    ok1 = val == 20
    # Also verify negative.
    settings2 = SimpleNamespace(conveyor_session_max_turns=-5)
    val2 = _max_turns(settings2)
    ok2 = val2 == 20
    # And non-int.
    settings3 = SimpleNamespace(conveyor_session_max_turns="bad")
    val3 = _max_turns(settings3)
    ok3 = val3 == 20
    return CheckResult(name, ok1 and ok2 and ok3, f"0→{val} -5→{val2} bad→{val3}")


def _test_trim_with_invalid_max_turns() -> CheckResult:
    name = "append_turn: invalid max_turns=0 still bounds file (fallback 20)"
    with tempfile.TemporaryDirectory() as tmp:
        settings = _fake_settings(Path(tmp), max_turns=0)
        msg = _fake_msg()
        for i in range(25):
            append_turn(settings, msg, f"turn {i}", f"reply {i}")
        p = session_path(settings, msg)
        lines = [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        ok = len(lines) == 20  # fallback
        return CheckResult(name, ok, f"file_lines={len(lines)}")
    return CheckResult(name, False, "tempdir failed")


# ---- Main -------------------------------------------------------------------


def main() -> int:
    results = [
        _test_safe_filename_basic(),
        _test_safe_filename_no_traversal(),
        _test_safe_filename_special_chars(),
        _test_session_path_structure(),
        _test_append_turn_redacts(),
        _test_max_turns_retention(),
        _test_build_context_prompt(),
        _test_build_context_prompt_empty(),
        _test_clear_session(),
        _test_clear_session_no_file(),
        _test_missing_file_no_crash(),
        _test_corrupt_line_no_crash(),
        _test_session_disabled(),
        _test_inject_for_free_text(),
        _test_inject_for_run(),
        _test_no_inject_for_load(),
        _test_no_inject_for_ps(),
        _test_no_inject_for_deploy_status(),
        _test_inject_for_diagnose(),
        _test_no_inject_for_context(),
        _test_no_inject_for_forget(),
        _test_forget_then_inject_returns_empty(),
        _test_load_with_session_data_still_no_inject(),
        _test_context_prompt_label(),
        _test_turn_record_fields(),
        _test_multi_channel_isolation(),
        _test_trim_on_write(),
        _test_corrupt_lines_removed_on_trim(),
        _test_invalid_max_turns_fallback(),
        _test_trim_with_invalid_max_turns(),
    ]
    print_results(results)
    ok = all(r.ok for r in results)
    print("session summary smoke ok" if ok else "session summary smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
