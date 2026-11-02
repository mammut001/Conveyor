import unittest
from copy import deepcopy
from overnight_github import assert_mergeable, GitHubDelivery


class GitHubGateTests(unittest.TestCase):
    def setUp(self):
        self.repo='owner/Conveyor';self.head='a'*40
        self.candidate={'number':12,'baseRefName':'preview','state':'OPEN','isDraft':False,
                        'mergeable':'MERGEABLE','headRefOid':self.head,
                        'headRepository':{'name':'Conveyor'},'headRepositoryOwner':{'login':'owner'},
                        'statusCheckRollup':[{'status':'COMPLETED','conclusion':'SUCCESS'}]}
        self.receipt={'repo':self.repo,'pr':12,'head':self.head,'passed':True,'checks':['reproduction','regression']}

    def test_production_stale_head_and_missing_ci_are_rejected(self):
        assert_mergeable(self.repo,self.candidate,self.receipt)
        for field,value in [('baseRefName','main'),('headRefOid','b'*40),('statusCheckRollup',[]),('isDraft',True)]:
            candidate=deepcopy(self.candidate);candidate[field]=value
            with self.assertRaises(ValueError):
                assert_mergeable(self.repo,candidate,self.receipt)

    def test_merge_uses_exact_verified_head(self):
        import json
        calls=[]
        def command(args):
            calls.append(args)
            return json.dumps(self.candidate) if 'view' in args else ''
        delivery=GitHubDelivery(self.repo,command=command)
        self.assertEqual(delivery.merge_verified(12,self.receipt),self.head)
        self.assertEqual(calls[-1][-2:],['--match-head-commit',self.head])

    def test_failed_check_is_not_hidden_by_successful_check(self):
        self.candidate['statusCheckRollup'].append({'status':'COMPLETED','conclusion':'FAILURE'})
        with self.assertRaises(ValueError):
            assert_mergeable(self.repo,self.candidate,self.receipt)

    def test_resume_after_merge_does_not_merge_twice(self):
        import json
        from unittest.mock import Mock
        self.candidate.update(state='MERGED',mergeable='UNKNOWN',mergeCommit={'oid':'b'*40})
        command=Mock(return_value=json.dumps(self.candidate))
        delivery=GitHubDelivery(self.repo,command=command)
        self.assertEqual(delivery.merge_verified(12,self.receipt),self.head)
        self.assertEqual(command.call_count,1)

    def test_recurrence_reopens_fingerprinted_issue(self):
        import hashlib,json
        from unittest.mock import Mock
        finding={'scenario':'stream','reproduction':'whitespace stripped'}
        digest=hashlib.sha256(f'{self.repo}\nstream\nwhitespace stripped'.encode()).hexdigest()
        row={'number':7,'state':'CLOSED','body':f'<!-- conveyor-nightly:{digest} -->'}
        command=Mock(side_effect=[json.dumps([row]),''])
        result=GitHubDelivery(self.repo,command=command).issue(finding)
        self.assertEqual(result['number'],7)
        self.assertEqual(result['state'],'OPEN')
        self.assertEqual(command.call_args.args[0][1:3],['issue','reopen'])
