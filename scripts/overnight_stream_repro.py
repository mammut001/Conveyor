#!/usr/bin/env python3
"""Immutable acceptance probe for Web streaming Markdown whitespace.

Run with the candidate repository as cwd. The probe itself must be provided
by the coordinator, outside the repair worktree, so the agent cannot edit it.
"""
import asyncio
import queue
import sys
from pathlib import Path

sys.path.insert(0,str(Path.cwd()))
from conveyor.web.web_chat import WebChatPort


async def main():
    events=queue.Queue()
    port=WebChatPort(events,None,'')
    expected='    print("hello")\n\n'
    await port.edit_progress(None,'probe',expected+' ▍')
    kind,payload=events.get_nowait()
    assert kind=='delta', 'stream update must remain a delta'
    assert payload['text']==expected, 'Web delta stripped Markdown indentation or trailing newlines'
    print('stream Markdown whitespace contract: PASS')


if __name__=='__main__':
    asyncio.run(main())
