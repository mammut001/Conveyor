"""Project/topic isolation through real queue, Git worktrees and task controls.

Only the LLM execution is stubbed. Routing, persistence, Git and Apply/Discard
all run normally, exercising the flow a Telegram operator uses.
"""
import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from agents import AgentStore, agent_for_chat, conversation_for_chat
from channel.telegram import TelegramChatOutbound, inbound_from_update
from channel.telegram_identity import TelegramAddress, context_tag
from channel.types import InboundMessage
from handlers.dispatch import dispatch
from handlers.job_queue import JobQueue
from handlers.session import append_turn, build_context_prompt
from handlers.tools.confirm import clear_all_pending, create_pending, get_pending
from handlers.tools.runner import execute_confirmed
from tests.test_agent_workspace import _repo
from tests.test_agents import _settings
from runner import CodexRunner


class Port:
    supports_inline_buttons = True
    supports_attachments = False
    wait_for_job = True

    def __init__(self):
        self.messages, self.buttons = [], []

    async def reply(self, msg, text):
        self.messages.append((msg.chat_id, text))
        return str(len(self.messages))

    send_new = reply

    async def edit_progress(self, msg, placeholder, text):
        return True

    async def reply_with_buttons(self, msg, text, buttons):
        self.buttons.append(buttons)
        return await self.reply(msg, text)


class TelegramAgentFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.host = _repo(root / 'host', 'host')
        self.repo_a = _repo(root / 'alpha', 'alpha')
        self.repo_b = _repo(root / 'beta', 'beta')
        self.settings = replace(_settings(root), codex_workspace_root=self.host,
                                long_term_memory_enabled=True, chat_tools_enabled=True)
        for folder in ('logs', 'worktrees', 'locks'):
            (self.settings.codex_task_root / folder).mkdir(parents=True, exist_ok=True)
        self.store = AgentStore(self.settings)
        self.a = self.store.create({'name': 'Alpha', 'workspace_path': str(self.repo_a), 'instructions': 'Alpha only.'})
        self.b = self.store.create({'name': 'Beta', 'workspace_path': str(self.repo_b), 'instructions': 'Beta only.'})
        self.runner = CodexRunner(self.settings)
        self.queue = JobQueue()
        self.queue.configure(self.settings, self.runner)
        patcher = patch('handlers.job_queue.get_job_queue', return_value=self.queue)
        patcher.start()
        self.addCleanup(patcher.stop)
        clear_all_pending()
        self.addCleanup(clear_all_pending)
        self.port = Port()

    def message(self, text, topic=11):
        return InboundMessage('telegram', '1', f'-10042:topic:{topic}', '5', text, chat_type='group', mentioned_bot=True)

    def routed(self, msg):
        return replace(msg, chat_id=conversation_for_chat(self.settings, msg.channel, msg.chat_id))

    async def test_two_projects_execute_diff_apply_and_discard_in_their_own_topics(self):
        prompts = []

        async def fake_model(job, progress):
            prompts.append(job.prompt)
            file = job.worktree_path / 'README.md'
            file.write_text(file.read_text() + 'edited\n')
            job.final_message_path.write_text(f'Edited {job.workspace_root.name}')
            job.return_code = 0

        with patch.object(self.runner, '_run_codex_attempt', side_effect=fake_model):
            await dispatch(self.message('/agent Alpha'), self.port, self.settings, self.runner)
            await dispatch(self.message('/fix add a line'), self.port, self.settings, self.runner)
            await dispatch(self.message('/agent Beta', 22), self.port, self.settings, self.runner)
            await dispatch(self.message('/fix add a different line', 22), self.port, self.settings, self.runner)
        self.assertIn('Alpha only.', prompts[0])
        self.assertNotIn('Beta only.', prompts[0])
        self.assertIn('Beta only.', prompts[1])
        self.assertNotIn('Alpha only.', prompts[1])
        await dispatch(self.message('/diff'), self.port, self.settings, self.runner)
        self.assertIn('alpha', self.port.messages[-1][1])
        self.assertNotIn('-beta', self.port.messages[-1][1])
        await dispatch(self.message('/last'), self.port, self.settings, self.runner)
        self.assertEqual(self.port.messages[-1][1], 'Edited alpha')
        await dispatch(self.message('/apply'), self.port, self.settings, self.runner)
        self.assertIn('Applied', self.port.messages[-1][1])
        self.assertEqual((self.repo_a / 'README.md').read_text(), 'alpha\nedited\n')
        self.assertEqual((self.repo_b / 'README.md').read_text(), 'beta\n')
        self.assertEqual((self.host / 'README.md').read_text(), 'host\n')
        await dispatch(self.message('/discard', 22), self.port, self.settings, self.runner)
        self.assertIn('Discarded', self.port.messages[-1][1])
        self.assertEqual((self.repo_b / 'README.md').read_text(), 'beta\n')

    async def test_private_switch_keeps_history_and_resumed_jobs_on_original_project(self):
        msg = replace(self.message('/agent Alpha'), chat_id='42', chat_type='p2p')
        await dispatch(msg, self.port, self.settings, self.runner)
        alpha = self.routed(msg)
        append_turn(self.settings, alpha, 'alpha-only discussion', 'alpha response')
        ok, _, queued = await self.queue.enqueue('fix', 'queued alpha task', alpha, self.port, self.runner)
        self.assertTrue(ok)
        await dispatch(replace(msg, text='/agent Beta'), self.port, self.settings, self.runner)
        beta = self.routed(msg)
        self.assertNotIn('alpha-only discussion', build_context_prompt(self.settings, beta))
        recovered = JobQueue()
        recovered.configure(self.settings, self.runner, recover=False)
        restored = await recovered.get_job(queued.id)
        self.assertEqual(agent_for_chat(self.settings, restored.channel, restored.chat_id)['id'], self.a['id'])
        await dispatch(replace(msg, text='/agent Alpha'), self.port, self.settings, self.runner)
        self.assertEqual(self.routed(msg).chat_id, alpha.chat_id)
        self.assertIn('alpha-only discussion', build_context_prompt(self.settings, self.routed(msg)))

    async def test_cancel_never_cancels_another_topic_or_operator(self):
        for topic, agent in ((11, self.a), (22, self.b)):
            self.store.bind_chat('telegram', self.message('', topic).chat_id, agent['id'])
        alpha, beta = self.routed(self.message('alpha')), self.routed(self.message('beta', 22))
        _, _, a_job = await self.queue.enqueue('fix', 'alpha task', alpha, self.port, self.runner)
        _, _, b_job = await self.queue.enqueue('fix', 'beta task', beta, self.port, self.runner)
        await dispatch(self.message('/cancel'), self.port, self.settings, self.runner)
        self.assertEqual(self.queue.job_snapshot(a_job.id)['state'], 'cancelled')
        self.assertEqual(self.queue.job_snapshot(b_job.id)['state'], 'queued')
        self.assertIsNone(self.runner.current_job)
        self.assertEqual(self.queue.conversation_jobs('telegram', beta.chat_id, '999'), [])

    async def test_switch_and_cross_topic_confirmation_cannot_run_old_action(self):
        self.store.bind_chat('telegram', self.message('').chat_id, self.a['id'])
        alpha = self.routed(self.message(''))
        pending = create_pending(tool_name='service_restart', arg='conveyor-web.service',
                                 operator_id='1', channel='telegram', chat_id=alpha.chat_id)
        self.store.bind_chat('telegram', self.message('').chat_id, self.b['id'])
        for msg in (self.routed(self.message('')), self.routed(self.message('', 22))):
            with patch('handlers.tools.runner.run_tool', new_callable=AsyncMock) as run:
                await execute_confirmed(msg, self.port, self.settings, pending.token)
                run.assert_not_called()
                self.assertIsNotNone(get_pending(pending.token))

    async def test_picker_is_bound_to_topic_and_create_validates_project(self):
        from handlers.agent_selection import handle_agent_callback
        await dispatch(self.message('/agent'), self.port, self.settings, self.runner)
        data = self.port.buttons[-1][1][0]['callback_data']
        self.assertLessEqual(len(data.encode()), 64)
        await handle_agent_callback(self.message('', 22), self.port, self.runner, self.settings, data)
        self.assertIsNone(self.store.bound_agent_id('telegram', self.message('', 22).chat_id))
        await handle_agent_callback(self.message(''), self.port, self.runner, self.settings, data)
        self.assertEqual(self.store.bound_agent_id('telegram', self.message('').chat_id), self.a['id'])
        await dispatch(self.message(f'/agent new New project | {self.repo_a}', 33), self.port, self.settings, self.runner)
        created = agent_for_chat(self.settings, 'telegram', self.message('', 33).chat_id)
        self.assertEqual((created['name'], created['workspace_path']), ('New project', str(self.repo_a)))
        before = len(self.store.list())
        await dispatch(self.message(f'/agent new Wrong | {self.repo_a / ".git"}', 44), self.port, self.settings, self.runner)
        self.assertEqual(len(self.store.list()), before)
        self.assertIsNone(self.store.bound_agent_id('telegram', self.message('', 44).chat_id))

    async def test_project_git_status_uses_selected_repository(self):
        await dispatch(self.message('/agent Alpha'), self.port, self.settings, self.runner)
        await dispatch(self.message('/git_status'), self.port, self.settings, self.runner)
        self.assertIn(str(self.repo_a), self.port.messages[-1][1])
        self.assertNotIn(str(self.host), self.port.messages[-1][1])

    async def test_long_job_releases_updates_and_remains_identifiable_after_switch(self):
        from channel.telegram import TelegramOutbound
        from handlers.jobs import _JOB_TASKS
        self.assertFalse(TelegramOutbound.wait_for_job)
        self.port.wait_for_job = False
        started, release = asyncio.Event(), asyncio.Event()

        async def fake_model(job, progress):
            started.set()
            await release.wait()
            job.final_message_path.write_text('Alpha finished in background')
            job.return_code = 0

        await dispatch(self.message('/agent Alpha'), self.port, self.settings, self.runner)
        with patch.object(self.runner, '_run_codex_attempt', side_effect=fake_model):
            try:
                await asyncio.wait_for(dispatch(self.message('/fix background task'), self.port, self.settings, self.runner), timeout=1)
                await asyncio.wait_for(started.wait(), timeout=3)
                await dispatch(self.message('/agent Beta'), self.port, self.settings, self.runner)
                await dispatch(self.message('/cancel'), self.port, self.settings, self.runner)
                self.assertFalse(self.runner.current_job.cancel_requested)
                self.assertIn('还没有任务', self.port.messages[-1][1])
            finally:
                release.set()
                await asyncio.gather(*list(_JOB_TASKS))
        final = [text for _, text in self.port.messages if 'Alpha finished in background' in text]
        self.assertTrue(final)
        self.assertTrue(final[-1].startswith('Alpha · q'))
        self.assertEqual(self.queue.list_jobs()[0]['state'], 'completed')

    async def test_recovery_preserves_live_owner_and_interrupts_dead_owner(self):
        self.store.bind_chat('telegram', self.message('').chat_id, self.a['id'])
        msg = self.routed(self.message('queued'))
        _, _, queued = await self.queue.enqueue('fix', 'pending', msg, self.port, self.runner)
        await self.queue.dequeue(require_idle=True)
        reopened = JobQueue()
        reopened.configure(self.settings, self.runner)
        self.assertEqual(reopened.job_snapshot(queued.id)['state'], 'running')
        with patch('handlers.job_queue._owner_is_live', return_value=False):
            reopened.recover_and_load()
        self.assertEqual(reopened.job_snapshot(queued.id)['state'], 'interrupted')

    async def test_startup_resumes_pending_with_original_topic_agent(self):
        self.store.bind_chat('telegram', self.message('').chat_id, self.a['id'])
        msg = self.routed(self.message('queued'))
        _, _, queued = await self.queue.enqueue('fix', 'pending', msg, self.port, self.runner)
        self.store.bind_chat('telegram', self.message('').chat_id, self.b['id'])
        reopened = JobQueue()
        reopened.configure(self.settings, self.runner)
        callback = AsyncMock()
        reopened.set_start_callback(callback)
        await reopened.start_pending()
        restored = callback.call_args.args[0]
        self.assertEqual(restored.id, queued.id)
        self.assertEqual(restored.chat_id, msg.chat_id)
        self.assertEqual(agent_for_chat(self.settings, restored.channel, restored.chat_id)['id'], self.a['id'])

    async def test_reset_returns_original_default_history(self):
        original = self.message('default discussion')
        append_turn(self.settings, original, 'default-only fact', 'default response')
        await dispatch(self.message('/agent Alpha'), self.port, self.settings, self.runner)
        alpha = self.routed(original)
        append_turn(self.settings, alpha, 'alpha-only fact', 'alpha response')
        await dispatch(self.message('/agent reset'), self.port, self.settings, self.runner)
        reset_msg = self.routed(original)
        self.assertEqual(reset_msg.chat_id, original.chat_id)
        self.assertIn('default-only fact', build_context_prompt(self.settings, reset_msg))
        self.assertNotIn('alpha-only fact', build_context_prompt(self.settings, reset_msg))
        await dispatch(self.message('/agent Alpha'), self.port, self.settings, self.runner)
        self.assertIn('alpha-only fact', build_context_prompt(self.settings, self.routed(original)))

    async def test_archived_pending_job_can_be_cancelled_without_project_fallback(self):
        await dispatch(self.message('/agent Alpha'), self.port, self.settings, self.runner)
        msg = self.routed(self.message('queued'))
        _, _, queued = await self.queue.enqueue('fix', 'pending', msg, self.port, self.runner)
        self.store.archive(self.a['id'])
        await dispatch(self.message('/cancel'), self.port, self.settings, self.runner)
        self.assertEqual(self.queue.job_snapshot(queued.id)['state'], 'cancelled')

    async def test_pinned_jobs_fail_closed_when_feature_disabled_or_database_unavailable(self):
        from agents import AgentError, workspace_for_chat, computer_target_for_chat
        from personal_tools.long_term_memory import owner_for_chat
        await dispatch(self.message('/agent Alpha'), self.port, self.settings, self.runner)
        msg = self.routed(self.message('queued'))
        with self.assertRaises(AgentError):
            workspace_for_chat(replace(self.settings, agents_enabled=False), 'telegram', msg.chat_id)
        with patch('agents.AgentStore.get', side_effect=__import__('sqlite3').OperationalError('busy')):
            with self.assertRaises(AgentError):
                workspace_for_chat(self.settings, 'telegram', msg.chat_id)
            with self.assertRaises(AgentError):
                computer_target_for_chat(replace(self.settings, agent_desktops_enabled=True), 'telegram', msg.chat_id)
            with self.assertRaises(Exception):
                owner_for_chat(self.settings, '1', 'telegram', msg.chat_id)

    async def test_telegram_routine_keeps_project_and_topic_after_switch(self):
        import routines
        self.store.bind_chat('telegram', self.message('').chat_id, self.a['id'])
        msg = self.routed(self.message('routine'))
        settings = replace(self.settings, routines_enabled=True)
        routine = routines.create_routine(settings, name='Check Alpha', schedule='0 9 * * *',
            prompt='Check repository', deliver=['web'], origin_channel='telegram', origin_chat_id=msg.chat_id)
        self.store.bind_chat('telegram', self.message('').chat_id, self.b['id'])
        seen = []

        async def fake_chat(msg, port, settings, **kwargs):
            seen.append(msg)
            port.last_text = 'Alpha checked'
            return 'answered', None

        with patch('handlers.chat.ask_chat', side_effect=fake_chat):
            result = await routines.run_single_routine(settings, self.runner, routine)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual((seen[0].channel, seen[0].chat_id, seen[0].operator_id), ('telegram', msg.chat_id, '1'))
        self.assertEqual(routines.list_routines(settings)[0]['agent_id'], self.a['id'])

    async def test_file_search_roots_use_the_selected_project(self):
        await dispatch(self.message('/agent Alpha'), self.port, self.settings, self.runner)
        await dispatch(self.message('/files_roots'), self.port, self.settings, self.runner)
        self.assertIn(str(self.repo_a), self.port.messages[-1][1])
        self.assertNotIn(str(self.host), self.port.messages[-1][1])


class TelegramTopicTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_topics_do_not_share_session_addresses(self):
        msg = NS(text='/agent', message_id=5, is_topic_message=True, message_thread_id=11)
        update = NS(effective_chat=NS(id=-10042, type='supergroup'), effective_user=NS(id=1),
                    effective_message=msg, get_bot=lambda: NS(username='Bot', id=2))
        self.assertEqual(inbound_from_update(update).chat_id, '-10042:topic:11')
        msg.message_thread_id = 22
        self.assertEqual(inbound_from_update(update).chat_id, '-10042:topic:22')
        msg.is_topic_message = False
        self.assertEqual(inbound_from_update(update).chat_id, '-10042')

    async def test_chat_port_decodes_topic_and_really_edits_progress(self):
        bot = NS(send_message=AsyncMock(return_value=NS(message_id=42)), edit_message_text=AsyncMock())
        port = TelegramChatOutbound(bot, '-10042:topic:11:agent:alpha')
        msg = InboundMessage('telegram', '1', '-10042:topic:11:agent:alpha', None, '')
        self.assertEqual(await port.send_new(msg, 'done'), '42')
        self.assertEqual(bot.send_message.call_args.kwargs['chat_id'], -10042)
        self.assertEqual(bot.send_message.call_args.kwargs['message_thread_id'], 11)
        self.assertTrue(await port.edit_progress(msg, '42', 'update'))
        bot.edit_message_text.assert_awaited_once()
        bot.edit_message_text.side_effect = RuntimeError('network failed')
        self.assertFalse(await port.edit_progress(msg, '42', 'update'))

    async def test_http_delivery_and_recovered_jobs_preserve_topic(self):
        import io
        import urllib.parse
        from scripts.telegram_api import send_message
        from handlers.jobs import RecoveredOutboundPort
        settings = NS(telegram_bot_token='test-token', telegram_allowed_user_id=1)
        requests = []

        def urlopen(request, **kwargs):
            requests.append(urllib.parse.parse_qs(request.data.decode()))
            return io.BytesIO(json.dumps({'ok': True}).encode())

        with patch('scripts.telegram_api.urllib.request.urlopen', side_effect=urlopen):
            send_message(settings, 'reminder', '-10042:topic:11:agent:alpha')
            port = RecoveredOutboundPort('telegram', '-10042:topic:22:agent:beta', settings)
            await port.send_new(None, 'recovered')
        self.assertEqual(requests[0]['chat_id'], ['-10042'])
        self.assertEqual(requests[0]['message_thread_id'], ['11'])
        self.assertEqual(requests[1]['message_thread_id'], ['22'])
        self.assertNotIn('agent', str(requests))

    async def test_invalid_addresses_fail_closed(self):
        for value in ('42:topic:0', '42:agent:../../etc', '42:topic:11:garbage', '@somewhere'):
            with self.assertRaises(ValueError):
                TelegramAddress.parse(value)
