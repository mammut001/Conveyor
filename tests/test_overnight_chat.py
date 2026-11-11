import unittest
from unittest.mock import patch,Mock
from conveyor.automation.overnight_chat import route,invoke


class ChatTests(unittest.IsolatedAsyncioTestCase):
    def test_explicit_commands_route_with_exact_objective(self):
        self.assertEqual(route('/nightly_start 检查聊天').arg,'检查聊天')
        self.assertEqual(route('启动夜间任务：检查聊天').tools,('nightly.start',))
        self.assertIsNone(route('修改夜间任务按钮'))

    async def test_unauthorized_channel_never_reaches_control(self):
        with patch.dict('os.environ',{'CONVEYOR_NIGHTLY_CONTROL':'1'}),patch('conveyor.automation.overnight_chat.client') as client:
            result=await invoke('start','test',operator_id='other',channel='web')
        self.assertFalse(result.ok);client.assert_not_called()

    async def test_start_returns_trackable_id(self):
        run={'id':'a'*32,'state':'running','objective':'检查聊天','tasks':[]}
        with patch.dict('os.environ',{'CONVEYOR_NIGHTLY_CONTROL':'1'}),patch('conveyor.automation.overnight_chat.client',return_value=run) as client:
            result=await invoke('start','检查聊天',operator_id='web-console',channel='web')
        self.assertTrue(result.ok)
        self.assertIn('/nightly_status '+'a'*32,result.text)
        client.assert_called_once_with({'action':'start','objective':'检查聊天'})

    async def test_web_chat_commands_reach_control_without_pi_job(self):
        import queue,tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from tests.test_pi_backend import _make_settings
        from channel.types import InboundMessage
        from conveyor.web.web_chat import WebChatPort,run_web_chat
        ident='a'*32
        for text,request in [('/nightly_start 检查聊天',{'action':'start','objective':'检查聊天'}),
                             ('/nightly_status '+ident,{'action':'status','run_id':ident}),
                             ('/nightly_cancel '+ident,{'action':'cancel','run_id':ident})]:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as root:
                settings=_make_settings(Path(root));events=queue.Queue()
                msg=InboundMessage(channel='web',operator_id='web-console',chat_id='nightly-ui',message_id='m1',text=text)
                port=WebChatPort(events,settings,'web:web-console:nightly-ui',prompt=text)
                result={'id':ident,'state':'running','objective':'检查聊天','tasks':[]}
                with patch.dict('os.environ',{'CONVEYOR_NIGHTLY_CONTROL':'1'}),patch('conveyor.automation.overnight_chat.client',return_value=result) as client,patch('handlers.jobs.handle_codex_job',new_callable=AsyncMock) as job:
                    await run_web_chat(msg,port,settings,SimpleNamespace(name='pi'),text)
                client.assert_called_once_with(request)
                job.assert_not_awaited()
                emitted=list(events.queue)
                self.assertTrue(any(name=='message' and ident in data.get('text','') for name,data in emitted))
                self.assertEqual(emitted[-1],('done',{'outcome':'answered'}))
