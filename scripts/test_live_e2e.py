import sys
import os
import asyncio
import time
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent.resolve()))

from config import load_settings
from channel.types import InboundMessage
from handlers.tools.observe_tools import (
    exec_desktop_observe_request,
    exec_desktop_observe_status,
    exec_desktop_upload_request,
    exec_desktop_upload_status,
    exec_desktop_upload_cleanup,
)
from handlers.tools.runner import run_tool

class MockPort:
    def __init__(self):
        self.images_sent = []

    async def reply(self, msg, text):
        print(f"[Bot Reply] {text}")

    async def send_image(self, chat_id, image_path, caption=None):
        print(f"[Bot Send Image] chat_id={chat_id}, path={image_path}, caption={caption}")
        self.images_sent.append((chat_id, image_path, caption))

async def main():
    settings = load_settings()
    print("1. Running /nodes status check...")
    nodes_status = await run_tool(settings, "nodes.status", "")
    print(nodes_status)
    if "macbook-payton" not in nodes_status or "online" not in nodes_status:
        print("ERROR: macbook-payton is not online! Please check polling agent.")
        return

    print("\n2. Creating remote observe request...")
    msg = InboundMessage(
        message_id="msg_live_test_001",
        channel="feishu",
        chat_id="chat_live_test",
        operator_id="usr_live_test",
        text="截图看看我电脑现在是什么",
    )
    res_obs = await exec_desktop_observe_request(settings, msg, "截图看看我电脑现在是什么", port=None)
    print(res_obs)
    
    req_id = None
    for line in res_obs.splitlines():
        if line.startswith("Request:"):
            req_id = line.split(":", 1)[1].strip()
            break
    
    if not req_id:
        print("ERROR: Failed to create observe request.")
        return

    print(f"Observe request ID created: {req_id}")
    print("Waiting for Mac agent to poll, claim, capture, and complete observe request (max 15 seconds)...")
    
    completed = False
    for _ in range(15):
        await asyncio.sleep(1.0)
        status_text = await exec_desktop_observe_status(settings, "")
        if f"{req_id}: completed" in status_text:
            print("✓ Observe request completed by Mac agent!")
            print(status_text)
            completed = True
            break
            
    if not completed:
        print("ERROR: Observe request did not complete in time.")
        status_text = await exec_desktop_observe_status(settings, "")
        print(status_text)
        return

    from desktop_observe_requests import load_observe_requests
    store = load_observe_requests(settings)
    req_record = store.get(req_id)
    screenshot_id = None
    if req_record and req_record.get("status") == "completed":
        screenshot_id = req_record.get("result", {}).get("screenshot_id")
    
    if not screenshot_id:
        print("ERROR: Failed to find screenshot ID in completed record.")
        return
    print(f"Screenshot ID: {screenshot_id}")

    print("\n3. Creating manual upload request...")
    res_up = await exec_desktop_upload_request(settings, msg, req_id)
    print(res_up)
    
    upload_id = None
    for line in res_up.splitlines():
        if line.startswith("Upload:"):
            upload_id = line.split(":", 1)[1].strip()
            break
            
    if not upload_id:
        print("ERROR: Failed to create upload request.")
        return
        
    print(f"Upload request ID created: {upload_id}")
    print("Waiting for Mac agent to poll, claim, generate thumbnail, and upload (max 15 seconds)...")
    
    completed_up = False
    port = MockPort()
    for _ in range(15):
        await asyncio.sleep(1.0)
        status_up = await exec_desktop_upload_status(settings, "", port=port, msg=msg)
        if f"{upload_id}: completed" in status_up or f"{upload_id}: delivered" in status_up or port.images_sent:
            print("✓ Upload request completed and delivered!")
            print(status_up)
            completed_up = True
            break
            
    if not completed_up:
        print("ERROR: Upload request did not complete in time.")
        status_up = await exec_desktop_upload_status(settings, "", port=port, msg=msg)
        print(status_up)
        return

    print("\n4. Running cleanup...")
    cleanup_res = await exec_desktop_upload_cleanup(settings, "")
    print(cleanup_res)

if __name__ == "__main__":
    asyncio.run(main())
