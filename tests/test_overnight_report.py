import json
import unittest
from overnight_report import format_run


class ReportTests(unittest.TestCase):
    def test_report_includes_real_pr_issue_and_preview_evidence(self):
        result={'issue':{'number':157},'receipt':{'pr':158},'deployment':{'healthy':True,'commit':'a'*40}}
        report=format_run({'id':'b'*32,'state':'completed','objective':'check preview','max_tasks':12,
                           'tasks':[{'role':'verify','state':'passed','result':json.dumps(result)}]})
        self.assertIn('/issues/157',report);self.assertIn('/pull/158',report)
        self.assertIn('/commit/'+'a'*40,report)
        self.assertIn('这不代表网站不存在其他问题',report)

    def test_report_keeps_blocked_outcome_and_retry_evidence_visible(self):
        report=format_run({'id':'b'*32,'state':'blocked','objective':'check',
            'tasks':[{'role':'verify','state':'blocked','result':json.dumps({'error':'CommandFailure'})}]})
        self.assertIn('需要处理阻塞',report);self.assertIn('CommandFailure',report)
        self.assertNotIn('连续两轮通过',report)
