#!/usr/bin/env python3
"""scripts/chat_memory_smoke.py — verify persistent chat memory across restarts.

Tests:
1. In-memory turns are written through to SQLite (chat_memory.db).
2. Simulated restart (wiping in-memory dicts) successfully restores conversation turns.
3. /deep last request is preserved across simulated restart.
4. /chat_clear empties both memory and database.
5. Session TTL expiry returns empty active window.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dataclasses
from config import Settings, load_settings
from handlers import chat, chat_memory
from handlers.chat import LastRequest

TMP = Path(tempfile.mkdtemp(prefix="conveyor-chat-memory-smoke-"))


def _settings(**kw) -> Settings:
    base = dataclasses.replace(
        load_settings(),
        chat_mode="auto",
        chat_history_turns=4,
        codex_task_root=TMP / "tasks",
        codex_memory_root=TMP / "mem",
    )
    return dataclasses.replace(base, **kw)


def test_persistence_and_restart():
    settings = _settings()
    key = "telegram:123"

    chat.reset(settings=settings)

    # 1. Add turns
    chat.remember(key, "Hello bot", "Hello human!", 4, settings=settings)
    chat.remember(key, "Who are you?", "I am Conveyor.", 4, settings=settings)

    h1 = chat.history(key, 4, settings=settings)
    assert len(h1) == 4, f"expected 4 turns, got {len(h1)}"
    assert h1[0]["content"] == "Hello bot"
    assert h1[3]["content"] == "I am Conveyor."

    # 2. Simulate process restart by clearing in-memory structures only
    chat._threads.clear()
    assert key not in chat._threads

    # 3. Fetch history again — must reload from SQLite
    h2 = chat.history(key, 4, settings=settings)
    assert len(h2) == 4, f"after restart expected 4 turns from SQLite, got {len(h2)}"
    assert h2[0]["content"] == "Hello bot"
    assert h2[3]["content"] == "I am Conveyor."

    # 4. Verify in-memory cache was repopulated
    assert key in chat._threads
    print("[ok] chat memory: conversation turns persist and recover across restarts")


def test_last_request_restart():
    settings = _settings()
    key = "telegram:456"

    chat.reset(settings=settings)

    # Save a /deep request
    chat.set_last(key, LastRequest(codex_prompt="re-run this query", confirm=True), settings=settings)

    # Simulate restart
    chat._last.clear()
    assert key not in chat._last

    # Pop last request — should come from SQLite
    popped = chat.pop_last(key, settings=settings)
    assert popped is not None, "expected popped request from SQLite"
    assert popped.codex_prompt == "re-run this query"
    assert popped.confirm is True

    # Subsequent pop should return None (already consumed)
    assert chat.pop_last(key, settings=settings) is None
    print("[ok] chat memory: /deep last request persists and pops across restarts")


def test_chat_clear():
    settings = _settings()
    key = "telegram:789"

    chat.remember(key, "Q1", "A1", 4, settings=settings)
    chat.set_last(key, LastRequest(codex_prompt="P1", confirm=False), settings=settings)

    # Clear chat
    chat.reset(key, settings=settings)

    # Verify memory and DB are empty for this key
    assert chat.history(key, 4, settings=settings) == []
    assert chat.pop_last(key, settings=settings) is None
    print("[ok] chat memory: reset/clear empties memory and database")


def test_ttl_expiry():
    settings = _settings()
    key = "telegram:ttl"

    chat.reset(settings=settings)
    t0 = 1000.0
    chat.remember(key, "old msg", "old reply", 4, now=t0, settings=settings)

    # Simulate restart
    chat._threads.clear()

    # Query with now = t0 + 1 hour (> 30 min TTL)
    h = chat.history(key, 4, now=t0 + 3600, settings=settings)
    assert h == [], f"expected empty history for expired session, got {h}"

    # Query with now = t0 + 10 min (< 30 min TTL)
    h_valid = chat.history(key, 4, now=t0 + 600, settings=settings)
    assert len(h_valid) == 2
    print("[ok] chat memory: session TTL correctly expires stale history")


def main():
    try:
        test_persistence_and_restart()
        test_last_request_restart()
        test_chat_clear()
        test_ttl_expiry()
        print("chat memory smoke ok")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
