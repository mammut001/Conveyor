"""handlers/goal_commands.py — answer a Supervisor goal from the phone.

`goal_notify` tells the owner 「目标「<title>」需要你回复：<question>」 on Telegram
and Feishu. These four commands are the other half: the owner answers from the
phone instead of opening the web console.

- `/goals` lists the goals that need the owner (`waiting_user` first, then
  `running`), each with its Chinese state, `第 N / M 轮` and the short id (the
  first 6 characters after ``goal-``) that the other three commands take.
- `/goal_reply <短编号> <你的回答>` pushes the goal forward with guidance.
- `/goal_done <短编号>` confirms the goal is achieved.
- `/goal_stop <短编号>` stops the goal.

`/goal_reply`, `/goal_done` and `/goal_stop` mutate goal state, so all four
commands run **only** for the configured owner and **only** in a private chat —
the same owner gate the runtime kill switch uses
(`agent_security_controls.owner_command`: verified owner id for the channel plus
`channel.auth.is_allowed`, `chat_type == "p2p"`). `/goals` is read-only but keeps
the same gate, because a goal title is nobody else's business. A Telegram Forum
Topic keeps its real `chat_type`, so a topic is correctly treated as a group.

Telegram and Feishu share `COMMAND_TABLE` (`handlers/commands.py`), so both
channels serve these commands and both `waiting_user` notifications carry the
matching `/goal_reply` hint (`goal_notify.REPLY_HINTS`).

All replies are Simplified Chinese; errors are the Chinese
`SupervisorServiceError` messages `supervisor_goals` already raises.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger("conveyor.handlers.goal_commands")

# Only these states are worth the owner's attention on a phone.
LISTED_STATES = ("waiting_user", "running")
# A phone message that lists 40 goals is a message nobody reads.
MAX_LISTED_GOALS = 10

STATE_LABELS = {
    "running": "进行中",
    "waiting_user": "等待你回复",
    "achieved": "已达成",
    "stopped": "已停止",
    "exhausted": "轮数用尽",
    "failed": "已失败",
}

OWNER_REFUSAL = "这个命令只有老板本人能用。"
GROUP_REFUSAL = "目标命令只在私聊里有效，请私聊我发送。"

USAGE_REPLY = "用法：/goal_reply <短编号> <你的回答>"
USAGE_DONE = "用法：/goal_done <短编号>"
USAGE_STOP = "用法：/goal_stop <短编号>"


def _goals_module():
    """Lazy import: `supervisor_goals` pulls in the whole supervisor stack."""
    from conveyor.agents import supervisor_goals
    return supervisor_goals


def _service_module():
    from conveyor.agents import supervisor_service
    return supervisor_service


# ---------------------------------------------------------------------------
# Owner gate (private chat, configured owner) — same rule as /agentkill
# ---------------------------------------------------------------------------

def owner_private(msg: Any, settings: Any) -> bool:
    """True only for the configured owner, in a private chat, on Telegram/Feishu."""
    from channel.auth import is_allowed

    if str(getattr(msg, "channel", "")) not in ("telegram", "feishu"):
        return False
    if str(getattr(msg, "chat_type", "")) != "p2p":
        return False
    if str(getattr(msg, "channel", "")) == "telegram":
        expected = str(getattr(settings, "telegram_allowed_user_id", "") or "")
    else:
        expected = str(getattr(settings, "lark_allowed_open_id", "") or "")
    if not expected or str(getattr(msg, "operator_id", "")) != expected:
        return False
    return bool(is_allowed(msg, settings))


async def _guard(msg: Any, port: Any, settings: Any) -> bool:
    """Reply in Chinese and return False when the command is not allowed here."""
    if owner_private(msg, settings):
        return True
    if str(getattr(msg, "chat_type", "")) != "p2p":
        await port.reply(msg, GROUP_REFUSAL)
    else:
        await port.reply(msg, OWNER_REFUSAL)
    return False


# ---------------------------------------------------------------------------
# Text (Simplified Chinese only — the owner reads this on a phone)
# ---------------------------------------------------------------------------

def _title(goal: dict) -> str:
    return " ".join(str(goal.get("title") or "").split()) or "未命名目标"


def _state_label(goal: dict) -> str:
    return STATE_LABELS.get(str(goal.get("state") or ""), str(goal.get("state") or "未知"))


def _rounds_text(goal: dict) -> str:
    used = goal.get("iterations_used")
    budget = goal.get("max_iterations")
    used = int(used) if isinstance(used, int) and used > 0 else 0
    budget = int(budget) if isinstance(budget, int) and budget > 0 else 0
    if used and budget:
        return f"第 {used} / {budget} 轮"
    if used:
        return f"第 {used} 轮"
    return "还没开始"


def _goal_line(index: int, goal: dict) -> str:
    short_id = _goals_module().short_goal_id(goal.get("id"))
    return f"{index}. 「{_title(goal)}」{_state_label(goal)} · {_rounds_text(goal)} · 短编号 {short_id}"


def goals_text(goals: list[dict]) -> str:
    """The `/goals` body: `waiting_user` first, then `running`, at most 10."""
    listed = [g for g in goals if isinstance(g, dict) and str(g.get("state") or "") in LISTED_STATES]
    # Newest first inside each group; the second (stable) sort puts the goals that
    # are actually waiting for the owner on top.
    listed.sort(key=lambda g: str(g.get("created_at") or ""), reverse=True)
    listed.sort(key=lambda g: 0 if str(g.get("state") or "") == "waiting_user" else 1)
    if not listed:
        return "当前没有需要你处理的目标。"
    shown = listed[:MAX_LISTED_GOALS]
    lines = [f"目标（{len(shown)} 个，等待你回复的在前）："]
    lines.extend(_goal_line(index, goal) for index, goal in enumerate(shown, start=1))
    if len(listed) > len(shown):
        lines.append(f"还有 {len(listed) - len(shown)} 个目标没显示出来。")
    lines.append("")
    lines.append("回复等待中的目标：/goal_reply <短编号> <你的回答>")
    lines.append("确认达成：/goal_done <短编号>")
    lines.append("停止：/goal_stop <短编号>")
    return "\n".join(lines)


def _reply_text(goal: dict) -> str:
    state = str(goal.get("state") or "")
    if state == "achieved":
        return f"目标「{_title(goal)}」已确认达成（{_rounds_text(goal)}）。"
    return f"已收到你的回复，目标「{_title(goal)}」继续跑（{_rounds_text(goal)}）。"


def _stop_text(goal: dict) -> str:
    state = str(goal.get("state") or "")
    if state == "achieved":
        return f"目标「{_title(goal)}」已经达成了，不用停。"
    if state in ("failed", "exhausted"):
        return f"目标「{_title(goal)}」已经结束了（{_state_label(goal)}），不用停。"
    return f"目标「{_title(goal)}」已停止。"


# ---------------------------------------------------------------------------
# Shared command plumbing
# ---------------------------------------------------------------------------

async def _run(port: Any, msg: Any, settings: Any, token: str,
               action: Callable[[str], dict]) -> dict | None:
    """Resolve the short id, run `action(goal_id)`, relay Chinese errors.

    Returns the updated goal, or None after the owner has already been told why
    nothing happened. Never raises: a bot command must not kill the dispatcher.
    """
    goals = _goals_module()
    svc = _service_module()
    try:
        goal_id = goals.resolve_goal_id(settings, token)
    except svc.SupervisorServiceError as exc:
        await port.reply(msg, exc.message)
        return None
    try:
        return action(goal_id)
    except svc.SupervisorServiceError as exc:
        await port.reply(msg, exc.message)
        return None
    except Exception:
        log.exception("goal command failed")
        await port.reply(msg, "这个操作没成功，请稍后再试。")
        return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def _goals(msg, port, _runner, settings, _arg):
    """List the goals that need the owner: waiting_user first, then running."""
    if not await _guard(msg, port, settings):
        return
    goals = _goals_module()
    svc = _service_module()
    try:
        listed = goals.list_goals(settings)
    except svc.SupervisorServiceError as exc:
        await port.reply(msg, exc.message)
        return
    except Exception:
        log.exception("goal list failed")
        await port.reply(msg, "目标列表没读出来，请稍后再试。")
        return
    await port.reply(msg, goals_text(listed))


async def _goal_reply(msg, port, _runner, settings, arg):
    """`/goal_reply <短编号> <你的回答>` — answer a waiting goal and keep going."""
    if not await _guard(msg, port, settings):
        return
    parts = (arg or "").strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await port.reply(msg, USAGE_REPLY)
        return
    token, message = parts[0], parts[1].strip()
    goals = _goals_module()

    def action(goal_id: str) -> dict:
        # No server handle: this runs in the bot process. `reply_goal` wakes the
        # goal loop through the registered wake hook instead.
        return goals.reply_goal(None, settings, goal_id, {"message": message, "achieved": False})

    goal = await _run(port, msg, settings, token, action)
    if goal is not None:
        await port.reply(msg, _reply_text(goal))


async def _goal_done(msg, port, _runner, settings, arg):
    """`/goal_done <短编号>` — confirm the goal is achieved."""
    if not await _guard(msg, port, settings):
        return
    token = (arg or "").strip()
    if not token:
        await port.reply(msg, USAGE_DONE)
        return
    goals = _goals_module()

    def action(goal_id: str) -> dict:
        return goals.reply_goal(None, settings, goal_id, {"achieved": True})

    goal = await _run(port, msg, settings, token, action)
    if goal is not None:
        await port.reply(msg, _reply_text(goal))


async def _goal_stop(msg, port, _runner, settings, arg):
    """`/goal_stop <短编号>` — stop the goal (and its active task)."""
    if not await _guard(msg, port, settings):
        return
    token = (arg or "").strip()
    if not token:
        await port.reply(msg, USAGE_STOP)
        return
    goals = _goals_module()

    def action(goal_id: str) -> dict:
        return goals.stop_goal(None, settings, goal_id)

    goal = await _run(port, msg, settings, token, action)
    if goal is not None:
        await port.reply(msg, _stop_text(goal))
