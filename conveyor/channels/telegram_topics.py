"""Telegram Forum Topics (`message_thread_id`) integration for Conveyor Agents.

Enables Telegram's native Vertical Topics Bar (竖排话题栏) experience:
  1. `/topics` (or `/topics sync`) in a Forum-enabled group or Bot Threaded Mode
     automatically creates a dedicated Forum Topic per active Agent (`Conveyor`,
     `Finance`, `Researcher`, `Coder`, etc.), binds `message_thread_id` to that
     Agent, and sends + pins an Agent Status Card (`置顶消息`) at the top of each topic.
  2. `/topics bind <Agent>` inside any existing topic binds that topic thread to
     the specified Agent and pins its status card.
  3. Owner messages inside a bound topic (or private Bot topic thread) are
     automatically routed to that topic's bound Agent with isolated per-topic
     conversation history (`<chat_id>:topic:<thread_id>`), without requiring
     `@bot` mentions on every message.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any

from channel.types import InboundMessage
from redaction import redact_text, truncate

logger = logging.getLogger("conveyor.telegram_topics")

# Telegram's 6 allowed RGB integer icon colors for createForumTopic.
TOPIC_ICON_COLORS: tuple[int, ...] = (
    0x6FB9F0,  # blue
    0xFFD67E,  # yellow
    0xCB86DB,  # violet
    0x8EEE98,  # green
    0xFF93B2,  # rose
    0xFB6F5F,  # red-orange
)

# Local fallback markers used to detect a dead (closed/deleted) forum topic.
# The canonical check lives in channel.telegram._is_dead_topic_error; this is
# only consulted if that import is unavailable.
_DEAD_TOPIC_MARKERS: tuple[str, ...] = (
    "thread not found",
    "topic_deleted",
    "topic deleted",
    "topic_closed",
    "topic closed",
)


def _is_dead_topic_error(exc: BaseException) -> bool:
    """True when Telegram says the forum topic is closed/deleted.

    Delegates to :func:`channel.telegram._is_dead_topic_error` when it is
    importable (there is no circular import between the two modules) and
    falls back to a local marker check otherwise.
    """
    impl = None
    try:
        from channel.telegram import _is_dead_topic_error as impl
    except Exception:
        impl = None
    if impl is not None:
        try:
            return bool(impl(exc))
        except Exception:
            return False
    try:
        text = str(exc).lower()
    except Exception:
        return False
    return any(marker in text for marker in _DEAD_TOPIC_MARKERS)


# Per-base-chat locks that serialize sync_forum_topics so concurrent `/topics`
# runs cannot both read bindings and then both create duplicate topics.
_SYNC_LOCKS: dict[str, asyncio.Lock] = {}


def _sync_lock(chat_id: str | int) -> asyncio.Lock:
    """Return the module-level lock for `chat_id`'s base chat.

    The dict is bounded by the number of base chats the owner syncs.
    """
    key = str(base_chat_id(chat_id))
    lock = _SYNC_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SYNC_LOCKS[key] = lock
    return lock


def _owner_id(settings: Any) -> str:
    uid = getattr(settings, "telegram_allowed_user_id", None)
    return str(uid) if uid is not None else ""


def topic_chat_id(chat_id: str | int, thread_id: str | int) -> str:
    """Canonical topic-scoped chat_id (`<base_chat_id>:topic:<thread_id>`)."""
    base = str(chat_id or "").partition(":topic:")[0]
    return f"{base}:topic:{int(thread_id)}"


def base_chat_id(chat_id: str | int) -> str:
    return str(chat_id or "").partition(":topic:")[0]


def _connect(settings: Any) -> sqlite3.Connection:
    path = Path(settings.codex_memory_root) / "state" / "job_queue.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS telegram_topic_bindings (
               chat_id TEXT NOT NULL,
               thread_id INTEGER NOT NULL,
               operator TEXT NOT NULL,
               agent_id TEXT NOT NULL,
               title TEXT NOT NULL DEFAULT '',
               pinned_message_id INTEGER,
               updated_at REAL NOT NULL,
               PRIMARY KEY (chat_id, thread_id)
           )"""
    )
    return conn


def _resolve_agent(settings: Any, requested: str) -> dict[str, Any]:
    from conveyor.agents.agents import AgentStore
    store = AgentStore(settings)
    name = str(requested or "").strip()
    if not name:
        raise ValueError("请指定助手名称或 ID，例如：/topics bind Finance")
    if name.casefold() in ("main", "default", "conveyor"):
        default_agent = store.get("default")
        if default_agent and not default_agent.get("archived"):
            return default_agent
    matched = [
        item for item in store.list()
        if not item.get("archived")
        and (item["id"] == name or item["name"].casefold() == name.casefold())
    ]
    if len(matched) != 1:
        raise ValueError(f"未找到可用助手：{name}")
    return matched[0]


def bind_topic(
    settings: Any,
    *,
    chat_id: str | int,
    thread_id: int | str,
    operator: str,
    agent_id: str,
    title: str = "",
    pinned_message_id: int | None = None,
) -> dict[str, Any]:
    """Bind a Telegram `(chat_id, thread_id)` topic to an active Agent for the owner."""
    owner = _owner_id(settings)
    if not owner or str(operator) != owner:
        raise PermissionError("仅限本人操作 Telegram 话题绑定")
    agent = _resolve_agent(settings, agent_id)
    raw_chat = base_chat_id(chat_id)
    tid = int(thread_id)
    scoped_chat = topic_chat_id(raw_chat, tid)
    now = time.time()
    clean_title = truncate(redact_text((title or agent["name"]).strip()), 64)

    conn = _connect(settings)
    try:
        with conn:
            conn.execute(
                """INSERT INTO telegram_topic_bindings
                   (chat_id, thread_id, operator, agent_id, title, pinned_message_id, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                       operator = excluded.operator,
                       agent_id = excluded.agent_id,
                       title = excluded.title,
                       pinned_message_id = COALESCE(excluded.pinned_message_id, telegram_topic_bindings.pinned_message_id),
                       updated_at = excluded.updated_at""",
                (raw_chat, tid, owner, agent["id"], clean_title, pinned_message_id, now),
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS agent_channel_bindings(
                       channel TEXT NOT NULL CHECK(channel IN ('telegram','feishu')),
                       chat_id TEXT NOT NULL,
                       operator TEXT NOT NULL,
                       agent_id TEXT NOT NULL,
                       PRIMARY KEY(channel,chat_id))"""
            )
            if agent["id"] == "default":
                conn.execute(
                    "DELETE FROM agent_channel_bindings WHERE channel = 'telegram' AND chat_id = ?",
                    (scoped_chat,),
                )
            else:
                conn.execute(
                    """INSERT INTO agent_channel_bindings(channel, chat_id, operator, agent_id)
                       VALUES ('telegram', ?, ?, ?)
                       ON CONFLICT(channel, chat_id) DO UPDATE SET
                           agent_id = excluded.agent_id,
                           operator = excluded.operator""",
                    (scoped_chat, owner, agent["id"]),
                )
    finally:
        conn.close()

    from conveyor.agents import agent_conversations
    if agent_conversations.enabled(settings):
        synthetic = InboundMessage(
            channel="telegram",
            operator_id=owner,
            chat_id=scoped_chat,
            message_id=None,
            text="",
            chat_type="p2p",
            thread_id=str(tid),
        )
        try:
            agent_conversations.ensure_entry(settings, synthetic)
        except Exception:
            logger.debug("Skipping agent_conversations.ensure_entry for topic %s", scoped_chat, exc_info=True)
    return agent


def unbind_topic(settings: Any, *, chat_id: str | int, thread_id: int | str) -> bool:
    """Remove a Telegram topic binding for `(chat_id, thread_id)`.

    Deletes the `telegram_topic_bindings` row and the scoped
    `agent_channel_bindings` routing row written by `bind_topic`. Returns True
    when a binding row was actually removed.
    """
    owner = _owner_id(settings)
    if not owner:
        return False
    raw_chat = base_chat_id(chat_id)
    try:
        tid = int(thread_id)
    except (TypeError, ValueError):
        return False
    scoped_chat = topic_chat_id(raw_chat, tid)
    conn = _connect(settings)
    try:
        with conn:
            cur = conn.execute(
                """DELETE FROM telegram_topic_bindings
                   WHERE chat_id = ? AND thread_id = ? AND operator = ?""",
                (raw_chat, tid, owner),
            )
            removed = cur.rowcount > 0
            conn.execute(
                """CREATE TABLE IF NOT EXISTS agent_channel_bindings(
                       channel TEXT NOT NULL CHECK(channel IN ('telegram','feishu')),
                       chat_id TEXT NOT NULL,
                       operator TEXT NOT NULL,
                       agent_id TEXT NOT NULL,
                       PRIMARY KEY(channel,chat_id))"""
            )
            conn.execute(
                "DELETE FROM agent_channel_bindings WHERE channel = 'telegram' AND chat_id = ?",
                (scoped_chat,),
            )
    finally:
        conn.close()
    return removed


def bound_topic_agent(
    settings: Any,
    chat_id: str | int,
    thread_id: str | int | None,
) -> str | None:
    """Return the active Agent ID bound to `(chat_id, thread_id)`, or None."""
    if thread_id is None:
        return None
    owner = _owner_id(settings)
    if not owner:
        return None
    try:
        tid = int(thread_id)
    except (TypeError, ValueError):
        return None
    raw_chat = base_chat_id(chat_id)
    conn = _connect(settings)
    try:
        row = conn.execute(
            """SELECT agent_id FROM telegram_topic_bindings
               WHERE chat_id = ? AND thread_id = ? AND operator = ?""",
            (raw_chat, tid, owner),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    from conveyor.agents.agents import AgentStore
    agent = AgentStore(settings).get(row["agent_id"])
    return agent["id"] if agent and not agent.get("archived") else None


def list_topic_bindings(settings: Any, chat_id: str | int) -> list[dict[str, Any]]:
    """List active topic bindings for `chat_id`."""
    owner = _owner_id(settings)
    if not owner:
        return []
    raw_chat = base_chat_id(chat_id)
    conn = _connect(settings)
    try:
        rows = conn.execute(
            """SELECT * FROM telegram_topic_bindings
               WHERE chat_id = ? AND operator = ?
               ORDER BY thread_id ASC""",
            (raw_chat, owner),
        ).fetchall()
    finally:
        conn.close()
    from conveyor.agents.agents import AgentStore
    store = AgentStore(settings)
    active: list[dict[str, Any]] = []
    for row in rows:
        agent = store.get(row["agent_id"])
        if agent and not agent.get("archived"):
            item = dict(row)
            item["agent_name"] = agent["name"]
            active.append(item)
    return active


def contextualize_inbound(settings: Any, msg: InboundMessage) -> InboundMessage:
    """Scope an owner's Telegram topic message to its dedicated `<chat_id>:topic:<thread_id>` session.

    The message routes like a DM: `owner_topic=True` (accepted wherever a
    private chat is required for routing) and `mentioned_bot=True` so the bot
    answers without an @mention. `chat_type` is left untouched, so a topic in
    a supergroup forum keeps reading as a group for privacy guards.

    Leaves non-topic messages and unbound group chats completely untouched.
    """
    if msg.channel != "telegram" or not msg.thread_id:
        return msg
    owner = _owner_id(settings)
    if not owner or msg.operator_id != owner:
        return msg
    bound_agent = bound_topic_agent(settings, msg.chat_id, msg.thread_id)
    if bound_agent is None and msg.chat_type != "p2p":
        return msg
    return replace(
        msg,
        chat_id=topic_chat_id(msg.chat_id, msg.thread_id),
        # chat_type is deliberately preserved: a bound topic in a supergroup
        # forum is still a group other people can read, so privacy guards
        # (long-term memory, /agentkill, task status) must keep seeing
        # "group". owner_topic makes it route like a DM instead.
        owner_topic=True,
        mentioned_bot=True,
    )


def format_pinned_card(agent: dict[str, Any], *, thread_id: int) -> str:
    """Format the pinned status message (`置顶消息`) at the top of an Agent's Telegram Topic."""
    name = str(agent.get("name") or "Conveyor")
    role = str(agent.get("persona") or agent.get("instructions") or "").replace("\n", " ").strip()
    role_line = truncate(redact_text(role), 90) if role else "日常问答、查资料、跑命令与代码维护"
    workspace = str(agent.get("workspace_path") or "").strip()
    lines = [
        f"📌 [{name}] 专属话题置顶",
        f"职责：{role_line}",
    ]
    if workspace:
        lines.append(f"目录：{Path(workspace).name}")
    lines.extend([
        f"话题 ID：#{thread_id} · 历史记录独立保存",
        "在此话题内直接发消息即可与该助手对话（无需 @Bot）。",
        "发送 /conversations 可接续网页端已有会话，发送 /agents 可切换助手。",
    ])
    return "\n".join(lines)


async def _probe_topic_alive(bot: Any, chat_id: int | str, thread_id: int) -> bool:
    """Probe whether a forum topic still exists in Telegram.

    Returns True when the topic is healthy (the probe succeeded) **or** when
    the probe failed for a transient reason (network, permissions, missing
    method on the bot) so the existing binding is kept. Returns False only
    when Telegram explicitly reports the topic is closed/deleted.
    """
    try:
        await bot.send_chat_action(
            chat_id=chat_id,
            action="typing",
            message_thread_id=thread_id,
        )
        return True
    except Exception as exc:
        if _is_dead_topic_error(exc):
            logger.info(
                "Topic %s on chat %s is closed/deleted (%s); recreating",
                thread_id,
                chat_id,
                exc,
            )
            return False
        logger.debug(
            "Liveness probe for topic %s on chat %s failed transiently; "
            "keeping existing binding: %s",
            thread_id,
            chat_id,
            exc,
        )
        return True


async def sync_forum_topics(
    bot: Any,
    chat_id: int | str,
    operator_id: str,
    settings: Any,
) -> dict[str, Any]:
    """Create/sync one Telegram Forum Topic per active Agent and pin its status card."""
    owner = _owner_id(settings)
    if not owner or str(operator_id) != owner:
        raise PermissionError("仅限本人同步 Telegram 话题栏")
    # Serialize per base chat so concurrent `/topics` runs cannot both read the
    # bindings and then both create duplicate topics.
    async with _sync_lock(chat_id):
        return await _sync_forum_topics_locked(bot, chat_id, owner, settings)


async def _sync_forum_topics_locked(
    bot: Any,
    chat_id: int | str,
    owner: str,
    settings: Any,
) -> dict[str, Any]:
    from conveyor.agents.agents import AgentStore
    store = AgentStore(settings)
    agents = [a for a in store.list() if not a.get("archived")]
    if not agents:
        agents = [store.get("default") or {"id": "default", "name": "Conveyor", "instructions": ""}]

    existing_rows = list_topic_bindings(settings, chat_id)
    by_agent = {row["agent_id"]: row for row in existing_rows}
    created: list[dict[str, Any]] = []
    reused: list[dict[str, Any]] = []
    target_chat: int | str
    try:
        target_chat = int(base_chat_id(chat_id))
    except ValueError:
        target_chat = base_chat_id(chat_id)

    for idx, agent in enumerate(agents):
        if agent["id"] in by_agent:
            row = by_agent[agent["id"]]
            thread_id = int(row["thread_id"])
            # Probe liveness before reusing: if the owner deleted/closed the
            # topic in Telegram the binding row still exists, so recreate it.
            if await _probe_topic_alive(bot, target_chat, thread_id):
                reused.append(row)
                continue
            # Dead topic: drop the stale binding and fall through to create a
            # fresh topic for this agent.
            unbind_topic(settings, chat_id=target_chat, thread_id=thread_id)
            by_agent.pop(agent["id"], None)
        icon_color = TOPIC_ICON_COLORS[idx % len(TOPIC_ICON_COLORS)]
        topic_title = f"{agent['name']}"
        try:
            topic = await bot.create_forum_topic(
                chat_id=target_chat,
                name=topic_title[:120],
                icon_color=icon_color,
            )
        except Exception as exc:
            logger.info("create_forum_topic unavailable on chat %s: %s", target_chat, exc)
            return {
                "ok": False,
                "created": created,
                "existing": reused,
                "error": str(exc),
                "text": format_topics_guide(settings, chat_id, error=str(exc)),
            }
        tid = int(getattr(topic, "message_thread_id", 0) or 0)
        if tid <= 0:
            continue
        pinned_id: int | None = None
        try:
            card_text = format_pinned_card(agent, thread_id=tid)
            sent = await bot.send_message(
                chat_id=target_chat,
                message_thread_id=tid,
                text=card_text,
                disable_web_page_preview=True,
            )
            mid = getattr(sent, "message_id", None)
            if isinstance(mid, int):
                pinned_id = mid
                try:
                    await bot.pin_chat_message(
                        chat_id=target_chat,
                        message_id=mid,
                        disable_notification=True,
                    )
                except Exception:
                    logger.debug("pin_chat_message not permitted in topic %s", tid, exc_info=True)
        except Exception:
            logger.debug("Failed to send/pin welcome card in topic %s", tid, exc_info=True)

        bind_topic(
            settings,
            chat_id=target_chat,
            thread_id=tid,
            operator=owner,
            agent_id=agent["id"],
            title=topic_title,
            pinned_message_id=pinned_id,
        )
        created.append({"agent_id": agent["id"], "agent_name": agent["name"], "thread_id": tid})

    summary_lines = [
        "✅ Telegram 竖排话题栏已同步完成！",
        f"新增话题：{len(created)} 个 · 已有绑定：{len(reused)} 个",
        "",
    ]
    for item in created:
        summary_lines.append(f"• #{item['thread_id']} {item['agent_name']}（已自动绑定并置顶状态卡）")
    for item in reused:
        summary_lines.append(f"• #{item['thread_id']} {item['agent_name']}（已绑定）")
    summary_lines.extend([
        "",
        "💡 提示：在手机 Telegram 顶部往下拉或长按话题栏，选择「显示为竖排边栏 (Show as Vertical Bar)」，即可获得左侧竖排话题切换栏。",
    ])
    return {
        "ok": True,
        "created": created,
        "existing": reused,
        "text": "\n".join(summary_lines),
    }


def format_topics_guide(settings: Any, chat_id: str | int, *, error: str = "") -> str:
    """Return a clear Chinese guide when `/topics` is run in a non-Forum chat."""
    bindings = list_topic_bindings(settings, chat_id)
    from conveyor.agents.agents import AgentStore
    agents = [a for a in AgentStore(settings).list() if not a.get("archived")]
    agent_names = "、".join(a["name"] for a in agents[:6]) or "Conveyor"
    lines = [
        "🗂 Telegram 竖排话题栏（Forum Topics）设置指南",
        "",
        f"当前已配置助手：{agent_names}",
    ]
    if bindings:
        lines.append("当前聊天已绑定的话题：")
        for row in bindings:
            lines.append(f"• #{row['thread_id']} → {row['agent_name']}")
        lines.append("")
    lines.extend([
        "要在 Telegram 手机端开启左侧「竖排话题栏」并让每个话题对应一个助手：",
        "1️⃣ 新建一个只有你和本 Bot 的私密群组，在群设置中开启「话题 (Topics)」",
        "2️⃣ 将本 Bot 设为管理员（勾选「管理话题」和「置顶消息」权限）",
        "3️⃣ 在群内发送 `/topics`，Bot 会自动为每个助手创建专属话题并在顶部置顶状态卡",
        "4️⃣ 在 Telegram 中长按顶部话题栏 → 选择「显示为竖排边栏」，即可获得与网页端一致的左侧竖排栏体验！",
        "",
        "• 在任意已有话题内发送 `/topics bind <助手名>` 可将该话题绑定到指定助手。",
        "• 在普通私聊中也可直接发送 `/agents` 按钮切换当前助手。",
    ])
    return "\n".join(lines)
