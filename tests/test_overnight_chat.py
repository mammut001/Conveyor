import unittest
from unittest.mock import patch,Mock
from overnight_chat import route,invoke


class ChatTests(unittest.IsolatedAsyncioTestCase):
    def test_explicit_commands_route_with_exact_objective(self):
        self.assertEqual(route('/nightly_start 检查聊天').arg,'检查聊天')
        self.assertEqual(route('启动夜间任务：检查聊天').tools,('nightly.start',))
        self.assertIsNone(route('修改夜间任务按钮'))

    async def test_unauthorized_channel_never_reaches_control(self):
        with patch.dict('os.environ',{'CONVEYOR_NIGHTLY_CONTROL':'1'}),patch('overnight_chat.client') as client:
            result=await invoke('start','test',operator_id='other',channel='web')
        self.assertFalse(result.ok);client.assert_not_called()

    async def test_start_returns_trackable_id(self):
        run={'id':'a'*32,'state':'running','objective':'检查聊天','tasks':[]}
        with patch.dict('os.environ',{'CONVEYOR_NIGHTLY_CONTROL':'1'}),patch('overnight_chat.client',return_value=run) as client:
            result=await invoke('start','检查聊天',operator_id='web-console',channel='web')
        self.assertTrue(result.ok)
        self.assertIn('/nightly_status '+'a'*32,result.text)
        client.assert_called_once_with({'action':'start','objective':'检查聊天'})
