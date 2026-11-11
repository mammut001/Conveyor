"""Authenticated conversation picker; transport identity remains Web-only."""
from datetime import datetime, timezone
from http import HTTPStatus
from urllib.parse import urlparse

from channel.types import InboundMessage
from conveyor.agents import agent_conversations as conversations
from conveyor.core.transcript_store import get_transcript_store


def _entry(handler, agent_id):
    from conveyor.agents import agents
    settings = getattr(handler.server.control, 'settings', None)
    if not conversations.enabled(settings):
        raise LookupError('not found')
    agent = agents.AgentStore(settings).get(agent_id)
    if not agent or agent['archived']:
        raise LookupError('not found')
    return settings, InboundMessage(channel='web', operator_id=agents.WEB_OPERATOR,
        chat_id=agents.chat_id_for(agent_id), message_id=None, text='', chat_type='p2p')


def _public(settings, row):
    # UTC ISO 8601 times like the transcript's; last_activity is the newest
    # message, or created_at while the conversation is still empty.
    session_id = conversations.transcript_id(row)
    created = datetime.fromtimestamp(row['created_at'], timezone.utc).isoformat(
        timespec='microseconds').replace('+00:00', 'Z')
    last = get_transcript_store(settings).last_message(session_id)
    return {'id': row['id'], 'title': row['title'], 'origin_channel': row['origin_channel'],
            'session_id': session_id, 'created_at': created,
            'last_activity': (last or {}).get('created_at') or created}


def get(handler, query):
    try:
        settings, msg = _entry(handler, (query.get('agent_id') or [''])[0])
        rows = conversations.list_for_entry(settings, msg)
        handler._json(HTTPStatus.OK, {'conversations': [_public(settings, row) for row in rows]})
    except (ValueError, PermissionError, LookupError):
        handler._json(HTTPStatus.NOT_FOUND, {'error': 'conversation unavailable'})


def post(handler, body):
    origin = handler.headers.get('Origin', '')
    host = handler.headers.get('Host', '')
    if (handler.headers.get('Sec-Fetch-Site', '') in ('cross-site', 'same-site')
            or (origin and urlparse(origin).hostname != host.split(':')[0])
            or handler.headers.get('X-Requested-With') != 'XMLHttpRequest'
            or 'application/json' not in handler.headers.get('Content-Type', '').lower()):
        handler._json(HTTPStatus.FORBIDDEN, {'error': 'origin or request denied'})
        return
    try:
        if not isinstance(body.get('agent_id'), str) or not isinstance(body.get('title', ''), str):
            raise ValueError('invalid request')
        settings, msg = _entry(handler, body['agent_id'])
        row = conversations.new(settings, msg, body.get('title') or 'Web 新话题')
        handler._json(HTTPStatus.CREATED, {'conversation': _public(settings, row)})
    except (ValueError, PermissionError, LookupError):
        handler._json(HTTPStatus.NOT_FOUND, {'error': 'conversation unavailable'})
