#!/usr/bin/env python3
"""scripts/topic_watch_smoke.py — verify proactive topic watch & push notifications.

Tests:
1. Watch lifecycle: subscribe (/watch), list (/watches), cancel (/unwatch).
2. Content digest computation & change detection.
3. check_topic_watches_and_send triggers on new results and skips when digest is identical.
4. Dry-run mode functions without sending messages or updating digests.
"""
from __future__ import annotations

import dataclasses
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings, load_settings
from personal_tools.store import PersonalToolsStore
from personal_tools.topic_watch import (
    compute_digest,
    watch_topic,
    unwatch_topic,
    list_watches,
    check_topic_watches_and_send,
)
from personal_tools.web_search import SearchResult

TMP = Path(tempfile.mkdtemp(prefix="conveyor-topic-watch-smoke-"))


def _settings(**kw) -> Settings:
    base = dataclasses.replace(
        load_settings(),
        codex_workspace_root=TMP / "ws",
        codex_task_root=TMP / "tasks",
        codex_memory_root=TMP / "mem",
        web_search_backend="brave",
    )
    return dataclasses.replace(base, **kw)


def _clean_db(settings: Settings) -> None:
    PersonalToolsStore(settings)
    from personal_tools.store import _connect
    with _connect(settings) as conn:
        conn.execute("DELETE FROM topic_watches")
        conn.commit()


def test_watch_lifecycle():
    settings = _settings()
    _clean_db(settings)
    store = PersonalToolsStore(settings)

    # 1. Create watch
    res = watch_topic(settings, "op1", "telegram", "12345", "OpenAI agents", 4.0)
    assert res.ok is True, f"watch creation failed: {res.text}"
    assert "已添加话题关注 #1" in res.text

    # 2. List watches
    res_list = list_watches(settings, "op1")
    assert res_list.ok is True
    assert "OpenAI agents" in res_list.text
    assert "#1" in res_list.text

    # 3. Unwatch
    res_unwatch = unwatch_topic(settings, "op1", 1)
    assert res_unwatch.ok is True
    assert "已取消话题关注 #1" in res_unwatch.text

    # 4. List again (empty)
    res_empty = list_watches(settings, "op1")
    assert "没有正在关注的话题" in res_empty.text

    print("[ok] topic watch: subscribe, list, and unwatch lifecycle works")


def test_change_detection_and_send():
    settings = _settings()
    _clean_db(settings)
    store = PersonalToolsStore(settings)

    # Add active watch
    watch_topic(settings, "op2", "telegram", "999", "Claude 3.8", 1.0)

    delivered_messages = []

    def fake_deliver(settings, channel, chat_id, text):
        delivered_messages.append((channel, chat_id, text))
        return True

    results_run1 = [
        SearchResult(title="Claude 3.8 Released", url="https://example.com/c38", snippet="Anthropic launched Claude 3.8", source="brave", rank=1),
        SearchResult(title="Benchmarks for Claude 3.8", url="https://example.com/bench", snippet="Top scores", source="brave", rank=2),
    ]

    from datetime import timedelta
    t0 = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)

    # Patch web search and message delivery
    with patch("personal_tools.topic_watch.search_web", return_value=(results_run1, "")), \
         patch("personal_tools.topic_watch._deliver_topic_message", side_effect=fake_deliver):

        # First run at t0: should detect new content and deliver
        sent = check_topic_watches_and_send(settings, now_utc=t0)
        assert sent == 1, f"expected 1 sent notification, got {sent}"
        assert len(delivered_messages) == 1
        assert "Claude 3.8 Released" in delivered_messages[0][2]
        assert "https://example.com/c38" in delivered_messages[0][2]

        # Second run at t0 + 2h with identical search results: due, but should skip!
        delivered_messages.clear()
        sent_again = check_topic_watches_and_send(settings, now_utc=t0 + timedelta(hours=2))
        assert sent_again == 0, f"expected 0 sent for identical content, got {sent_again}"
        assert len(delivered_messages) == 0

        # Third run at t0 + 4h with fresh results: due and has changes -> deliver!
        results_run2 = [
            SearchResult(title="Claude 3.8 Update 2", url="https://example.com/c38-v2", snippet="New patch released", source="brave", rank=1),
        ]
        with patch("personal_tools.topic_watch.search_web", return_value=(results_run2, "")):
            sent_new = check_topic_watches_and_send(settings, now_utc=t0 + timedelta(hours=4))
            assert sent_new == 1, f"expected 1 sent for updated content, got {sent_new}"
            assert len(delivered_messages) == 1
            assert "Claude 3.8 Update 2" in delivered_messages[0][2]

    print("[ok] topic watch: change detection correctly triggers on new content and skips duplicates")


def test_dry_run_mode():
    settings = _settings()
    _clean_db(settings)
    watch_topic(settings, "op3", "telegram", "888", "Rust 2026", 2.0)

    results = [
        SearchResult(title="Rust 2026 Edition", url="https://example.com/rust", snippet="Rust roadmap", source="brave", rank=1),
    ]

    with patch("personal_tools.topic_watch.search_web", return_value=(results, "")):
        sent = check_topic_watches_and_send(settings, dry_run=True)
        assert sent == 1, f"dry-run should report would-send count, got {sent}"

        # Check DB: last_digest should NOT be set because dry_run=True
        watches = PersonalToolsStore(settings).list_topic_watches("op3")
        assert watches[0].last_digest is None, "dry-run must not mutate last_digest"

    print("[ok] topic watch: dry-run reports without sending or modifying database")


def _sr(title, url, snippet="s"):
    return SearchResult(title=title, url=url, snippet=snippet, source="brave", rank=1)


def _run(settings, results, when, delivered):
    def fake_deliver(settings, channel, chat_id, text):
        delivered.append(text)
        return True

    with patch("personal_tools.topic_watch.search_web", return_value=(results, "")), \
         patch("personal_tools.topic_watch._deliver_topic_message", side_effect=fake_deliver):
        return check_topic_watches_and_send(settings, now_utc=when)


def test_only_new_urls_are_pushed():
    from datetime import timedelta

    settings = _settings()
    _clean_db(settings)
    watch_topic(settings, "op4", "telegram", "1", "Rust", 1.0)
    t0 = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)
    delivered: list[str] = []
    base = [_sr("A", "https://www.a.example/post/"), _sr("B", "https://b.example/x?id=1")]
    assert _run(settings, base, t0, delivered) == 1
    assert delivered[0].startswith("🔔 开始关注：Rust")

    # Same pages re-ranked, with tracking params / scheme / www changes.
    shuffled = [
        _sr("B (retitled)", "http://b.example/x?utm_source=feed&id=1"),
        _sr("A", "https://a.example/post#comments"),
    ]
    delivered.clear()
    assert _run(settings, shuffled, t0 + timedelta(hours=2), delivered) == 0, delivered

    # One genuinely new page: only it is listed.
    delivered.clear()
    more = shuffled + [_sr("C new", "https://c.example/new")]
    assert _run(settings, more, t0 + timedelta(hours=4), delivered) == 1
    text = delivered[0]
    assert "新增 1 条" in text and "C new" in text and "a.example" not in text, text
    print("[ok] topic watch: re-ranking / tracking params are not news; only new URLs are pushed")


def _chat_settings():
    return _settings(chat_mode="auto", chat_api_key="k", chat_model="m", chat_base_url="http://127.0.0.1:9")


def _fake_stream(reply=None, error=False):
    async def fake(config, messages):
        if error:
            from runner.chat_client import ChatError
            raise ChatError("boom")
        fake.messages = messages
        yield reply

    return fake


def test_brief_and_skip():
    from datetime import timedelta

    settings = _chat_settings()
    _clean_db(settings)
    watch_topic(settings, "op5", "telegram", "1", "Claude", 1.0)
    t0 = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)
    delivered: list[str] = []

    fake = _fake_stream("Claude 5 发布了新版本，详见 https://evil.example/x [[CONFIDENCE: high]]")
    with patch("runner.chat_client.stream_chat", fake):
        assert _run(settings, [_sr("Claude 5 out", "https://a.example/1", "snippet text")], t0, delivered) == 1
    text = delivered[0]
    user_prompt = fake.messages[-1]["content"]
    assert "Claude 5 发布了新版本" in text and "evil.example" not in text and "[[" not in text, text
    assert "snippet text" not in text.split("来源")[1], "snippets are dropped when a brief exists"
    assert "Claude 5 out" in user_prompt and "untrusted" in fake.messages[0]["content"]

    delivered.clear()
    with patch("runner.chat_client.stream_chat", _fake_stream("[[SKIP]]")):
        assert _run(settings, [_sr("Unrelated", "https://b.example/2")], t0 + timedelta(hours=2), delivered) == 0
    # The skipped URL is remembered, so it is not re-judged next time.
    watches = PersonalToolsStore(settings).list_topic_watches("op5")
    assert "b.example/2" in watches[0].seen_urls

    delivered.clear()
    with patch("runner.chat_client.stream_chat", _fake_stream(error=True)):
        assert _run(settings, [_sr("New thing", "https://c.example/3", "plain snippet")], t0 + timedelta(hours=4), delivered) == 1
    assert "New thing" in delivered[0] and "plain snippet" in delivered[0]
    print("[ok] topic watch: model brief (links stripped), [[SKIP]] veto, fallback listing on errors")


def test_legacy_watch_baselines_silently_and_migration():
    import sqlite3
    from datetime import timedelta

    settings = _settings()
    _clean_db(settings)
    watch_topic(settings, "op6", "telegram", "1", "Go", 1.0)
    store = PersonalToolsStore(settings)
    w = store.list_topic_watches("op6")[0]
    store.update_topic_watch_check(w.id, "2026-09-01T00:00:00+00:00", "olddigest")  # pre-upgrade row
    delivered: list[str] = []
    t0 = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)
    assert _run(settings, [_sr("Old news", "https://a.example/old")], t0, delivered) == 0
    assert _run(settings, [_sr("Old news", "https://a.example/old"), _sr("Fresh", "https://a.example/new")],
                t0 + timedelta(hours=2), delivered) == 1
    assert "Fresh" in delivered[0] and "Old news" not in delivered[0]

    # An old database without the seen_urls column is migrated on open.
    old = TMP / "old-mem"
    old.mkdir(parents=True, exist_ok=True)
    legacy_settings = _settings(codex_memory_root=old)
    PersonalToolsStore(legacy_settings)
    from personal_tools.store import _connect
    with _connect(legacy_settings) as conn:
        conn.execute("DROP TABLE topic_watches")
        conn.execute(
            "CREATE TABLE topic_watches (id INTEGER PRIMARY KEY AUTOINCREMENT, operator_id TEXT NOT NULL, "
            "topic TEXT NOT NULL, interval_minutes INTEGER NOT NULL DEFAULT 360, channel TEXT NOT NULL DEFAULT 'telegram', "
            "chat_id TEXT NOT NULL DEFAULT '', last_checked_at TEXT, last_digest TEXT, status TEXT NOT NULL DEFAULT 'active', "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.commit()
    PersonalToolsStore(legacy_settings)
    with _connect(legacy_settings) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(topic_watches)")}
    assert "seen_urls" in cols, cols
    print("[ok] topic watch: legacy watches baseline silently; old databases gain seen_urls")


def main():
    try:
        test_watch_lifecycle()
        test_change_detection_and_send()
        test_dry_run_mode()
        test_only_new_urls_are_pushed()
        test_brief_and_skip()
        test_legacy_watch_baselines_silently_and_migration()
        print("topic watch smoke ok")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
