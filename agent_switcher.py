"""Owner-private, channel-neutral Agent/topic buttons with bound one-use selectors."""
from __future__ import annotations

import secrets
import time

import agent_channel_routing as routing
import agent_conversations as conversations
from agents import AgentStore
from redaction import redact_text, truncate

PAGE_SIZE = 8
TTL = 15 * 60


def available(msg, port):
    return (getattr(port, 'supports_inline_buttons', False) is True
            or (msg.channel == 'feishu' and callable(getattr(port, 'send_card', None))))


def _connect(settings):
    db = conversations._connect(settings)
    db.execute('''CREATE TABLE IF NOT EXISTS agent_switcher_actions(
        token TEXT PRIMARY KEY, channel TEXT NOT NULL, chat_id TEXT NOT NULL,
        operator TEXT NOT NULL, agent_id TEXT NOT NULL, action TEXT NOT NULL,
        argument TEXT NOT NULL, expires REAL NOT NULL)''')
    return db


def _issue(settings, msg, agent_id, action, argument=''):
    token = secrets.token_hex(16)
    db = _connect(settings)
    try:
        with db:
            db.execute('DELETE FROM agent_switcher_actions WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO agent_switcher_actions VALUES(?,?,?,?,?,?,?,?)',
                       (token, msg.channel, msg.chat_id, msg.operator_id, agent_id,
                        action, str(argument), time.time() + TTL))
    finally:
        db.close()
    return 'acs:' + token


def _label(value):
    return truncate(redact_text(str(value).replace('\n', ' ')), 45)


async def show(settings, msg, port, screen='home', page=0, notice=''):
    if not routing.authorized(settings, msg):
        return
    agent = conversations._entry_agent(settings, msg)
    current = conversations.summary(settings, msg)
    origin = {'web': 'Web', 'telegram': 'Telegram', 'feishu': '飞书'}
    text = '\n'.join(filter(None, [notice, f"当前 Agent：{_label(agent['name'])}",
        f"当前会话：{_label(current['title'])} · {origin.get(current['origin_channel'], '其他入口')}",
        '切换 Agent 不搬移历史；接续会话才使用所选上下文。']))
    buttons = []
    fallback_choices = []
    def button(label, action, argument=''):
        return {'text': label, 'callback_data': _issue(settings, msg, agent['id'], action, argument)}
    if screen in ('agents', 'topics'):
        rows = ([row for row in AgentStore(settings).list() if not row['archived']]
                if screen == 'agents' else conversations.list_for_entry(settings, msg))
        page = max(0, min(page, max(0, (len(rows)-1)//PAGE_SIZE)))
        text += '\n\n' + ('选择 Agent' if screen == 'agents' else '选择要接着聊的会话')
        text += f' · 第 {page+1}/{max(1, (len(rows)+PAGE_SIZE-1)//PAGE_SIZE)} 页'
        for row in rows[page*PAGE_SIZE:(page+1)*PAGE_SIZE]:
            selected = row['id'] == (agent['id'] if screen == 'agents' else current['id'])
            label = _label(row['name'] if screen == 'agents' else row['title'])
            if screen == 'topics':
                label = origin.get(row['origin_channel'], '其他入口') + ' · ' + label
            command = '/agent ' + row['id'] if screen == 'agents' else '/conversation ' + row['id'][:12]
            fallback_choices.append(label + '：' + command)
            buttons.append([button(('✓ ' if selected else '') + label,
                                   'agent' if screen == 'agents' else 'topic', row['id'])])
        nav = []
        if page: nav.append(button('上一页', screen, page-1))
        if (page+1)*PAGE_SIZE < len(rows): nav.append(button('下一页', screen, page+1))
        if nav: buttons.append(nav)
    buttons.append([button('切换 Agent', 'agents'), button('接续会话', 'topics')])
    buttons.append([button('另开新话题', 'new'), button('回到本入口', 'local')])
    text += '\n\n文字入口：/agents · /conversations；自定义标题：/conversation new 标题'
    fallback = text + '\n' + '\n'.join(fallback_choices) + '\n回到本入口：/conversation local；新话题：/conversation new 标题'
    if msg.channel == 'feishu':
        elements = [{'tag': 'markdown', 'content': text}]
        for row in buttons:
            elements.append({'tag': 'action', 'actions': [
                {'tag': 'button', 'text': {'tag': 'plain_text', 'content': b['text']},
                 'type': 'default', 'value': {'action': 'agent_switcher', 'token': b['callback_data']}}
                for b in row]})
        result = await port.send_card(msg, {'config': {'wide_screen_mode': True, 'update_multi': True},
            'header': {'title': {'tag': 'plain_text', 'content': 'Agent 与会话'}, 'template': 'turquoise'},
            'elements': elements})
        if result is None:
            await port.reply(msg, fallback)
    else:
        try:
            await port.reply_with_buttons(msg, text, buttons)
        except Exception:
            await port.reply(msg, fallback)


async def handle(settings, msg, port, data):
    # The token was minted only from a verified private entry. Matching the
    # physical chat prevents forwarded Feishu cards (whose events lack chat_type)
    # from granting private-chat authority in a group or another entry.
    if not routing.authorized(settings, msg):
        return
    token = data[4:] if isinstance(data, str) and data.startswith('acs:') else ''
    if len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
        await port.reply(msg, '按钮无效，请重新发送 /agents 或 /conversations。')
        return
    db = _connect(settings)
    try:
        with db:
            row = db.execute('SELECT * FROM agent_switcher_actions WHERE token=?', (token,)).fetchone()
            if (not row or row['expires'] < time.time() or
                    (row['channel'], row['chat_id'], row['operator']) !=
                    (msg.channel, msg.chat_id, msg.operator_id)):
                row = None
            elif db.execute('DELETE FROM agent_switcher_actions WHERE token=?', (token,)).rowcount != 1:
                row = None
    finally:
        db.close()
    if row is None:
        await port.reply(msg, '按钮已失效或已处理，请重新发送 /agents 或 /conversations。')
        return
    action, argument = row['action'], row['argument']
    agent = conversations._entry_agent(settings, msg)
    if action != 'agent' and row['agent_id'] != agent['id']:
        await show(settings, msg, port, notice='Agent 已改变，请在当前会话重新选择。')
        return
    try:
        if action == 'agent':
            target = AgentStore(settings).get(argument)
            if not target or target['archived']: raise ValueError('Agent 已不可用')
            routing.bind(settings, msg, argument)
            await show(settings, msg, port, notice='已切换 Agent。后续消息发往下方会话。')
        elif action == 'topic':
            conversations.select(settings, msg, argument)
            await show(settings, msg, port, notice='已接续会话。后续消息使用这段历史。')
        elif action == 'new':
            conversations.new(settings, msg, '新话题')
            await show(settings, msg, port, notice='已另开新话题，可以直接开始聊。')
        elif action == 'local':
            conversations.select(settings, msg, 'local')
            await show(settings, msg, port, notice='已回到当前入口的独立会话。')
        elif action in ('agents', 'topics'):
            await show(settings, msg, port, action, int(argument or 0))
    except (ValueError, PermissionError):
        await show(settings, msg, port, notice='该条目已不可用，请重新选择。')
