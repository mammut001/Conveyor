"""Exercise real selection from transport buttons, including stale and forwarded cards."""
import os
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_switcher as ui
import agent_conversations as conversations
import agent_channel_routing as routing
from agents import AgentStore
from channel.types import InboundMessage
from channel.feishu_cards import parse_action, extract_card_action
from tests.test_agent_channel_routing import settings_for


class Port:
    supports_inline_buttons = True
    def __init__(self): self.text=''; self.buttons=[]; self.cards=[]
    async def reply(self,msg,text): self.text=text
    async def reply_with_buttons(self,msg,text,buttons): self.text=text; self.buttons=buttons
    async def send_card(self,msg,card):
        self.cards.append(card)
        self.text=card['elements'][0]['content']
        self.buttons=[[{'text': b['text']['content'], 'callback_data': b['value']['token']}
                       for b in row['actions']] for row in card['elements'][1:]]
        return 'card-id'
    def token(self, label):
        return next(b['callback_data'] for row in self.buttons for b in row if label in b['text'])


class SwitcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.settings=settings_for(Path(tmp.name))
        flags=patch.dict(os.environ, {'CONVEYOR_NAMED_AGENTS':'1', 'CONVEYOR_AGENT_CONTEXT':'1',
                                     'CONVEYOR_AGENT_CHANNEL_ROUTING':'1'})
        flags.start(); self.addCleanup(flags.stop)
        self.store=AgentStore(self.settings)
        self.a=self.store.create({'name':'Writer','tools_allowlist':[]})
        self.b=self.store.create({'name':'Reviewer','tools_allowlist':[]})
        self.tg=InboundMessage(channel='telegram',operator_id='1',chat_id='1',message_id='1',text='/agents',chat_type='p2p')
        self.fs=replace(self.tg,channel='feishu',operator_id='ou_owner',chat_id='oc_private')
        self.port=Port()

    async def test_both_transports_switch_agents_and_continue_only_selected_history(self):
        for msg in (self.tg,self.fs):
            with self.subTest(channel=msg.channel):
                await routing.maybe_command(self.settings,msg,self.port)
                await ui.handle(self.settings,msg,self.port,self.port.token('Writer'))
                self.assertEqual(routing.bound(self.settings,msg.channel,msg.chat_id),self.a['id'])
                self.assertIn('当前 Agent：Writer',self.port.text)
                local=conversations.summary(self.settings,msg)
                await ui.handle(self.settings,msg,self.port,self.port.token('另开新话题'))
                topic=conversations.summary(self.settings,msg)
                self.assertNotEqual(local['id'],topic['id'])
                await ui.handle(self.settings,msg,self.port,self.port.token('回到本入口'))
                self.assertEqual(conversations.summary(self.settings,msg)['id'],local['id'])
                await ui.handle(self.settings,msg,self.port,self.port.token('接续会话'))
                await ui.handle(self.settings,msg,self.port,self.port.token('新话题'))
                self.assertEqual(conversations.summary(self.settings,msg)['id'],topic['id'])

    async def test_forwarded_foreign_operator_and_group_callbacks_cannot_switch(self):
        await ui.show(self.settings,self.fs,self.port,'agents')
        token=self.port.token('Writer')
        for msg in (replace(self.fs,chat_id='oc_group'),replace(self.fs,operator_id='other'),
                    replace(self.fs,chat_type='group'),self.tg):
            await ui.handle(self.settings,msg,self.port,token)
            self.assertIsNone(routing.bound(self.settings,msg.channel,msg.chat_id))
        # Rejected attempts must not consume the original owner's selector.
        await ui.handle(self.settings,self.fs,self.port,token)
        self.assertEqual(routing.bound(self.settings,'feishu',self.fs.chat_id),self.a['id'])

    async def test_stale_topic_card_after_agent_switch_does_not_create_in_new_agent(self):
        routing.bind(self.settings,self.tg,self.a['id'])
        await ui.show(self.settings,self.tg,self.port)
        token=self.port.token('另开新话题')
        routing.bind(self.settings,self.tg,self.b['id'])
        before=conversations.summary(self.settings,self.tg)
        await ui.handle(self.settings,self.tg,self.port,token)
        self.assertEqual(conversations.summary(self.settings,self.tg)['id'],before['id'])
        self.assertIn('Agent 已改变',self.port.text)

    async def test_new_topic_is_once_only_across_retries_and_tokens_expire(self):
        await ui.show(self.settings,self.tg,self.port)
        token=self.port.token('另开新话题')
        await ui.handle(self.settings,self.tg,self.port,token)
        selected=conversations.summary(self.settings,self.tg)
        await ui.handle(self.settings,self.tg,self.port,token)
        self.assertEqual(conversations.summary(self.settings,self.tg)['id'],selected['id'])
        self.assertIn('已失效或已处理',self.port.text)
        await ui.show(self.settings,self.tg,self.port,'agents')
        token=self.port.token('Writer')
        with patch('agent_switcher.time.time',return_value=10**12):
            await ui.handle(self.settings,self.tg,self.port,token)
        self.assertIsNone(routing.bound(self.settings,'telegram','1'))

    async def test_pagination_and_callback_limits(self):
        for i in range(10): self.store.create({'name':f'Agent {i}','tools_allowlist':[]})
        await ui.show(self.settings,self.tg,self.port,'agents')
        first=self.port.text
        await ui.handle(self.settings,self.tg,self.port,self.port.token('下一页'))
        self.assertNotEqual(first,self.port.text)
        self.assertIn('第 2/',self.port.text)
        self.assertTrue(all(len(b['callback_data'].encode())<=64 for row in self.port.buttons for b in row))

    async def test_archived_target_and_disabled_feature_fail_closed(self):
        await ui.show(self.settings,self.tg,self.port,'agents')
        token=self.port.token('Writer')
        self.store.archive(self.a['id'])
        await ui.handle(self.settings,self.tg,self.port,token)
        self.assertIsNone(routing.bound(self.settings,'telegram','1'))
        self.assertIn('已不可用',self.port.text)
        await ui.show(self.settings,self.tg,self.port,'agents')
        token=self.port.token('Reviewer')
        with patch.dict(os.environ, {'CONVEYOR_AGENT_CHANNEL_ROUTING':'0'}):
            await ui.handle(self.settings,self.tg,self.port,token)
        self.assertIsNone(routing.bound(self.settings,'telegram','1'))

    async def test_feishu_schema_routes_only_bound_token(self):
        await ui.show(self.settings,self.fs,self.port,'agents')
        button=self.port.cards[-1]['elements'][1]['actions'][0]
        payload=parse_action(button['value'])
        self.assertEqual(payload['action'],'agent_switcher')
        self.assertIsNone(parse_action({'action':'agent_switcher','job_id':'task'}))
        self.assertIsNone(parse_action({'action':'agent_switcher','token':payload['token'],'job_id':'task'}))
        event={'event':{'operator':{'open_id':'ou_owner'},'context':{'open_chat_id':'oc_private'},'action':{'value':button['value']}}}
        identity,value=extract_card_action(event)
        self.assertEqual(identity['chat_id'],'oc_private')
        await ui.handle(self.settings,self.fs,self.port,value['token'])

    async def test_failed_interactive_delivery_retains_executable_text_choices(self):
        from unittest.mock import AsyncMock
        self.port.reply_with_buttons=AsyncMock(side_effect=RuntimeError('offline keyboard'))
        await ui.show(self.settings,self.tg,self.port,'agents')
        self.assertIn('/agent '+self.a['id'],self.port.text)
        self.port.send_card=AsyncMock(return_value=None)
        await ui.show(self.settings,self.fs,self.port,'topics')
        root=conversations.summary(self.settings,self.fs)
        self.assertIn('/conversation '+root['id'][:12],self.port.text)

    async def test_short_help_exposes_the_direct_switchers(self):
        from handlers.commands import _help
        await _help(self.tg,self.port,None,self.settings,'')
        self.assertIn('/agents',self.port.text)
        self.assertIn('/conversations',self.port.text)
        self.assertLess(len(self.port.text),600)
