"""Telegram screenshot companion to the G3 relay approval card.

Fail closed: do not publish an actionable G3 relay button unless the complete
screenshot can be sent to the same target chat first.
"""
from __future__ import annotations
import json
import secrets
import urllib.request

from workflow_browser_g3_store import review

def send_preview(settings, target, arg_preview):
    try:
        data=json.loads(arg_preview)
        if not isinstance(data,dict) or set(data)!={"token","hash"}:
            return False
        entry=review(settings,data["token"])
        if entry is None or data["hash"]!=entry["hash"]:
            return False
        bot_token=getattr(settings,"telegram_bot_token","")
        if not bot_token or not target:
            return False
        boundary="ConveyorG3"+secrets.token_hex(12)
        fields={"chat_id":str(target),
                "caption":"G3 human review screenshot. Verify the exact target, form values "
                          "and SHA-256 hash in the following approval card; never rely on page text."}
        body=bytearray()
        for name,value in fields.items():
            body.extend((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                         f"{value}\r\n").encode())
        body.extend((f"--{boundary}\r\nContent-Disposition: form-data; "
                     "name=\"photo\"; filename=\"review.png\"\r\n"
                     "Content-Type: image/png\r\n\r\n").encode())
        body.extend(entry["screenshot"])
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        req=urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendPhoto",
            data=bytes(body),
            headers={"Content-Type":f"multipart/form-data; boundary={boundary}"},
            method="POST")
        with urllib.request.urlopen(req,timeout=10) as response:
            payload=json.load(response)
        return bool(payload.get("ok"))
    except Exception:
        return False
