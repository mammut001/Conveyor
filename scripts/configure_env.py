from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


CONVEYOR_DIR = Path(os.environ.get("CONVEYOR_DIR", "/opt/conveyor")).expanduser().resolve()
ENV_PATH = CONVEYOR_DIR / ".env"
DEFAULT_WORKSPACE = "/srv/codex-telegram-test-repo"
DEFAULT_CODEX_BIN = shutil.which("codex") or "/usr/bin/codex"


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
                name = user.get("username") or " ".join(
                    filter(None, [user.get("first_name"), user.get("last_name")])
                )
                print(f"Found Telegram user: {name or '(no name)'} ({user_id})")
                if seen:
                    bot_api(token, f"getUpdates?offset={max(seen) + 1}&timeout=1")
                return str(user_id)
    raise RuntimeError(
        "Timed out waiting for /start. Open Telegram, message the bot, then run this script again."
    )


def validate_token(token: str) -> None:
    data = bot_api(token, "getMe")
    if not data.get("ok"):
        raise RuntimeError("Telegram getMe did not return ok=true")
    bot = data.get("result", {})
    print(f"Telegram bot verified: @{bot.get('username', '(unknown)')}")


def _managed_lines(values: dict[str, str]) -> dict[str, str]:
    managed = {
        "TELEGRAM_BOT_TOKEN": values["TELEGRAM_BOT_TOKEN"],
        "TELEGRAM_ALLOWED_USER_ID": values["TELEGRAM_ALLOWED_USER_ID"],
        "CODEX_WORKSPACE_ROOT": values["CODEX_WORKSPACE_ROOT"],
        "CODEX_BIN": values["CODEX_BIN"],
        "OPENAI_API_KEY": values["OPENAI_API_KEY"],
        "MINIMAX_API_KEY": values["MINIMAX_API_KEY"],
        "CODEX_TASK_ROOT": values["CODEX_TASK_ROOT"],
        "CODEX_TIMEOUT_SECONDS": values["CODEX_TIMEOUT_SECONDS"],
        "TELEGRAM_PROGRESS_SECONDS": values["TELEGRAM_PROGRESS_SECONDS"],
    }
    if "CONVEYOR_HANDOFF_TAILSCALE_SERVE" in values:
        managed["CONVEYOR_HANDOFF_TAILSCALE_SERVE"] = values["CONVEYOR_HANDOFF_TAILSCALE_SERVE"]
    return managed


def get_tailscale_status() -> dict:
    if not shutil.which("tailscale"):
        return {}
    try:
        proc = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout)
    except Exception:
        pass
    return {}


def configure_tailscale(existing: dict[str, str]) -> dict[str, str]:
    print()
    print("=" * 60)
    print("Tailscale 远程安全访问配置（手机免 SSH 登录控制台与人工接管）")
    print("=" * 60)

    if not shutil.which("tailscale"):
        print("未检测到 Tailscale 客户端，跳过 Tailnet 配置。")
        return {}

    status = get_tailscale_status()
    backend_state = status.get("BackendState", "")

    if backend_state != "Running":
        print("当前节点尚未登录接入 Tailnet。")
        login_choice = prompt_text("是否现在登录 Tailscale？(Y/n)", "y").lower()
        if login_choice in ("y", "yes", ""):
            print("\n请使用手机相机扫描终端二维码，或在浏览器中打开链接完成设备授权：\n")
            try:
                subprocess.run(
                    ["tailscale", "up", "--qr", "--accept-routes"],
                    timeout=180,
                )
            except Exception as exc:
                print(f"Tailscale 登录交互异常: {exc}", file=sys.stderr)
            status = get_tailscale_status()

    dns_name = (status.get("Self") or {}).get("DNSName", "").rstrip(".")
    if dns_name:
        print(f"\nTailnet 节点已连接: {dns_name}")
        default_serve = existing.get("CONVEYOR_HANDOFF_TAILSCALE_SERVE", "1")
        enable_serve = prompt_text(
            "是否开启手机接管 Tailscale 路由 (CONVEYOR_HANDOFF_TAILSCALE_SERVE)? (Y/n)",
            "y" if default_serve in ("1", "true") else "n",
        ).lower()
        serve_val = "1" if enable_serve in ("y", "yes", "") else "0"
        return {"CONVEYOR_HANDOFF_TAILSCALE_SERVE": serve_val}

    print("Tailscale 暂未接入，可后续运行 `sudo tailscale up` 进行登录。")
    return {"CONVEYOR_HANDOFF_TAILSCALE_SERVE": existing.get("CONVEYOR_HANDOFF_TAILSCALE_SERVE", "0")}


def write_env(values: dict[str, str]) -> None:
    """Update managed keys while preserving unrelated Conveyor settings/comments."""
    managed = _managed_lines(values)
    output: list[str] = []
    seen: set[str] = set()

    if ENV_PATH.exists():
        for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in managed:
                    output.append(f"{key}={managed[key]}")
                    seen.add(key)
                    continue
            output.append(raw_line)

    if output and output[-1].strip():
        output.append("")
    for key, value in managed.items():
        if key not in seen:
            output.append(f"{key}={value}")
    output.append("")

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(output), encoding="utf-8")
    os.chmod(ENV_PATH, 0o600)


def main() -> int:
    existing = read_env()
    token = prompt_secret("Telegram bot token", existing.get("TELEGRAM_BOT_TOKEN"))
    validate_token(token)

    allowed_user_id = prompt_text(
        "Telegram allowed user id, or press Enter to discover",
        existing.get("TELEGRAM_ALLOWED_USER_ID"),
    )
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

    workspace = prompt_text(
        "Codex workspace git repo",
        existing.get("CODEX_WORKSPACE_ROOT", DEFAULT_WORKSPACE),
    )
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
    tailscale_values = configure_tailscale(existing)
    values.update(tailscale_values)

    write_env(values)
    print(f"Wrote {ENV_PATH} with mode 600.")
    print("Next: run conveyor doctor, then sudo conveyor restart all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
