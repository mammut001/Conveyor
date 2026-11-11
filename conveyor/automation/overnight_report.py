"""Human-readable bounded overnight run outcomes with durable evidence links."""
import json
from datetime import datetime,timezone


def format_run(run,repo='mammut001/Conveyor'):
    labels={'running':'运行中','completed':'已完成','cancelled':'已停止','expired':'已到时限','blocked':'需要处理阻塞',
            'queued':'等待执行','passed':'通过'}
    roles={'discover':'检查','repair':'修复','verify':'独立验收'}
    lines=[f"夜间任务：{run['id']}",f"状态：{labels.get(run['state'],run['state'])}",f"目标：{run['objective']}",
           '交付：preview 分支和独立预览环境','验收范围：已配置的页面、健康、会话及独立源码检查。']
    if run.get('deadline'):
        lines.append('运行截止：'+datetime.fromtimestamp(run['deadline'],timezone.utc).isoformat(timespec='seconds'))
    tasks=run.get('tasks',[])
    if run.get('max_tasks'):
        lines.append(f"任务额度：{len(tasks)}/{run['max_tasks']}；单个问题最多修复 3 次。")
    links=set();commits=set();failures=set();summary={};plans={}
    for task in tasks:
        key=(task['role'],task['state']);summary[key]=summary.get(key,0)+1
        try:
            result=json.loads(task.get('result') or '{}')
        except (ValueError,TypeError):
            continue
        if not isinstance(result,dict):
            continue
        plan=result.get('acceptance_plan')
        if isinstance(plan,dict) and isinstance(plan.get('sha256'),str):
            plans[plan['sha256']]=plan.get('scenarios',[])
        for number in result.get('issues',[]):
            if isinstance(number,int): links.add(f'https://github.com/{repo}/issues/{number}')
        issue=result.get('issue') or {}
        if isinstance(issue,dict) and isinstance(issue.get('number'),int):
            links.add(f"https://github.com/{repo}/issues/{issue['number']}")
        for item in (result.get('candidate'),result.get('receipt')):
            if isinstance(item,dict) and isinstance(item.get('pr'),int):
                links.add(f"https://github.com/{repo}/pull/{item['pr']}")
        deployment=result.get('deployment') or {}
        if isinstance(deployment,dict) and deployment.get('healthy') and deployment.get('commit'):
            commits.add(deployment['commit'])
        if result.get('error'):
            failures.add(str(result['error']))
        if result.get('verification_failed'):
            failures.add('独立验收未通过，已安排下一次修复')
    for (role,state),count in summary.items():
        lines.append(f"• {roles.get(role,role)}：{labels.get(state,state)} × {count}")
    for digest,scenarios in plans.items():
        lines.append('目标验收计划：'+digest)
        for scenario in scenarios:
            if isinstance(scenario,str):
                lines.append('• '+scenario)
    lines.extend(sorted(links))
    for commit in sorted(commits):
        lines.append(f'已验证预览版本：https://github.com/{repo}/commit/{commit}')
    for reason in sorted(failures):
        lines.append('检查记录：'+reason)
    if run['state']=='completed':
        lines.append('已配置的验收检查连续两轮通过；这不代表网站不存在其他问题。')
    lines.extend([f"查看：/nightly_status {run['id']}",f"停止：/nightly_cancel {run['id']}"])
    return '\n'.join(lines)
