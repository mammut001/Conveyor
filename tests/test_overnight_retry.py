import unittest
from unittest.mock import AsyncMock,Mock
from overnight_pipeline import OvernightPipeline


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_verification_queues_bounded_new_repair(self):
        github=Mock()
        pipeline=OvernightPipeline(github=github,discover=None,repair=None,
                                   verify=AsyncMock(side_effect=RuntimeError('test failure')),deploy=AsyncMock())
        payload={'issue':{'number':12},'candidate':{'pr':34,'head':'a'*40},'repair_attempt':1}
        result=await pipeline.verify({'payload':payload})
        self.assertEqual(result.next_tasks[0][0],'repair')
        self.assertEqual(result.next_tasks[0][2]['repair_attempt'],2)
        github.merge_verified.assert_not_called()
        pipeline.deploy_candidate.assert_not_awaited()
        payload['repair_attempt']=3
        with self.assertRaisesRegex(RuntimeError,'retry limit'):
            await pipeline.verify({'payload':payload})
