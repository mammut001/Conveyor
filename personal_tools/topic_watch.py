"""personal_tools/topic_watch.py — Proactive Topic Watch & Push Notifications.

Allows operators to subscribe to topics (/watch <topic> [interval_hours]).
Periodic scheduler ticks check web search for fresh developments, compute
content diff digests, and push updates to supported chat channels when real
changes occur.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from config import Settings
from personal_tools.base import ToolResult
from personal_tools.store import PersonalToolsStore, TopicWatchRow
from personal_tools.web_search import search_web, SearchResult
from redaction import redact_text, truncate

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS = 6.0
MIN_INTERVAL_HOURS = 0.5
MAX_INTERVAL_HOURS = 168.0  # 7 days
SUPPORTED_WATCH_CHANNELS = {"telegram"}


def compute_digest(results: list[SearchResult]) -> str:
    """Compute a SHA-256 digest of top search results to detect changes."""
    raw = "\n".join(f"{r.url.strip()}|{r.title.strip()}" for r in results[:3])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def watch_topic(
    settings: Settings,
    operator_id: str,
    channel: str,
    chat_id: str,
    topic: str,
    interval_hours: float = DEFAULT_INTERVAL_HOURS,
) -> ToolResult:
    """Subscribe to proactive updates for a topic."""
    topic = topic.strip()
    if not topic:
        return ToolResult(ok=False, text="⚠️ 请提供要关注的话题或关键词。用法：/watch <话题> [小时数]")
    if channel not in SUPPORTED_WATCH_CHANNELS:
        return ToolResult(
            ok=False,
            text="⚠️ 当前话题关注仅支持 Telegram 推送；Feishu 推送尚未实现，因此没有创建订阅。",
        )

    interval_hours = max(MIN_INTERVAL_HOURS, min(MAX_INTERVAL_HOURS, float(interval_hours)))
    interval_minutes = int(interval_hours * 60)

    store = PersonalToolsStore(settings)
    watch = store.create_topic_watch(
        operator_id=operator_id,
        topic=topic,
        channel=channel,
        chat_id=chat_id,
        interval_minutes=interval_minutes,
    )
    return ToolResult(
        ok=True,
        text=(
            f"✅ 已添加话题关注 #{watch.id}\n"
            f"• 话题：{watch.topic}\n"
            f"• 检查周期：每 {interval_hours:g} 小时\n"
            f"• 推送通道：{watch.channel}\n\n"
            f"有新动态时将自动推送。发 /unwatch {watch.id} 可取消关注。"
        ),
    )


def unwatch_topic(settings: Settings, operator_id: str, watch_id: int) -> ToolResult:
    """Cancel a topic subscription."""
    store = PersonalToolsStore(settings)
    ok = store.delete_topic_watch(operator_id, watch_id)
    if ok:
        return ToolResult(ok=True, text=f"✅ 已取消话题关注 #{watch_id}。")
    return ToolResult(ok=False, text=f"⚠️ 未找到有效的话题关注 #{watch_id}。发 /watches 查看当前关注列表。")


def list_watches(settings: Settings, operator_id: str) -> ToolResult:
    """List all active topic watches for this operator."""
    store = PersonalToolsStore(settings)
    watches = store.list_topic_watches(operator_id, status="active")
    if not watches:
        return ToolResult(
            ok=True,
            text="📋 当前没有正在关注的话题。\n使用 /watch <话题> [小时数] 添加关注。",
        )

    lines = [f"📋 正在关注的话题 ({len(watches)} 个)："]
    for w in watches:
        hours = w.interval_minutes / 60
        last = w.last_checked_at[:16].replace("T", " ") if w.last_checked_at else "尚未检查"
        lines.append(f"• #{w.id} [{hours:g}h/次] {w.topic}（上次检查: {last}）")
    lines.append("\n发 /unwatch <id> 可取消关注。")
    return ToolResult(ok=True, text="\n".join(lines))


def _deliver_topic_message(settings: Settings, channel: str, chat_id: str, text: str) -> bool:
    """Deliver proactive notification to the operator's channel."""
    if channel == "telegram":
        try:
            from scripts.telegram_api import send_message
            send_message(settings, text, chat_id=int(chat_id))
            return True
        except Exception as exc:
            logger.error("Failed to send Telegram topic watch notification: %s", exc)
            return False
    if channel == "feishu":
        logger.warning("Feishu delivery not yet implemented for topic watches")
        return False
    logger.warning("Unknown channel %s for topic watch", channel)
    return False


def check_topic_watches_and_send(
    settings: Settings,
    *,
    now_utc: datetime | None = None,
    dry_run: bool = False,
) -> int:
    """Evaluate due topic watches, query web search, and push diffs.

    Returns the number of notifications sent.
    """
    if getattr(settings, "web_search_backend", "disabled") == "disabled":
        logger.debug("Topic watches: web search is disabled, skipping checks")
        return 0

    store = PersonalToolsStore(settings)
    now = now_utc or datetime.now(timezone.utc)
    due = store.list_due_topic_watches(now=now)
    if not due:
        return 0

    now_iso = now.replace(microsecond=0).isoformat()
    sent_count = 0

    for w in due:
        results, err = search_web(settings, w.topic, limit=5)
        if err or not results:
            if err:
                logger.warning("Topic watch #%d (%s) search failed: %s", w.id, w.topic, err)
            continue

        digest = compute_digest(results)
        if w.last_digest and digest == w.last_digest:
            # No significant change detected since last check
            if not dry_run:
                store.update_topic_watch_check(w.id, now_iso, digest)
            continue

        # New content detected (or initial check)
        lines = [f"🔔 话题动态更新：{w.topic}\n"]
        for i, r in enumerate(results[:3], 1):
            title = r.title.strip() or "网页链接"
            snippet = r.snippet.strip()
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            lines.append(f"{i}. {title}\n   {r.url}")
            if snippet:
                lines.append(f"   {snippet}")
        lines.append(f"\n发 /unwatch {w.id} 可取消关注。")
        text = "\n".join(lines)

        if dry_run:
            logger.info("[dry-run] would deliver topic watch #%d to %s:%s", w.id, w.channel, w.chat_id)
            sent_count += 1
            continue

        ok = _deliver_topic_message(settings, w.channel, w.chat_id, text)
        if ok:
            sent_count += 1
            store.update_topic_watch_check(w.id, now_iso, digest)
            logger.info("Delivered topic watch #%d notification to %s:%s", w.id, w.channel, w.chat_id)
        else:
            logger.warning("Failed to deliver topic watch #%d notification", w.id)

    return sent_count
