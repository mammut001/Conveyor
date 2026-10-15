"""Owner-only Telegram/Feishu private-chat Agent binding (default off).

A channel/chat binding is a server-side lookup, not a capability token.
The only permitted operator is the configured allowlisted human owner.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import agent_context


def enabled(settings):
    return bool(agent_context.enabled(settings) and
        os.environ.get("CONVEYOR_AGENT_CHANNEL_ROUTING", "").lower()
        in ("1", "true", "yes", "on"))


def _owner(settings, channel):
    if channel == "telegram":
        return str(settings.telegram_allowed_user_id)
    if channel == "feishu":
        return str(settings.lark_allowed_open_id or "")
    return ""


def authorized(settings, msg):
    from channel.auth import is_allowed
    return (enabled(settings) and msg.channel in ("telegram", "feishu")
            and msg.chat_type == "p2p" and bool(msg.chat_id)
            and bool(_owner(settings, msg.channel))
            and msg.operator_id == _owner(settings, msg.channel)
            and is_allowed(msg, settings))


def _connect(settings):
    path = Path(settings.codex_memory_root) / "state" / "job_queue.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("""CREATE TABLE IF NOT EXISTS agent_channel_bindings(
        channel TEXT NOT NULL CHECK(channel IN ('telegram','feishu')),
        chat_id TEXT NOT NULL,
        operator TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        PRIMARY KEY(channel,chat_id))""")
    return connection


def bound(settings, channel, chat_id):
    """Return a currently live Agent ID or None; never trust an archived row."""
    if not enabled(settings) or channel not in ("telegram", "feishu") or not chat_id:
        return None
    operator = _owner(settings, channel)
    if not operator:
        return None
    db = _connect(settings)
    try:
        row = db.execute(
            "SELECT agent_id FROM agent_channel_bindings "
            "WHERE channel=? AND chat_id=? AND operator=?",
            (channel, str(chat_id), operator)).fetchone()
    finally:
        db.close()
    if row is None:
        return None
    import agents
    agent = agents.AgentStore(settings).get(row["agent_id"])
    return agent["id"] if agent and not agent["archived"] else None


def bind(settings, msg, requested):
    if not authorized(settings, msg):
        raise PermissionError("private owner chat required")
    from agents import AgentStore
    store = AgentStore(settings)
    name = str(requested or "").strip()
    if not name:
        raise ValueError("Usage: /agent <name|id|main>")
    if name.casefold() in ("main", "default"):
        agent = store.get("default")
    else:
        matched = [item for item in store.list()
                   if not item["archived"] and item["id"] != "default"
                   and (item["id"] == name or item["name"].casefold() == name.casefold())]
        if len(matched) != 1:
            raise ValueError("Unknown, archived or ambiguous Agent")
        agent = matched[0]
    if not agent or agent["archived"]:
        raise ValueError("Agent unavailable")
    db = _connect(settings)
    try:
        with db:
            if agent["id"] == "default":
                db.execute("DELETE FROM agent_channel_bindings WHERE channel=? AND chat_id=?",
                           (msg.channel, msg.chat_id))
            else:
                db.execute(
                    """INSERT INTO agent_channel_bindings(channel,chat_id,operator,agent_id)
                       VALUES (?,?,?,?)
                       ON CONFLICT(channel,chat_id) DO UPDATE SET
                       agent_id=excluded.agent_id,operator=excluded.operator""",
                    (msg.channel, msg.chat_id, msg.operator_id, agent["id"]))
    finally:
        db.close()
    return agent


def is_agent_command(settings, msg):
    if not enabled(settings) or msg.channel not in ("telegram", "feishu"):
        return False
    word = (msg.text.strip().split(maxsplit=1) or [""])[0]
    return word.split("@", 1)[0].casefold() in ("/agent", "/agents")


async def maybe_command(settings, msg, port):
    """Owner DMs only; ignore agent selection in group and non-owner chats."""
    if not is_agent_command(settings, msg):
        return False
    if not authorized(settings, msg):
        return True  # silent group/non-owner refusal, no binding
    raw = msg.text.strip()
    word, _, argument = raw.partition(" ")
    word = word.split("@", 1)[0].casefold()  # Telegram /agent@bot username
    if word == "/agents":
        import agents
        from redaction import truncate
        items = [a for a in agents.AgentStore(settings).list() if not a["archived"]]
        lines = ["Available Agents:", "• main — default assistant"]
        for agent in items:
            if agent["id"] == "default":
                continue
            persona = str(agent.get("persona") or "").replace("\n", " ").strip()
            lines.append(f'• {agent["name"]} ({agent["id"]}) — {truncate(persona, 80)}')
        await port.reply(msg, "\n".join(lines))
        return True
    if word != "/agent":
        return False
    try:
        selected = bind(settings, msg, argument)
    except (ValueError, PermissionError) as exc:
        await port.reply(msg, str(exc))
    else:
        await port.reply(msg, f'[{selected["name"]}] Selected Agent. '
                        "Further messages use this Agent's history and tools.")
    return True


class LabeledPort:
    """Prefix safe channel replies; preserve original chat/message credentials."""
    def __init__(self, port, name):
        self._port = port
        self._prefix = f"[{name}] "
        self.supports_inline_buttons = getattr(port, "supports_inline_buttons", False)
        self.supports_attachments = getattr(port, "supports_attachments", False)

    def __getattr__(self, name):
        return getattr(self._port, name)

    def _label(self, text):
        return self._prefix + text if isinstance(text, str) and not text.startswith(self._prefix) else text

    async def reply(self, msg, text):
        return await self._port.reply(msg, self._label(text))

    async def send_new(self, msg, text):
        return await self._port.send_new(msg, self._label(text))

    async def edit_progress(self, msg, placeholder_id, text):
        return await self._port.edit_progress(msg, placeholder_id, self._label(text))

    async def reply_with_buttons(self, msg, text, buttons):
        return await self._port.reply_with_buttons(msg, self._label(text), buttons)

    async def send_card(self, msg, card):
        return await self._port.send_card(msg, card)


def label_port(settings, msg, port):
    if not authorized(settings, msg):
        return port
    selected = bound(settings, msg.channel, msg.chat_id)
    if selected:
        import agents
        info = agents.AgentStore(settings).get(selected)
        if info:
            return LabeledPort(port, info["name"])
    return port
