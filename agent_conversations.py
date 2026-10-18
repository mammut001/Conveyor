"""Explicit conversation selection, separate from transport/Agent authority.

Only verified owner private chats and the authenticated Web console can select.
A conversation joins history, never the other entry's transport credentials.
"""
from __future__ import annotations

from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps
import hashlib
import sqlite3
import time
import uuid
from pathlib import Path


_ACTIVE = ContextVar("conveyor_conversation", default=None)


def enabled(settings):
    import agent_channel_routing
    return agent_channel_routing.enabled(settings)


def _connect(settings):
    path = Path(settings.codex_memory_root) / 'state' / 'job_queue.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=10000')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS agent_conversations(
          id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
          origin_channel TEXT NOT NULL, origin_operator TEXT NOT NULL,
          origin_chat_id TEXT NOT NULL, transport_chat_id TEXT NOT NULL UNIQUE,
          history_key TEXT NOT NULL, title TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_conversation_selections(
          channel TEXT NOT NULL, chat_id TEXT NOT NULL, operator TEXT NOT NULL,
          agent_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
          PRIMARY KEY(channel,chat_id,operator,agent_id));
    ''')
    return db


def _owner(settings, channel):
    import agent_channel_routing
    return 'web-console' if channel == 'web' else agent_channel_routing._owner(settings, channel)


def _visible(settings, row, agent_id):
    return bool(row and row['agent_id'] == agent_id
                and row['origin_operator'] == _owner(settings, row['origin_channel']))


def _agent(settings, agent_id):
    import agents
    agent = agents.AgentStore(settings).get(agent_id)
    if not agent or agent['archived']:
        raise ValueError('会话不可用')
    return agent


def _entry_agent(settings, msg):
    import agents
    import agent_channel_routing
    if not enabled(settings):
        raise PermissionError('会话选择未开启')
    if msg.channel == 'web':
        if msg.operator_id != agents.WEB_OPERATOR:
            raise PermissionError('会话不可用')
        agent = agents.agent_for_chat(settings, msg.channel, msg.chat_id)
        if not agent or not msg.chat_id.startswith(agents.AGENT_CHAT_PREFIX):
            raise PermissionError('请在 Agent 工作区选择会话')
        return _agent(settings, agent['id'])
    if not agent_channel_routing.authorized(settings, msg):
        raise PermissionError('仅限本人私聊')
    return _agent(settings, agent_channel_routing.bound(settings, msg.channel, msg.chat_id) or 'default')


def _root(settings, agent_id, channel, chat_id, operator):
    """Create the independent root for an entry; legacy Web history stays intact."""
    import agents
    identity = '\0'.join((agent_id, channel, operator, chat_id))
    cid = hashlib.sha256(identity.encode()).hexdigest()[:32]
    is_web = channel == 'web'
    transport = chat_id if is_web else 'topic-' + cid
    key = ((f'web:{chat_id}' if agent_id == 'default'
            else f'agent:{agent_id}:{agents.session_id_for(agent_id)}') if is_web
           else (f'{channel}:{chat_id}' if agent_id == 'default'
                 else f'agent:{agent_id}:conversation:{cid}'))
    title = {'web': 'Web 会话（原有记录）', 'telegram': 'Telegram 独立会话',
             'feishu': '飞书独立会话'}[channel]
    db = _connect(settings)
    try:
        existing = db.execute('SELECT * FROM agent_conversations WHERE id=?', (cid,)).fetchone()
        if existing:
            return dict(existing)
        with db:
            db.execute('INSERT OR IGNORE INTO agent_conversations VALUES(?,?,?,?,?,?,?,?,?)',
                       (cid, agent_id, channel, operator, chat_id, transport, key, title, time.time()))
        return dict(db.execute('SELECT * FROM agent_conversations WHERE id=?', (cid,)).fetchone())
    finally:
        db.close()


def ensure_entry(settings, msg):
    agent = _entry_agent(settings, msg)
    root = _root(settings, agent['id'], msg.channel, msg.chat_id, msg.operator_id)
    db = _connect(settings)
    try:
        with db:
            db.execute('INSERT OR IGNORE INTO agent_conversation_selections VALUES(?,?,?,?,?)',
                       (msg.channel, msg.chat_id, msg.operator_id, agent['id'], root['id']))
            selected = db.execute('''SELECT c.* FROM agent_conversations c
                JOIN agent_conversation_selections s ON s.conversation_id=c.id
                WHERE s.channel=? AND s.chat_id=? AND s.operator=? AND s.agent_id=?''',
                (msg.channel, msg.chat_id, msg.operator_id, agent['id'])).fetchone()
            if not _visible(settings, selected, agent['id']):
                db.execute('''UPDATE agent_conversation_selections SET conversation_id=?
                    WHERE channel=? AND chat_id=? AND operator=? AND agent_id=?''',
                    (root['id'], msg.channel, msg.chat_id, msg.operator_id, agent['id']))
    finally:
        db.close()
    return root


def for_web_chat(settings, chat_id):
    if not enabled(settings) or not str(chat_id).startswith('topic-'):
        return None
    db = _connect(settings)
    try:
        row = db.execute('SELECT * FROM agent_conversations WHERE transport_chat_id=?', (chat_id,)).fetchone()
        if not row or not _visible(settings, row, row['agent_id']):
            return None
        try:
            _agent(settings, row['agent_id'])
        except ValueError:
            return None
        return dict(row)
    finally:
        db.close()


def current(settings, channel, chat_id, operator, agent_id):
    if not enabled(settings) or operator != _owner(settings, channel):
        return None
    snapshot = _ACTIVE.get()
    if snapshot and snapshot[:4] == (channel, chat_id, operator, agent_id):
        return snapshot[4] if _visible(settings, snapshot[4], agent_id) else None
    if channel == 'web':
        if chat_id.startswith('topic-'):
            row = for_web_chat(settings, chat_id)
            return row if row and row['agent_id'] == agent_id else None
        import agents
        if chat_id == agents.chat_id_for(agent_id):
            return _root(settings, agent_id, channel, chat_id, operator)
        return None
    db = _connect(settings)
    try:
        row = db.execute('''SELECT c.* FROM agent_conversations c
            JOIN agent_conversation_selections s ON s.conversation_id=c.id
            WHERE s.channel=? AND s.chat_id=? AND s.operator=? AND s.agent_id=?''',
                         (channel, chat_id, operator, agent_id)).fetchone()
        return dict(row) if _visible(settings, row, agent_id) else None
    finally:
        db.close()


def list_for_entry(settings, msg):
    import agents
    agent = _entry_agent(settings, msg)
    ensure_entry(settings, msg)
    _root(settings, agent['id'], 'web', agents.chat_id_for(agent['id']), agents.WEB_OPERATOR)
    db = _connect(settings)
    try:
        rows = db.execute('SELECT * FROM agent_conversations WHERE agent_id=? ORDER BY created_at DESC',
                          (agent['id'],)).fetchall()
        return [dict(row) for row in rows if _visible(settings, row, agent['id'])]
    finally:
        db.close()


def select(settings, msg, reference):
    agent = _entry_agent(settings, msg)
    rows = list_for_entry(settings, msg)
    if reference == 'local':
        target = ensure_entry(settings, msg)
    else:
        # A short code is a selector, never an authority or arbitrary session ID.
        if not isinstance(reference, str) or len(reference) < 8 or len(reference) > 32:
            raise ValueError('会话不可用，请重新打开会话列表')
        matches = [row for row in rows if row['id'].startswith(reference)]
        if len(matches) != 1:
            raise ValueError('会话不可用，请重新打开会话列表')
        target = matches[0]
    db = _connect(settings)
    try:
        with db:
            db.execute('''INSERT INTO agent_conversation_selections VALUES(?,?,?,?,?)
                ON CONFLICT(channel,chat_id,operator,agent_id) DO UPDATE SET
                conversation_id=excluded.conversation_id''',
                       (msg.channel, msg.chat_id, msg.operator_id, agent['id'], target['id']))
    finally:
        db.close()
    return target


def new(settings, msg, title='新话题'):
    from redaction import redact_text, truncate
    agent = _entry_agent(settings, msg)
    cid = uuid.uuid4().hex
    db = _connect(settings)
    try:
        with db:
            db.execute('INSERT INTO agent_conversations VALUES(?,?,?,?,?,?,?,?,?)',
                       (cid, agent['id'], msg.channel, msg.operator_id, msg.chat_id,
                        'topic-' + cid, f"agent:{agent['id']}:conversation:{cid}",
                        truncate(redact_text(title.strip() or '新话题'), 80), time.time()))
    finally:
        db.close()
    return select(settings, msg, cid)


def transcript_id(row):
    from transcript_store import session_identity
    return session_identity('web', row['transport_chat_id'], 'web-console')


def summary(settings, msg):
    agent = _entry_agent(settings, msg)
    ensure_entry(settings, msg)
    row = current(settings, msg.channel, msg.chat_id, msg.operator_id, agent['id'])
    return row


async def maybe_command(settings, msg, port):
    import agent_channel_routing
    if not enabled(settings) or msg.channel not in ('telegram', 'feishu'):
        return False
    raw = msg.text.strip()
    if msg.reply_to is None and not msg.attachments:
        raw = {'接着已有会话聊': '/conversations', '查看会话': '/conversations',
               '回到本入口会话': '/conversation local'}.get(raw.rstrip('。？！?!'), raw)
    parts = raw.split(maxsplit=2)
    word = (parts[0] if parts else '').split('@', 1)[0].lower()
    if word not in ('/conversation', '/conversations'):
        return False
    if not agent_channel_routing.authorized(settings, msg):
        return True
    try:
        if word == '/conversations' or len(parts) == 1:
            selected = summary(settings, msg)
            lines = ['接着已有会话聊', '只显示当前 Agent 的会话；选择后接续上下文。', '']
            for row in list_for_entry(settings, msg):
                marker = '●' if row['id'] == selected['id'] else '○'
                lines += [f"{marker} {row['title']}", f"/conversation {row['id'][:12]}"]
            lines += ['', '回到本入口：/conversation local', '另开话题：/conversation new <标题>']
            await port.reply(msg, '\n'.join(lines))
        else:
            row = (new(settings, msg, parts[2] if len(parts) > 2 else '新话题')
                   if parts[1] == 'new' else select(settings, msg, parts[1]))
            await port.reply(msg, f"已接续：{row['title']}\n后续消息使用这段历史；权限仍由当前入口校验。")
    except (ValueError, PermissionError) as exc:
        await port.reply(msg, str(exc))
    return True


def scoped(fn):
    """Pin conversation selection for one inbound request across awaits/tasks."""
    @wraps(fn)
    async def wrapped(msg, port, settings, *args, **kwargs):
        import agent_channel_routing as routing
        controls = {'接着已有会话聊', '查看会话', '回到本入口会话'}
        word = (msg.text.strip().split(maxsplit=1) or [''])[0].split('@', 1)[0].lower()
        if (not enabled(settings) or routing.is_agent_command(settings, msg)
                or word in ('/conversation', '/conversations')
                or msg.text.strip().rstrip('。？！?!') in controls):
            return await fn(msg, port, settings, *args, **kwargs)
        if routing.authorized(settings, msg):
            ensure_entry(settings, msg)
        import agents
        agent = agents.agent_for_chat(settings, msg.channel, msg.chat_id)
        row = current(settings, msg.channel, msg.chat_id, msg.operator_id, agent['id']) if agent else None
        if not row:
            return await fn(msg, port, settings, *args, **kwargs)
        token = _ACTIVE.set((msg.channel, msg.chat_id, msg.operator_id, agent['id'], row))
        try:
            return await fn(msg, port, settings, *args, **kwargs)
        finally:
            _ACTIVE.reset(token)
    return wrapped


def recent_history(settings, row, limit):
    """Rebuild a bounded context from the explicitly selected durable transcript."""
    if limit <= 0:
        return []
    from transcript_store import get_transcript_store
    session = get_transcript_store(settings).get_session(transcript_id(row)) or {}
    turns = []
    if not session.get('messages') and legacy_entry_root(row):
        # Default-Agent legacy JSONL is already scoped to this exact entry.
        # Unlike the mixed physical transcript it does not contain other Agents.
        import json
        from handlers.session import _safe_filename
        path = Path(settings.codex_memory_root) / 'session' / _safe_filename(
            row['origin_channel'], row['origin_chat_id'], row['origin_operator'])
        try:
            for line in path.read_text(encoding='utf-8').splitlines()[-min(limit, 20):]:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                turns += [{'role': 'user', 'content': str(record.get('user') or '')[:4000]},
                          {'role': 'assistant', 'content': str(record.get('assistant') or '')[:4000]}]
        except (OSError, ValueError, TypeError):
            pass
    for message in session.get('messages', []):
        if message.get('kind') == 'context_reset':
            turns = []
        elif (message.get('role') in ('user', 'assistant')
              and message.get('kind') in ('chat', 'codex')):
            turns.append({'role': message['role'], 'content': str(message.get('content') or '')[:4000]})
    return turns[-2 * min(limit, 20):]


def refinement_id(settings, msg):
    """Task worktrees stay transport-scoped even when chat history is continued."""
    from refinement_store import stable_session_id
    import agents
    base = stable_session_id(msg.channel, msg.chat_id, msg.operator_id)
    if not enabled(settings):
        return base
    agent = agents.agent_for_chat(settings, msg.channel, msg.chat_id)
    row = current(settings, msg.channel, msg.chat_id, msg.operator_id, agent['id']) if agent else None
    return base + ':conversation:' + row['id'] if row else base


def execution_record(settings, msg, cid):
    import agents
    if not enabled(settings) or msg.operator_id != _owner(settings, msg.channel):
        raise PermissionError('会话归属已改变，任务未执行')
    agent = agents.agent_for_chat(settings, msg.channel, msg.chat_id)
    db = _connect(settings)
    try:
        row = db.execute('SELECT * FROM agent_conversations WHERE id=?', (cid,)).fetchone()
        if (not agent or not _visible(settings, row, agent['id'])
                or (msg.channel == 'web' and row['transport_chat_id'] != msg.chat_id)):
            raise PermissionError('会话归属已改变，任务未执行')
        _agent(settings, agent['id'])
        return dict(row)
    finally:
        db.close()


@contextmanager
def pin(settings, msg, row):
    token = _ACTIVE.set((msg.channel, msg.chat_id, msg.operator_id, row['agent_id'], row))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def scoped_job(fn):
    @wraps(fn)
    async def wrapped(msg, port, runner, *args, conversation_id='', conversation_context=None, **kwargs):
        if not conversation_id:
            return await fn(msg, port, runner, *args, **kwargs)
        import agent_context
        try:
            row = execution_record(runner.settings, msg, conversation_id)
            if not isinstance(conversation_context, dict) or not conversation_context:
                raise PermissionError('queued context missing')
            saved = dict(conversation_context)
            saved['tool_capabilities'] = frozenset(saved['tool_capabilities'])
            ctx = agent_context.validated(runner.settings, agent_context.AgentContext(**saved),
                                          msg.channel, msg.chat_id, msg.operator_id)
        except (ValueError, PermissionError, KeyError, TypeError):
            queue_id = kwargs.get('queue_job_id')
            if queue_id:
                from handlers.job_queue import get_job_queue
                await get_job_queue().mark_running_failed('conversation identity denied', queue_id)
            await port.reply(msg, '会话归属已改变，任务未执行。请在目标会话重新提交。')
            return
        with agent_context.bind(ctx), pin(runner.settings, msg, row):
            return await fn(msg, port, runner, *args, **kwargs)
    return wrapped


def legacy_entry_root(row):
    return (row['agent_id'] == 'default' and row['origin_channel'] in ('telegram', 'feishu')
            and row['history_key'] == f"{row['origin_channel']}:{row['origin_chat_id']}")
