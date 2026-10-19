"""Authenticated conversation picker; transport identity remains Web-only."""
from http import HTTPStatus
from urllib.parse import urlparse

from channel.types import InboundMessage
import agent_conversations as conversations


def _entry(handler, agent_id):
    import agents
    settings = getattr(handler.server.control, 'settings', None)
    if not conversations.enabled(settings):
        raise LookupError('not found')
    agent = agents.AgentStore(settings).get(agent_id)
    if not agent or agent['archived']:
        raise LookupError('not found')
    return settings, InboundMessage(channel='web', operator_id=agents.WEB_OPERATOR,
        chat_id=agents.chat_id_for(agent_id), message_id=None, text='', chat_type='p2p')


def _public(row):
    return {'id': row['id'], 'title': row['title'], 'origin_channel': row['origin_channel'],
            'session_id': conversations.transcript_id(row)}


def get(handler, query):
    try:
        settings, msg = _entry(handler, (query.get('agent_id') or [''])[0])
        rows = conversations.list_for_entry(settings, msg)
        handler._json(HTTPStatus.OK, {'conversations': [_public(row) for row in rows]})
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
        handler._json(HTTPStatus.CREATED, {'conversation': _public(row)})
    except (ValueError, PermissionError, LookupError):
        handler._json(HTTPStatus.NOT_FOUND, {'error': 'conversation unavailable'})
