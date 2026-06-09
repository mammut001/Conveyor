from __future__ import annotations

import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ENV_PATH = Path("/opt/conveyor/.env")
DEFAULT_WORKSPACE = "/srv/codex-telegram-test-repo"
DEFAULT_CODEX_BIN = "/usr/bin/codex"


def prompt_secret(label: str, existing: str | None = None) -> str:
    suffix = " [keep existing]" if existing else ""
    while True:
        value = getpass.getpass(f"{label}{suffix}: ").strip()
        if value:
            return value
        if existing:
            return existing
        print(f"{label} is required.", file=sys.stderr)


def prompt_text(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default or ""


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def bot_api(token: str, method: str) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def discover_user_id(token: str) -> str:
    print()
    print("Send /start to your Telegram bot from the phone account you want to whitelist.")
    print("Waiting for Telegram updates for up to 90 seconds...")
    deadline = time.time() + 90
    seen: set[int] = set()
    while time.time() < deadline:
        try:
            data = bot_api(token, "getUpdates?timeout=10")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Telegram getUpdates failed: {exc}") from exc
        for update in data.get("result", []):
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                seen.add(update_id)
            message = update.get("message") or update.get("edited_message")
            user = (message or {}).get("from") or {}
            user_id = user.get("id")
            if user_id:
                name = user.get("username") or " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
                print(f"Found Telegram user: {name or '(no name)'} ({user_id})")
                if seen:
                    bot_api(token, f"getUpdates?offset={max(seen) + 1}&timeout=1")
                return str(user_id)
    raise RuntimeError("Timed out waiting for /start. Open Telegram, message the bot, then run this script again.")


def validate_token(token: str) -> None:
    data = bot_api(token, "getMe")
    if not data.get("ok"):
        raise RuntimeError("Telegram getMe did not return ok=true")
    bot = data.get("result", {})
    print(f"Telegram bot verified: @{bot.get('username', '(unknown)')}")


def write_env(values: dict[str, str]) -> None:
    lines = [
        f"TELEGRAM_BOT_TOKEN={values['TELEGRAM_BOT_TOKEN']}",
        f"TELEGRAM_ALLOWED_USER_ID={values['TELEGRAM_ALLOWED_USER_ID']}",
        f"CODEX_WORKSPACE_ROOT={values['CODEX_WORKSPACE_ROOT']}",
        f"CODEX_BIN={values['CODEX_BIN']}",
        f"OPENAI_API_KEY={values['OPENAI_API_KEY']}",
        f"MINIMAX_API_KEY={values['MINIMAX_API_KEY']}",
        f"CODEX_TASK_ROOT={values['CODEX_TASK_ROOT']}",
        f"CODEX_TIMEOUT_SECONDS={values['CODEX_TIMEOUT_SECONDS']}",
        f"TELEGRAM_PROGRESS_SECONDS={values['TELEGRAM_PROGRESS_SECONDS']}",
        "",
    ]
    ENV_PATH.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(ENV_PATH, 0o600)


def main() -> int:
    existing = read_env()
    token = prompt_secret("Telegram bot token", existing.get("TELEGRAM_BOT_TOKEN"))
    validate_token(token)

    allowed_user_id = prompt_text("Telegram allowed user id, or press Enter to discover", existing.get("TELEGRAM_ALLOWED_USER_ID"))
    if not allowed_user_id:
        allowed_user_id = discover_user_id(token)

    openai_api_key = existing.get("OPENAI_API_KEY", "")
    minimax_api_key = existing.get("MINIMAX_API_KEY", "")
    default_provider = "minimax" if minimax_api_key or not openai_api_key else "openai"
    provider = prompt_text("Provider for Codex auth: openai or minimax", default_provider).lower()
    if provider == "minimax":
        minimax_api_key = prompt_secret("MiniMax API key for Codex", minimax_api_key)
    elif provider == "openai":
        openai_api_key = prompt_secret("OpenAI API key for Codex", openai_api_key)
    else:
        raise RuntimeError("Provider must be openai or minimax.")
    workspace = prompt_text("Codex workspace git repo", existing.get("CODEX_WORKSPACE_ROOT", DEFAULT_WORKSPACE))
    codex_bin = prompt_text("Codex binary", existing.get("CODEX_BIN", DEFAULT_CODEX_BIN))

    values = {
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_ALLOWED_USER_ID": allowed_user_id,
        "CODEX_WORKSPACE_ROOT": workspace,
        "CODEX_BIN": codex_bin,
        "OPENAI_API_KEY": openai_api_key,
        "MINIMAX_API_KEY": minimax_api_key,
        "CODEX_TASK_ROOT": existing.get("CODEX_TASK_ROOT", "/srv/conveyor"),
        "CODEX_TIMEOUT_SECONDS": existing.get("CODEX_TIMEOUT_SECONDS", "3600"),
        "TELEGRAM_PROGRESS_SECONDS": existing.get("TELEGRAM_PROGRESS_SECONDS", "20"),
    }
    write_env(values)
    print(f"Wrote {ENV_PATH} with mode 600.")
    print("Next: run scripts/healthcheck.sh, then sudo systemctl enable --now codex-telegram-bot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
