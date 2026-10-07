"""Telegram agent/project selection, scoped to the current chat or forum topic."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from agents import AgentError, AgentStore, agent_for_chat, conversation_for_chat, enabled
from channel.telegram_identity import TelegramAddress, context_tag

_PAGE_SIZE = 6


def _source(msg) -> str:
    return TelegramAddress.parse(msg.chat_id).source


def _resolve(store: AgentStore, query: str) -> dict:
    agent = store.get(query)
    if agent and not agent['archived']:
        return agent
    matches = [a for a in store.list() if a['name'].casefold() == query.casefold()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AgentError("多个 Agent 使用这个名称，请用 /agent list 中的 ID 选择。")
    raise AgentError("没有找到这个 Agent，请用 /agent list 查看名称和 ID。")


async def _project_path(value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise AgentError("项目目录必须是服务器上已存在的 Git 仓库绝对路径。")
    try:
        proc = await asyncio.create_subprocess_exec(
            'git', '-C', str(path), 'rev-parse', '--show-toplevel',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise AgentError("检查项目目录超时，请重试。")
        if proc.returncode != 0 or Path(stdout.decode().strip()).resolve() != path.resolve():
            raise AgentError("请选择 Git 仓库根目录。")
    except OSError as exc:
        raise AgentError("无法检查服务器上的 Git 仓库。") from exc
    return str(path.resolve())


async def _picker(msg, port, settings, page: int = 0) -> None:
    store = AgentStore(settings)
    all_agents = store.list()
    pages = max(1, (len(all_agents) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    source = _source(msg)
    current_id = store.bound_agent_id('telegram', source) or 'default'
    current = store.get(current_id)
    title = current['name'] if current and not current['archived'] else '已归档'
    topic = TelegramAddress.parse(source).topic_id
    lines = [f"当前 Agent：{title}", f"范围：{'当前话题 #' + str(topic) if topic else '当前聊天'}", f"选择项目 Agent（{page + 1}/{pages}）："]
    buttons = []
    tag = context_tag(source)
    for agent in all_agents[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
        marker = '✓ ' if agent['id'] == current_id else ''
        lines.append(f"{marker}{agent['name']} · {agent['id']}\n项目：{agent['workspace_path'] or settings.codex_workspace_root}")
        buttons.append([{'text': f"{marker}{agent['name']}", 'callback_data': f"agent:select:{agent['id']}:{tag}"}])
    navigation = []
    if page:
        navigation.append({'text': '上一页', 'callback_data': f"agent:page:{page - 1}:{tag}"})
    if page + 1 < pages:
        navigation.append({'text': '下一页', 'callback_data': f"agent:page:{page + 1}:{tag}"})
    if navigation:
        buttons.append(navigation)
    buttons.append([{'text': '返回默认对话', 'callback_data': f"agent:reset:default:{tag}"}])
    lines.append("/agent <名称或 ID> 切换；/agent new 名称 | /绝对项目路径 创建。切回原 Agent 可继续原对话。")
    # Long paths must not hide a project's ID. Send details in bounded chunks,
    # then put the picker beneath the last chunk.
    chunks, text = [], ''
    for line in lines:
        if text and len(text) + len(line) + 1 > 3000:
            chunks.append(text)
            text = ''
        text = f"{text}\n{line}" if text else line
    chunks.append(text)
    for chunk in chunks[:-1]:
        await port.reply(msg, chunk)
    if getattr(port, 'supports_inline_buttons', False) is True:
        await port.reply_with_buttons(msg, chunks[-1], buttons)
    else:
        await port.reply(msg, chunks[-1])


async def handle_agent_command(msg, port, runner, settings, arg: str) -> None:
    if not enabled(settings):
        await port.reply(msg, "Agent 功能未开启：设置 CONVEYOR_AGENTS_ENABLED=true 后重启服务。")
        return
    if msg.channel != 'telegram':
        await port.reply(msg, "Web 中可直接选择 Agent；此命令用于 Telegram 聊天或话题。")
        return
    try:
        source = _source(msg)
        store = AgentStore(settings)
        if not arg or arg == 'list' or arg.startswith('list '):
            try:
                page = int(arg[5:]) - 1 if arg.startswith('list ') else 0
            except ValueError:
                raise AgentError("用法：/agent list [页码]")
            await _picker(replace(msg, chat_id=source), port, settings, page)
            return
        if arg == 'reset':
            store.bind_chat('telegram', source, None)
            await port.reply(msg, "已返回默认对话，默认会话原有历史仍保留。已绑定 Agent 的历史、任务和改动也保留，选择它即可继续。")
            return
        if arg.startswith('new '):
            name, sep, path = arg[4:].partition('|')
            payload = {'name': name.strip()}
            if sep:
                payload['workspace_path'] = await _project_path(path.strip())
            selected = store.create(payload)
        else:
            selected = _resolve(store, arg)
        try:
            store.bind_chat('telegram', source, selected['id'])
        except AgentError as exc:
            if arg.startswith('new '):
                raise AgentError(f"已创建 {selected['name']} ({selected['id']})，尚未切换：{exc}") from exc
            raise
        routed = conversation_for_chat(settings, 'telegram', source)
        agent = agent_for_chat(settings, 'telegram', routed)
        await port.reply(msg, (
            f"已切换到 {agent['name']} ({agent['id']})\n"
            f"项目：{agent['workspace_path'] or settings.codex_workspace_root}\n"
            "该 Agent 的对话、任务和审批独立保留；切回即可继续。\n"
            "发送 /status 查看此对话的任务；/agent 选择其他项目。"
        ))
    except (AgentError, ValueError) as exc:
        await port.reply(msg, str(exc))


async def handle_agent_callback(msg, port, runner, settings, data: str) -> None:
    """Called only after the adapter's operator allowlist gate."""
    parts = data.split(':')
    if len(parts) != 4 or parts[0] != 'agent':
        return
    try:
        source = _source(msg)
    except ValueError:
        return
    if parts[3] != context_tag(source):
        await port.reply(msg, "此选择按钮属于其他聊天或话题，请在这里发送 /agent。")
        return
    if parts[1] == 'select':
        await handle_agent_command(msg, port, runner, settings, parts[2])
    elif parts[1] == 'reset':
        await handle_agent_command(msg, port, runner, settings, 'reset')
    elif parts[1] == 'page' and parts[2].isdigit():
        await handle_agent_command(msg, port, runner, settings, f"list {int(parts[2]) + 1}")
