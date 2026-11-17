"""handlers/chat_intents_phone.py — the web chat intents on the phone.

The web 对话 (``conveyor/agents/chat_intents.py``) understands 「帮我建个 repo 叫 X」,
「迭代三个小时，把…做好」, 「进度怎么样了」, 「先停一下」, 「把 X 接着做」: deterministic
patterns first, then the intent decider, then single-use server-side action cards
executed via ``chat_intents.execute_action``. This module runs that *same*
resolution (``resolve_intent_turn``) for the owner on Telegram and Feishu,
rendering each card the way its channel can:

- **Telegram** — inline keyboard buttons whose ``callback_data`` carries only the
  server-side action id (``ci:<action_id>``); the callback handler runs the action
  and replies with its follow-up. Navigation-only web buttons (打开项目 / 查看进度 /
  查看改动) become a plain text hint — we never try to open the web app.
- **Feishu** — the same action ids as numbered text options; the owner's next
  message 「1」/「2」 picks one within the pending TTL (mirrors the web pending-name
  flow). Interactive Feishu card callbacks are not wired for chat intents in this
  codebase, so the numbered fallback is used.

Safety mirrors ``agent_switcher`` + ``goal_commands``: only the configured owner in
a private chat is served (``goal_commands.owner_private``); a button runs at most
once (``execute_action``'s claim) and only for the chat it was issued to
(``get_action_card_session``); other users or chats are refused politely.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Navigation-only web buttons cannot open the web app from a phone; show this.
_NAV_HINT = "（进度和改动在网页的「项目」里可以看到。）"
# The numbered-choice window matches the web pending-name TTL.
_CHOICE_TTL_SECONDS = 10 * 60


def _owner_private(msg: Any, settings: Any) -> bool:
    from handlers.goal_commands import owner_private
    return owner_private(msg, settings)


def _session_id(msg: Any) -> str:
    """A stable per-chat session key so a card is only usable where it was sent."""
    return f"{getattr(msg, 'channel', '')}:{getattr(msg, 'chat_id', '')}"


def _parse_choice(text: str) -> int | None:
    """「1」/「2」/… → zero-based index; anything else → ``None``."""
    raw = (text or "").strip()
    if not raw.isdigit():
        return None
    value = int(raw)
    return value - 1 if value >= 1 else None


def _split_buttons(card: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    from conveyor.agents.chat_intents import _NAVIGATION_ONLY_ACTIONS
    nav = _NAVIGATION_ONLY_ACTIONS
    buttons = [b for b in (card.get("buttons") or []) if isinstance(b, dict)]
    actionable = [b for b in buttons if b.get("action") not in nav]
    nav_only = [b for b in buttons if b.get("action") in nav]
    return actionable, nav_only


# ---- Rendering -------------------------------------------------------------


async def _render_card(
    msg: Any, port: Any, settings: Any, session_id: str,
    text: str, card: dict[str, Any] | None,
) -> None:
    """Deliver a reply + optional action card the way this channel can."""
    from conveyor.agents import chat_intents

    if card is None:
        await port.reply(msg, text)
        return

    actionable, nav_only = _split_buttons(card)
    hint = _NAV_HINT if nav_only else ""
    channel = str(getattr(msg, "channel", ""))

    if actionable and channel == "telegram" and getattr(port, "supports_inline_buttons", False):
        # Telegram: one inline button per action; callback_data is only the id.
        rows = [[{"text": str(b["label"]), "callback_data": f"ci:{b['id']}"}] for b in actionable]
        await port.reply_with_buttons(msg, text + hint, rows)
        return

    if actionable:
        # Feishu (and any channel without live buttons): numbered text options.
        lines = [text]
        for index, button in enumerate(actionable, start=1):
            lines.append(f"{index}. {button['label']}")
        lines.append(f"回复数字选择（{_CHOICE_TTL_SECONDS // 60} 分钟内有效）")
        if hint:
            lines.append(hint)
        chat_intents.store_pending_choice(
            settings, session_id, [str(b["id"]) for b in actionable],
            ttl_seconds=_CHOICE_TTL_SECONDS,
        )
        await port.reply(msg, "\n".join(lines))
        return

    # Nothing to press (only navigation buttons): plain text + hint.
    await port.reply(msg, text + hint)


# ---- Entry points ----------------------------------------------------------


async def maybe_handle(msg: Any, port: Any, settings: Any, text: str) -> bool:
    """Owner-only phone chat intents. Returns ``True`` when the turn was handled.

    Groups and non-owners return ``False`` immediately (``owner_private``), so the
    caller keeps its normal dispatch path.
    """
    if not _owner_private(msg, settings):
        return False
    from conveyor.agents import chat_intents

    session_id = _session_id(msg)
    raw = (text or "").strip()
    if not raw:
        return False

    # 1. A pending numbered choice (Feishu): the owner just answered 「1」/「2」.
    action_ids = chat_intents.pop_pending_choice(settings, session_id)
    if action_ids is not None:
        index = _parse_choice(raw)
        if index is not None and 0 <= index < len(action_ids):
            await run_action(settings, msg, port, action_ids[index])
            return True
        # Not a valid choice: drop the stale pending and let the message through.

    # 2. Same resolution as web chat (patterns → decider → cards).
    handled, reply_text, card, _notice = await chat_intents.resolve_intent_turn(
        settings, session_id, raw,
    )
    if not handled:
        return False
    await _render_card(msg, port, settings, session_id, reply_text, card)
    return True


async def run_action(settings: Any, msg: Any, port: Any, action_id: str) -> None:
    """Run a single-use action button the owner pressed, replying with the result.

    Owner + private-chat only (reuses goal_commands.owner_private), then a button
    that belongs to a different chat is refused, then
    ``chat_intents.execute_action`` enforces the one-use claim (a replay returns
    409 and is relayed politely).
    """
    from http import HTTPStatus

    from conveyor.agents import chat_intents

    if not _owner_private(msg, settings):
        await port.reply(msg, "这个操作只有老板本人能用。")
        return

    session_id = _session_id(msg)
    card_session = chat_intents.get_action_card_session(settings, action_id)
    if card_session is None:
        await port.reply(msg, "这个操作已经失效了，请重新操作。")
        return
    if card_session != session_id:
        await port.reply(msg, "这不是发给你的操作，不能替你点。")
        return

    status, resp = chat_intents.execute_action(None, settings, action_id)
    if status != HTTPStatus.OK:
        await port.reply(msg, str(resp.get("error") or "这个操作没成功，请稍后再试。"))
        return
    await _render_action_result(msg, port, settings, session_id, resp)


async def _render_action_result(
    msg: Any, port: Any, settings: Any, session_id: str, resp: dict[str, Any],
) -> None:
    from conveyor.agents import chat_intents

    follow_up = resp.get("follow_up_message") or {}
    text = str(follow_up.get("text") or "")
    card = follow_up.get("action_card")
    if card is not None or text:
        await _render_card(
            msg, port, settings, session_id, text,
            card if isinstance(card, dict) else None,
        )
        return
    # No follow-up reply (e.g. a cancel, or a web-only prefill/navigation action).
    outcome = str(resp.get("outcome") or "已处理。")
    client_action = resp.get("client_action") or {}
    if isinstance(client_action, dict) and client_action.get("type") == "prefill_chat":
        prefill = str(client_action.get("text") or "").strip()
        await port.reply(msg, f"{outcome}\n把这句话补全再发给我：{prefill}")
        return
    await port.reply(msg, outcome)
