"""Read-only Telegram quick-control menus backed by current Conveyor state.

The Telegram chat is *not* a Web agent session. Navigation may inspect jobs
and workers, but never remaps an operator's chat ID or grants Apply/Discard.
Callbacks are selectors only; every selection is re-validated against the
authenticated physical Telegram chat and operator before any data is shown.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from redaction import redact_text, truncate
from transcript_store import get_transcript_store, session_identity

_NAV_PREFIX = "tgn:"
_LEGACY = {
    "👷 我的 workers": "workers",
    "👷 我的workers": "workers",
    "🔄 切换会话": "sessions",
    "我的 workers": "workers",
    "我的workers": "workers",
    "切换会话": "sessions",
}


@dataclass(frozen=True)
class NavigationScreen:
    text: str
    buttons: tuple[tuple[tuple[str, str], ...], ...]


def legacy_action(text: str) -> str | None:
    """Match an entire legacy reply-keyboard label, never a normal prompt."""
    normalized = " ".join(str(text or "").replace("\ufe0f", "").strip().split()).lower()
    return _LEGACY.get(normalized)


def _short(value: Any, limit: int = 72) -> str:
    return truncate(redact_text(str(value or "").replace("\n", " ")), limit)


def _job_token(job_id: str) -> str:
    return hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]


def _physical_jobs(queue: Any, operator_id: str, chat_id: str, *, limit: int = 40) -> list[dict]:
    """Always filter at SQL before limit; never list another operator's tasks."""
    return queue.list_jobs(
        limit, session_id=chat_id, channel="telegram", operator_id=operator_id,
    )


def _agent_rows(settings: Any) -> list[dict]:
    import agents
    if not agents.enabled(settings):
        return []
    return agents.AgentStore(settings).list()


def workers_screen(settings: Any, queue: Any, operator_id: str, chat_id: str) -> NavigationScreen:
    jobs = _physical_jobs(queue, operator_id, chat_id)
    running = sum(job.get("state") == "running" for job in jobs)
    queued = sum(job.get("state") == "queued" for job in jobs)
    lines = [
        "👷 Workers · Conveyor",
        f"当前 Telegram 聊天最近 {len(jobs)} 条任务中：运行中 {running} · 排队中 {queued}",
    ]
    buttons: list[tuple[tuple[str, str], ...]] = []
    agents = _agent_rows(settings)
    if agents:
        lines.append("\n已配置的命名 Agent（Web 工作区）：")
        for agent in agents[:20]:
            agent_id = str(agent["id"])
            lines.append(f"• {_short(agent.get('name'), 48)}")
            buttons.append(((f"👷 {_short(agent.get('name'), 38)}", f"{_NAV_PREFIX}agent:{agent_id}"),))
        lines.append("Worker 按钮可查看 Agent 状态；不会把 Telegram 消息切入 Web Agent。")
    else:
        lines.append("\n尚未启用命名 Agents。Telegram 任务仍使用默认 Worker。")
    buttons.append((("🔄 查看会话和任务", f"{_NAV_PREFIX}sessions"),))
    return NavigationScreen("\n".join(lines), tuple(buttons))


def sessions_screen(settings: Any, queue: Any, operator_id: str, chat_id: str) -> NavigationScreen:
    # Telegram's current identity is channel+operator+physical chat. Other
    # Telegram groups and Web sessions are intentionally invisible here.
    key = session_identity("telegram", chat_id, operator_id)
    record = get_transcript_store(settings).get_session(key)
    jobs = _physical_jobs(queue, operator_id, chat_id, limit=15)
    title = _short(record.get("title") if record else "当前 Telegram 聊天", 64)
    lines = [
        "🔄 会话与任务",
        f"当前会话：{title}",
        f"最近任务：{len(jobs)} 条（只显示此 Telegram 聊天）",
        "下面可以切换查看任务详情；不会改变任务执行会话。",
    ]
    buttons: list[tuple[tuple[str, str], ...]] = []
    for job in jobs[:8]:
        job_id = str(job.get("id") or "")
        if not job_id:
            continue
        label = f"{_short(job_id, 14)} · {_short(job.get('state') or 'unknown', 16)}"
        buttons.append(((label, f"{_NAV_PREFIX}job:{_job_token(job_id)}"),))
    if not jobs:
        lines.append("\n还没有历史任务。发送 /run 或 /fix 即可创建。")
    lines.append("\n跨 Agent 的多会话执行与切换请在 Web Workbench 中操作。")
    buttons.append((("👷 我的 Workers", f"{_NAV_PREFIX}workers"),))
    return NavigationScreen("\n".join(lines), tuple(buttons))


def navigation_screen(
    action: str,
    settings: Any,
    queue: Any,
    operator_id: str,
    chat_id: str,
) -> NavigationScreen | None:
    """Resolve a callback only from the caller's current scoped state."""
    if action == "workers":
        return workers_screen(settings, queue, operator_id, chat_id)
    if action == "sessions":
        return sessions_screen(settings, queue, operator_id, chat_id)
    if action.startswith("job:"):
        token = action[4:]
        if len(token) != 16 or any(c not in "0123456789abcdef" for c in token):
            return None
        matched = next(
            (job for job in _physical_jobs(queue, operator_id, chat_id, limit=500)
             if _job_token(str(job.get("id") or "")) == token),
            None,
        )
        if matched is None:
            return None
        return NavigationScreen(
            "\n".join([
                f"📋 Job {_short(matched['id'], 60)}",
                f"状态：{_short(matched.get('state'), 40)}",
                f"模式：{_short(matched.get('mode'), 32)}",
                f"需求：{_short(matched.get('prompt_preview'), 180)}",
                "这是只读详情；如需取消或审查变更，请使用既有受保护命令。",
            ]),
            ((("⬅️ 返回会话列表", f"{_NAV_PREFIX}sessions"),),),
        )
    if action.startswith("agent:"):
        agent_id = action[6:]
        import agents
        if not agents.enabled(settings):
            return None
        agent = agents.AgentStore(settings).get(agent_id)
        if not agent or agent.get("archived"):
            return None
        # Deliberately exclude system instructions, local workspace path,
        # desktop number and model configuration from Telegram cards.
        return NavigationScreen(
            f"👷 {_short(agent.get('name'), 64)}\n"
            "这是 Web Agent。请在 Web Workbench 选择它来执行任务。"
            "\nTelegram 当前聊天仍使用原来的执行上下文。",
            ((("⬅️ 返回 Workers", f"{_NAV_PREFIX}workers"),),),
        )
    return None


def callback_action(data: str) -> str | None:
    """Strict namespace prevents intercepting tool/onboarding/relay buttons."""
    if not isinstance(data, str) or not data.startswith(_NAV_PREFIX) or len(data) > 64:
        return None
    action = data[len(_NAV_PREFIX):]
    if action in ("workers", "sessions"):
        return action
    if action.startswith("job:") and len(action) == 20 and all(
        c in "0123456789abcdef" for c in action[4:]
    ):
        return action
    if action.startswith("agent:") and 1 <= len(action[6:]) <= 32 and all(
        c.isascii() and (c.islower() or c.isdigit()) for c in action[6:]
    ):
        return action
    return None
