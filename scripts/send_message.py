#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from scripts.telegram_api import send_message


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a Telegram message through the configured bot.")
    parser.add_argument("message", nargs="+", help="Message text to send")
    parser.add_argument("--chat-id", type=int, default=None, help="Override TELEGRAM_ALLOWED_USER_ID")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    args = parser.parse_args()

    settings = load_settings(args.env)
    send_message(settings, " ".join(args.message), args.chat_id)
    print("sent")


if __name__ == "__main__":
    main()
