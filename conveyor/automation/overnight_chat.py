"""Explicit preview-only chat entry points for bounded nightly work."""
import asyncio
import os
import re
from conveyor.automation.overnight_control import client


def route(text):
    from handlers.intent import RouteResult
    body=(text or '').strip()
    patterns=[('start',r'(?:/nightly_start|启动夜间任务)[：: ]+(.+)'),
              ('status',r'(?:/nightly_status|夜间任务状态)[：: ]+([0-9a-f]{32})'),
              ('cancel',r'(?:/nightly_cancel|停止夜间任务)[：: ]+([0-9a-f]{32})')]
    for action,pattern in patterns:
        match=re.fullmatch(pattern,body,re.DOTALL)
        if match:
            return RouteResult(kind='deterministic',tools=('nightly.'+action,),arg=match.group(1))
    return None


async def invoke(action,arg,*,operator_id='',channel='',**_kw):
    from personal_tools.base import ToolResult
    if os.getenv('CONVEYOR_NIGHTLY_CONTROL') != '1' or channel != 'web' or operator_id != 'web-console':
        return ToolResult(False,'夜间任务控制仅在已启用的预览工作台中开放。')
    request={'action':action}
    request['objective' if action=='start' else 'run_id']=arg.strip()
    try:
        run=await asyncio.to_thread(client,request)
    except (OSError,RuntimeError,ValueError):
        return ToolResult(False,'夜间任务控制暂不可用，请检查控制服务；未确认任务已经启动。')
    from conveyor.automation.overnight_report import format_run
    return ToolResult(True,format_run(run))


async def start(settings,arg,**kw):
    return await invoke('start',arg,**kw)


async def status(settings,arg,**kw):
    return await invoke('status',arg,**kw)


async def cancel(settings,arg,**kw):
    return await invoke('cancel',arg,**kw)
