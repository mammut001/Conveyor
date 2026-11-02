from __future__ import annotations
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from handlers.intent import project_progress_route
from personal_tools.project_progress import project_progress, resolve_repo


class ProjectProgressTests(unittest.IsolatedAsyncioTestCase):
    def test_routes_only_queries(self):
        for text, arg in [('Conveyor 最近进展如何', 'Conveyor'), ('查看 mammut001/Conveyor 的进度', 'mammut001/Conveyor'), ('/project_progress 12', '12')]:
            self.assertEqual(project_progress_route(text).arg, arg)
        for text in ['删除 Conveyor 项目', '修改项目进度页面', '帮我部署 Conveyor', 'Conveyor 最近进展如何并删除旧文件']:
            self.assertIsNone(project_progress_route(text))

    def test_explicit_unknown_never_uses_default(self):
        settings = SimpleNamespace(github_default_repo='owner/Conveyor')
        with patch('personal_tools.project_progress.PersonalToolsStore') as store:
            store.return_value.list_project_profiles.return_value = []
            self.assertEqual(resolve_repo(settings, 'Conveyor', 'alice'), 'owner/Conveyor')
            with self.assertRaises(ValueError):
                resolve_repo(settings, 'unknown', 'alice')
            store.return_value.list_project_profiles.assert_called_with('alice')

    async def test_partial_failures_and_failed_ci_remain_visible(self):
        settings = SimpleNamespace(github_token='opaque')
        def request(_settings, _method, endpoint, params):
            if endpoint.endswith('/actions/runs'):
                return True, {'workflow_runs': [{'name':'CI', 'conclusion':'failure', 'head_branch':'main', 'html_url':'https://github.com/o/r/actions/runs/1'}]}
            if endpoint.endswith('/issues'):
                return False, 'unavailable'
            if params['state'] == 'closed':
                return True, [{'number':2, 'title':'done', 'merged_at':'2026-11-02T00:00:00Z', 'html_url':'https://github.com/o/r/pull/2'}]
            return True, []
        with patch('personal_tools.project_progress._github_request', side_effect=request):
            result = await project_progress(settings, 'o/r', operator_id='alice')
        self.assertTrue(result.ok)
        self.assertIn('failure', result.text)
        self.assertIn('此部分状态未知', result.text)
        self.assertIn('https://github.com/o/r/pull/2', result.text)
        self.assertIn('合并记录不代表已部署', result.text)
