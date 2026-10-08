"""Conversation-scoped Telegram task controls, independent of runner lanes."""
from __future__ import annotations

from pathlib import Path

from redaction import redact_text, truncate

_COMMANDS = frozenset({'status', 'last', 'jobs', 'cancel', 'diff', 'apply', 'discard'})


def _scoped(msg, settings) -> bool:
    """Telegram conversations, and a Workers-selected canonical web session."""
    if msg.channel == 'telegram':
        return getattr(settings, 'agents_enabled', False) is True or ':topic:' in msg.chat_id
    return (
        msg.channel == 'web'
        and msg.operator_id == 'web-console'
        and getattr(settings, 'agents_enabled', False) is True
        and str(msg.chat_id).startswith('agent-')
    )


async def handle_conversation_command(name, msg, port, runner, settings, arg) -> bool:
    if name not in _COMMANDS or not _scoped(msg, settings):
        return False
    from handlers.job_queue import get_job_queue
    from refinement_store import RefinementStore, stable_session_id
    from web_control import WebControl

    queue = get_job_queue()
    jobs = queue.conversation_jobs(msg.channel, msg.chat_id, msg.operator_id)
    if name == 'jobs':
        try:
            limit = max(1, min(30, int(arg or 8)))
        except ValueError:
            limit = 8
        text = '\n'.join(f"{j['id']} · {j['state']} · {j['prompt_preview'][:100]}" for j in jobs[:limit])
        await port.reply(msg, text or '此 Agent 对话还没有任务。')
        return True
    if not jobs:
        await port.reply(msg, '此 Agent 对话还没有任务。其他项目的任务不会在这里操作。')
        return True
    control = WebControl(settings, runner, queue)
    if name == 'cancel':
        # Running work is the primary stop target; otherwise cancel the oldest
        # queued request in this conversation. Never touch another lane/chat.
        job = next((j for j in jobs if j['state'] == 'running'), None)
        if job is None:
            job = next((j for j in reversed(jobs) if j['state'] == 'queued'), None)
        if job is None:
            await port.reply(msg, '此对话没有正在运行或排队的任务。')
        else:
            _, text = await control.cancel_job(job['id'])
            await port.reply(msg, text)
        return True
    job = jobs[0]
    if name == 'status':
        active = next((j for j in jobs if j['state'] == 'running'), None)
        job = active or next((j for j in reversed(jobs) if j['state'] == 'queued'), None) or job
        runtime = control._runtime_metadata(job) or {}
        lines = [f"任务：{job['id']}", f"状态：{job['state']}", f"需求：{job['prompt_preview']}"]
        if runtime.get('last_event'):
            lines.append(f"进度：{truncate(str(runtime['last_event']), 1000)}")
        await port.reply(msg, redact_text('\n'.join(lines)))
        return True
    if name == 'last':
        # Read the durable result of this conversation, even after a restart.
        job = next((j for j in jobs if j['state'] not in ('queued', 'running')), job)
        runtime_id = str((job.get('metadata') or {}).get('runtime_job_id') or '')
        text = ''
        if runtime_id and all(c.isalnum() or c in '-_' for c in runtime_id):
            folder = Path(settings.codex_task_root) / 'logs' / runtime_id
            files = sorted(folder.glob('attempt-*-final.txt'), key=lambda p: p.stat().st_mtime)
            if files:
                text = files[-1].read_text(encoding='utf-8', errors='replace')
        await port.reply(msg, truncate(redact_text(text or f"{job['id']} · {job['state']}"), 3900))
        return True
    chain = RefinementStore(settings).active(stable_session_id(msg.channel, msg.chat_id, msg.operator_id))
    if chain:
        chain_job = queue.job_snapshot(str(chain.get('latest_queue_job_id') or chain.get('root_queue_job_id') or ''))
        if chain_job and (chain_job['channel'], chain_job['chat_id'], str(chain_job['operator_id'])) == (msg.channel, msg.chat_id, msg.operator_id):
            job = chain_job
    worktree = control._worktree(job)
    if name in ('apply', 'discard') and any(j['state'] in ('queued', 'running') for j in jobs):
        await port.reply(msg, '此对话还有排队或运行中的任务，请完成或取消后再应用/丢弃改动。')
        return True
    if name == 'diff':
        text = await runner.diff_job(job['id'], worktree)
    elif name == 'apply':
        text = await runner.apply_job(job['id'], worktree)
    else:
        text = await runner.discard_job(job['id'], worktree)
    await port.reply(msg, truncate(redact_text(text), 3900))
    return True
