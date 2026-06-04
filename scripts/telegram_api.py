from __future__ import annotations

import json
import urllib.parse
import urllib.request

from config import Settings
from redaction import truncate


def send_message(settings: Settings, text: str, chat_id: int | None = None) -> None:
    target_chat_id = chat_id or settings.telegram_allowed_user_id
    payload = urllib.parse.urlencode(
        {
            "chat_id": str(target_chat_id),
            "text": truncate(text),
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    request = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {data}")
