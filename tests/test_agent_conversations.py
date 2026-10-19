"""Independent entry roots and explicit continuation without transport adoption."""
from dataclasses import replace
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import agent_context as context
import agent_channel_routing as routing
import agent_conversations as conversations
from agents import AgentStore, chat_id_for, session_id_for
from channel.types import InboundMessage
from config import Settings
from handlers import chat
from handlers.session import append_turn, get_recent_turns, session_path
from transcript_store import get_transcript_store


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.settings = Settings(telegram_bot_token='test', telegram_allowed_user_id=1,
            lark_allowed_open_id='owner', codex_workspace_root=root, codex_bin='codex',
            codex_task_root=root/'tasks', codex_model=None, codex_timeout_seconds=30,
            codex_retry_429_delays_seconds=(), telegram_progress_seconds=1,
            codex_memory_root=root, user_timezone='UTC', agents_enabled=True,
            chat_mode='auto', chat_api_key='test', chat_model='test')
        flags = patch.dict(os.environ, {'CONVEYOR_NAMED_AGENTS':'1',
            'CONVEYOR_AGENT_CONTEXT':'1','CONVEYOR_AGENT_CHANNEL_ROUTING':'1'})
        flags.start(); self.addCleanup(flags.stop); self.addCleanup(chat.reset)
        store = AgentStore(self.settings)
        self.a = store.create({'name':'Writer','tools_allowlist':['notes.search']})
        self.b = store.create({'name':'Reviewer','tools_allowlist':[]})
        self.tg = self.msg('telegram'); self.fs = self.msg('feishu')
        for msg in (self.tg, self.fs): routing.bind(self.settings, msg, 'Writer')

    def msg(self, channel):
        return InboundMessage(channel=channel, operator_id='1' if channel=='telegram' else 'owner',
                              chat_id='private-'+channel, message_id='m', text='hello', chat_type='p2p')

    def key(self, msg):
        return context.history_key(self.settings, msg.channel, msg.chat_id, msg.operator_id)

    def web_row(self, msg):
        return next(row for row in conversations.list_for_entry(self.settings,msg)
                    if row['origin_channel']=='web')

    def test_entry_roots_and_context_files_are_independent(self):
        tg = conversations.summary(self.settings,self.tg)
        fs = conversations.summary(self.settings,self.fs)
        self.assertNotEqual(self.key(self.tg), self.key(self.fs))
        self.assertNotEqual(tg['history_key'], self.web_row(self.tg)['history_key'])
        append_turn(self.settings,self.tg,'telegram-only','private answer')
        self.assertEqual(get_recent_turns(self.settings,self.fs), [])
        self.assertNotEqual(session_path(self.settings,self.tg), session_path(self.settings,self.fs))
        self.assertIsNone(get_transcript_store(self.settings).get_session(session_id_for(self.a['id'])))
        self.assertEqual(get_transcript_store(self.settings).last_message(conversations.transcript_id(tg))['content'], 'private answer')

    def test_explicit_continuation_shares_history_and_keeps_transport_permissions(self):
        target = conversations.summary(self.settings,self.tg)
        append_turn(self.settings,self.tg,'previous topic','previous result')
        conversations.select(self.settings,self.fs,target['id'][:12])
        self.assertEqual(self.key(self.fs),self.key(self.tg))
        self.assertEqual(get_recent_turns(self.settings,self.fs)[0]['user'],'previous topic')
        web = context.resolve(self.settings,'web',target['transport_chat_id'],'web-console')
        phone = context.resolve(self.settings,'feishu',self.fs.chat_id,self.fs.operator_id)
        self.assertEqual(web.agent_id,phone.agent_id)
        self.assertEqual(web.origin_channel,'web')
        self.assertEqual(phone.origin_channel,'feishu')
        self.assertEqual(phone.operator_identity,'owner')
        self.assertEqual(web.tool_capabilities,frozenset({'notes.search'}))
        with self.assertRaises(context.AgentContextDenied):
            context.resolve(self.settings,'web',target['transport_chat_id'],'other-console-user')
        self.assertEqual(context.history_key(self.settings,'web',target['transport_chat_id'],'web-console'),self.key(self.tg))

    async def test_actual_model_context_is_independent_until_explicit_continuation(self):
        chat.remember(self.key(self.tg),'only in Telegram','telegram answer',6,settings=self.settings)
        payloads=[]
        async def answer(config,messages):
            payloads.append(messages)
            yield 'hello\n[[confidence:high]]'
        port=AsyncMock(); port.reply.return_value='pending'; port.handles_transcript_directly=False
        from handlers.dispatch import dispatch
        with patch('runner.chat_client.stream_chat',new=answer):
            await dispatch(self.fs,port,self.settings,object())
            self.assertNotIn('only in Telegram',str(payloads[-1]))
            conversations.select(self.settings,self.fs,conversations.summary(self.settings,self.tg)['id'])
            await dispatch(self.fs,port,self.settings,object())
            self.assertIn('only in Telegram',str(payloads[-1]))

    async def test_inflight_reply_stays_in_original_conversation_after_selection_change(self):
        original = conversations.summary(self.settings,self.tg)
        entered=asyncio.Event(); release=asyncio.Event()
        async def answer(config,messages):
            entered.set(); await release.wait(); yield 'original reply\n[[confidence:high]]'
        from handlers.dispatch import dispatch
        port=AsyncMock(); port.reply.return_value='pending'; port.handles_transcript_directly=False
        with patch('runner.chat_client.stream_chat',new=answer):
            running=asyncio.create_task(dispatch(self.tg,port,self.settings,object()))
            await asyncio.wait_for(entered.wait(),2)
            other=conversations.new(self.settings,self.tg,'another topic')
            release.set(); await running
        self.assertTrue(chat.history(original['history_key'],6,settings=self.settings))
        self.assertEqual(chat.history(other['history_key'],6,settings=self.settings),[])
        self.assertEqual(get_transcript_store(self.settings).last_message(conversations.transcript_id(original))['content'],'original reply')
        self.assertIsNone(get_transcript_store(self.settings).get_session(conversations.transcript_id(other)))

    async def test_durable_context_recovers_after_cache_expiry_and_clear_is_a_boundary(self):
        row=conversations.summary(self.settings,self.tg)
        append_turn(self.settings,self.tg,'durable old topic','old result')
        chat.remember(row['history_key'],'durable old topic','old result',6,now=1,settings=self.settings)
        self.assertEqual(chat.history(row['history_key'],6,settings=self.settings),[])
        payloads=[]
        async def answer(config,messages):
            payloads.append(messages); yield 'answer\n[[confidence:high]]'
        port=AsyncMock(); port.reply.return_value='pending'; port.handles_transcript_directly=False
        from handlers.dispatch import dispatch
        with patch('runner.chat_client.stream_chat',new=answer):
            await dispatch(self.tg,port,self.settings,object())
            self.assertIn('durable old topic',str(payloads[-1]))
            await dispatch(replace(self.tg,text='/chat_clear'),port,self.settings,object())
            await dispatch(self.tg,port,self.settings,object())
            self.assertNotIn('durable old topic',str(payloads[-1]))
        archived=get_transcript_store(self.settings).get_session(conversations.transcript_id(row))
        self.assertTrue(any(m['content']=='durable old topic' for m in archived['messages']))

    def test_selection_survives_agent_switch_and_local_restores_root(self):
        original=conversations.summary(self.settings,self.tg)
        topic=conversations.new(self.settings,self.tg,'项目发布')
        routing.bind(self.settings,self.tg,'Reviewer')
        self.assertNotEqual(conversations.summary(self.settings,self.tg)['id'],topic['id'])
        routing.bind(self.settings,self.tg,'Writer')
        self.assertEqual(conversations.summary(self.settings,self.tg)['id'],topic['id'])
        conversations.select(self.settings,self.tg,'local')
        self.assertEqual(conversations.summary(self.settings,self.tg)['id'],original['id'])

    def test_default_entry_reuses_its_own_legacy_context_without_importing_other_agents(self):
        import json
        from handlers.session import _safe_filename
        key=f'telegram:{self.tg.chat_id}'
        chat.remember(key,'legacy default question','legacy default answer',6,settings=self.settings)
        directory=self.settings.codex_memory_root/'session'; directory.mkdir(exist_ok=True)
        path=directory/_safe_filename(self.tg.channel,self.tg.chat_id,self.tg.operator_id)
        path.write_text(json.dumps({'user':'legacy default question','assistant':'legacy default answer'})+'\n')
        routing.bind(self.settings,self.tg,'main')
        row=conversations.summary(self.settings,self.tg)
        self.assertEqual(self.key(self.tg),key)
        self.assertEqual(session_path(self.settings,self.tg),path)
        self.assertEqual(conversations.recent_history(self.settings,row,6)[0]['content'],'legacy default question')
        routing.bind(self.settings,self.tg,'Writer')
        self.assertNotEqual(self.key(self.tg),key)
        self.assertEqual(get_recent_turns(self.settings,self.tg),[])

    def test_legacy_web_history_is_preserved_and_only_selected_explicitly(self):
        legacy=self.web_row(self.tg)
        chat.remember(legacy['history_key'],'old Web topic','old answer',6,settings=self.settings)
        self.assertEqual(chat.history(self.key(self.tg),6,settings=self.settings),[])
        conversations.select(self.settings,self.tg,legacy['id'])
        self.assertEqual(chat.history(self.key(self.tg),6,settings=self.settings)[0]['content'],'old Web topic')
        web=InboundMessage(channel='web',operator_id='web-console',chat_id=chat_id_for(self.a['id']),message_id=None,text='')
        self.assertEqual(session_path(self.settings,web),session_path(self.settings,self.tg))

    async def test_queued_conversation_survives_selection_and_process_reference_loss(self):
        from handlers.job_queue import JobQueue
        queue=JobQueue(); queue.configure(self.settings,runner=None,recover=False)
        runner=SimpleNamespace(settings=self.settings)
        first=conversations.summary(self.settings,self.tg)
        ok,_,job=await queue.enqueue('run','first task',self.tg,AsyncMock(),runner)
        self.assertTrue(ok)
        second=conversations.new(self.settings,self.tg,'second topic')
        _,_,other=await queue.enqueue('run','second task',self.tg,AsyncMock(),runner)
        self.assertNotEqual(job.session_id,other.session_id)
        self.assertFalse(other.refinement_intent)
        _,_,followup=await queue.enqueue('run','second followup',self.tg,AsyncMock(),runner)
        self.assertTrue(followup.refinement_intent)
        reopened=JobQueue(); reopened.configure(self.settings,runner=None,recover=False)
        restored=await reopened.get_job(job.id)
        self.assertEqual(restored.conversation_id,first['id'])
        seen=[]
        @conversations.scoped_job
        async def execute(msg,port,runner):
            seen.append(self.key(msg))
        await execute(self.tg,AsyncMock(),runner,conversation_id=restored.conversation_id,conversation_context=restored.conversation_context)
        self.assertEqual(seen,[first['history_key']])
        # A different completed request's context must not leak into this job.
        foreign=context.resolve(self.settings,'web',chat_id_for(self.b['id']),'web-console')
        with context.bind(foreign):
            await execute(self.tg,AsyncMock(),runner,conversation_id=restored.conversation_id,
                          conversation_context=restored.conversation_context)
        self.assertEqual(seen,[first['history_key'],first['history_key']])
        self.assertEqual(self.key(self.tg),second['history_key'])
        routing.bind(self.settings,self.tg,'Reviewer')
        denied=AsyncMock()
        await execute(self.tg,denied,runner,conversation_id=restored.conversation_id,conversation_context=restored.conversation_context)
        self.assertEqual(len(seen),2)
        self.assertIn('未执行',denied.reply.await_args.args[1])

    async def test_recovered_task_does_not_gain_capabilities_removed_by_delegation(self):
        from handlers.job_queue import JobQueue
        queue=JobQueue(); queue.configure(self.settings,runner=None,recover=False)
        runner=SimpleNamespace(settings=self.settings)
        actual=context.resolve(self.settings,self.tg.channel,self.tg.chat_id,self.tg.operator_id)
        restricted=replace(actual,tool_capabilities=frozenset(),delegation_id='bounded-child')
        with context.bind(restricted):
            _,_,job=await queue.enqueue('run','bounded request',self.tg,AsyncMock(),runner)
        seen=[]
        @conversations.scoped_job
        async def execute(msg,port,runner):
            ctx=context.for_request(self.settings,msg.channel,msg.chat_id,msg.operator_id)
            seen.append(ctx.tool_capabilities)
        await execute(self.tg,AsyncMock(),runner,conversation_id=job.conversation_id,
                      conversation_context=job.conversation_context)
        self.assertEqual(seen,[frozenset()])

    def test_continuing_history_does_not_share_task_worktree_authority(self):
        target=conversations.summary(self.settings,self.tg)
        conversations.select(self.settings,self.fs,target['id'])
        self.assertEqual(self.key(self.fs),self.key(self.tg))
        self.assertNotEqual(conversations.refinement_id(self.settings,self.fs),
                            conversations.refinement_id(self.settings,self.tg))

    def test_workbench_resolves_the_conversation_scoped_refinement_chain(self):
        from handlers.job_queue import JobQueue
        from refinement_store import RefinementStore
        from web_control import WebControl
        target=conversations.summary(self.settings,self.tg)
        msg=InboundMessage(channel='web',operator_id='web-console',
            chat_id=target['transport_chat_id'],message_id=None,text='')
        key=conversations.refinement_id(self.settings,msg)
        store=RefinementStore(self.settings)
        chain=store.bind_new(session_id=key,channel='web',operator_id='web-console',
            source_chat_id=msg.chat_id,worktree_path=self.settings.codex_memory_root/'wt',
            runtime_job_id='runtime',queue_job_id='queue')
        queue=JobQueue(); queue.configure(self.settings,runner=None,recover=False)
        control=WebControl(self.settings,runner=None,queue=queue)
        session={'id':conversations.transcript_id(target),'channel':'web',
                 'operator_id':'web-console','source_chat_id':msg.chat_id}
        result=control._attach_session_workbench(session,[])
        self.assertEqual(result['active_refinement']['chain_id'],chain['id'])

    def test_owner_agent_and_archive_checks_reject_foreign_selectors(self):
        target=conversations.summary(self.settings,self.tg)
        for bad in (replace(self.fs,operator_id='stranger'),replace(self.fs,chat_type='group')):
            with self.assertRaises(PermissionError): conversations.select(self.settings,bad,target['id'])
        routing.bind(self.settings,self.fs,'Reviewer')
        with self.assertRaises(ValueError): conversations.select(self.settings,self.fs,target['id'])
        self.assertNotIn(target['id'],[row['id'] for row in conversations.list_for_entry(self.settings,self.fs)])
        self.assertIsNone(conversations.for_web_chat(self.settings,'topic-'+('f'*32)))
        AgentStore(self.settings).archive(self.a['id'])
        with self.assertRaises(context.AgentContextDenied):
            context.resolve(self.settings,'web',target['transport_chat_id'],'web-console')

    async def test_command_listing_is_read_only_and_group_refusal_is_silent(self):
        before=conversations.summary(self.settings,self.tg)['id']
        port=AsyncMock()
        await conversations.maybe_command(self.settings,replace(self.tg,text='接着已有会话聊'),port)
        self.assertIn('/conversation',port.reply.await_args.args[1])
        self.assertEqual(conversations.summary(self.settings,self.tg)['id'],before)
        port.reset_mock()
        await conversations.maybe_command(self.settings,replace(self.tg,text='/conversations',chat_type='group'),port)
        port.reply.assert_not_awaited()
