"""Read-only GitHub project progress without a model or coding-agent job."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from personal_tools.base import ToolResult
from personal_tools.github_tools import _github_request
from personal_tools.store import PersonalToolsStore
from redaction import redact_text

_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/(?!\.{1,2}\Z)[A-Za-z0-9_.-]+\Z")


def resolve_repo(settings, selector: str, operator_id: str) -> str:
    selector = selector.strip()
    if _REPO.fullmatch(selector):
        return selector
    store = PersonalToolsStore(settings)
    if selector:
        projects = store.list_project_profiles(operator_id)
        matches = [p for p in projects if str(p.id) == selector or p.name.casefold() == selector.casefold()]
        if not matches:
            default = getattr(settings, 'github_default_repo', '') or ''
            if _REPO.fullmatch(default) and default.split('/')[1].casefold() == selector.casefold():
                return default
        if len(matches) != 1:
            raise ValueError('未找到唯一匹配的项目，请使用项目 ID 或 owner/repo。')
        repo = matches[0].github_repo or ''
    else:
        project = store.get_active_or_first_project(operator_id)
        repo = project.github_repo if project else getattr(settings, 'github_default_repo', '')
    if not _REPO.fullmatch(repo or ''):
        raise ValueError('请提供 GitHub 仓库 owner/repo，或为项目配置仓库。')
    return repo


def _item(row, extra=''):
    title = ' '.join(str(row.get('title', '')).split())[:140]
    return f"• #{row.get('number', '?')} {title}{extra}\n  {row.get('html_url', '')}"


async def project_progress(settings, arg: str, *, operator_id: str = '', **_kw) -> ToolResult:
    try:
        repo = await asyncio.to_thread(resolve_repo, settings, arg or '', operator_id)
    except ValueError as exc:
        return ToolResult(False, str(exc))
    requests = [
        ('待处理 PR（最近更新，最多 5 项）', f'/repos/{repo}/pulls', {'state': 'open', 'sort': 'updated', 'direction': 'desc', 'per_page': 5}),
        ('近期合并（最近更新的 20 个关闭 PR）', f'/repos/{repo}/pulls', {'state': 'closed', 'sort': 'updated', 'direction': 'desc', 'per_page': 20}),
        ('CI（最近 5 次运行，可能包含不同分支）', f'/repos/{repo}/actions/runs', {'per_page': 5}),
        ('待处理 Issue（最近更新，最多 5 项）', f'/repos/{repo}/issues', {'state': 'open', 'sort': 'updated', 'direction': 'desc', 'per_page': 10}),
    ]
    # Fixed fanout; urllib enforces a 15-second timeout per provider request.
    results = await asyncio.gather(*[
        asyncio.to_thread(_github_request, settings, 'GET', endpoint, params)
        for _, endpoint, params in requests
    ], return_exceptions=True)
    lines = [f'项目进度：{repo}', f'查询时间：{datetime.now(timezone.utc).isoformat(timespec="seconds")}', f'https://github.com/{repo}']
    successes = 0
    for index, ((label, _, _), result) in enumerate(zip(requests, results)):
        lines.extend(['', label])
        if isinstance(result, Exception):
            lines.append('⚠️ 读取失败，此部分状态未知。')
            continue
        ok, data = result
        if not ok:
            lines.append('⚠️ 读取失败，此部分状态未知。')
            continue
        rows = data.get('workflow_runs') if index == 2 and isinstance(data, dict) else data
        if not isinstance(rows, list):
            lines.append('⚠️ 返回格式异常，此部分状态未知。')
            continue
        successes += 1
        if index == 1:
            rows = sorted([r for r in rows if r.get('merged_at')], key=lambda r: r['merged_at'], reverse=True)[:5]
        elif index == 3:
            rows = [r for r in rows if 'pull_request' not in r][:5]
        if not rows:
            lines.append('本次查询范围内无记录。')
        for row in rows:
            if index == 2:
                state = row.get('conclusion') or row.get('status', 'unknown')
                lines.append(f"• {row.get('name', 'CI')} · {row.get('head_branch', '?')} · {state}\n  {row.get('html_url', '')}")
            else:
                extra = ' · 草稿' if index == 0 and row.get('draft') else ''
                if index == 1:
                    extra = f" · 合并于 {row['merged_at']}"
                lines.append(_item(row, extra))
    lines.extend(['', '下一步：优先查看失败 CI，再处理待合并 PR 和 Issue。合并记录不代表已部署；此报告不估算完成百分比。'])
    return ToolResult(bool(successes), redact_text('\n'.join(lines)))
