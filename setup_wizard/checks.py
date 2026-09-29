"""setup_wizard/checks.py — live verification for each integration.

Every check returns ``Result(ok, detail)`` and never raises for network or
auth failures; details are redacted so secrets never reach the terminal.
"""
from __future__ import annotations

import asyncio
import dataclasses
import imaplib
import json
import shutil
import smtplib
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

TIMEOUT = 20


@dataclass(frozen=True)
class Result:
    ok: bool
    detail: str = ""
    data: dict | None = None


def _redact(text: str) -> str:
    try:
        from redaction import redact_text

        return redact_text(text)
    except Exception:  # pragma: no cover - redaction always importable in repo
        return text


def _error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    return _redact(str(exc) or exc.__class__.__name__)[:200]


def _http_json(url: str, *, headers: dict | None = None, body: dict | None = None,
               timeout: int = TIMEOUT) -> tuple[dict, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "conveyor-setup",
        **({"Content-Type": "application/json"} if data else {}),
        **(headers or {}),
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8") or "{}")
        return payload, dict(resp.headers)


def settings_like(**overrides: Any) -> SimpleNamespace:
    """A Settings stand-in with dataclass defaults, for reusing app code
    (search, chat client) before a full configuration exists."""
    from config import Settings

    values: dict[str, Any] = {}
    for f in dataclasses.fields(Settings):
        if f.default is not dataclasses.MISSING:
            values[f.name] = f.default
        elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            values[f.name] = f.default_factory()  # type: ignore[misc]
    values.update(overrides)
    return SimpleNamespace(**values)


# ---- Telegram ---------------------------------------------------------------


def telegram_bot(token: str) -> Result:
    try:
        payload, _ = _http_json(f"https://api.telegram.org/bot{token}/getMe")
    except Exception as exc:
        return Result(False, "token 无效或网络不通（" + _error(exc).replace(token, "***") + "）")
    if not payload.get("ok"):
        return Result(False, "Telegram 拒绝了这个 token")
    bot = payload.get("result") or {}
    return Result(True, f"@{bot.get('username', '?')}", {"username": bot.get("username", "")})


def telegram_find_user(token: str, *, wait_seconds: int = 90,
                       tick: Callable[[int], None] | None = None) -> Result:
    """Wait for the operator to message the bot; return their user id."""
    deadline = time.time() + wait_seconds
    offset = None
    while time.time() < deadline:
        if tick:
            tick(int(deadline - time.time()))
        query = "getUpdates?timeout=5" + (f"&offset={offset}" if offset else "")
        try:
            payload, _ = _http_json(f"https://api.telegram.org/bot{token}/{query}", timeout=15)
        except Exception as exc:
            return Result(False, "读取消息失败（" + _error(exc).replace(token, "***") + "）")
        for update in payload.get("result", []):
            offset = int(update.get("update_id", 0)) + 1
            message = update.get("message") or update.get("edited_message") or {}
            user = message.get("from") or {}
            if user.get("id") and message.get("chat", {}).get("type") == "private":
                name = user.get("username") or " ".join(
                    filter(None, [user.get("first_name"), user.get("last_name")]))
                if offset:
                    try:  # acknowledge so the bot does not replay /start later
                        _http_json(f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout=0")
                    except Exception:
                        pass
                return Result(True, f"{name or '(无名)'} ({user['id']})", {"user_id": str(user["id"])})
    return Result(False, f"{wait_seconds} 秒内没收到私聊消息")


# ---- Codex -------------------------------------------------------------------


def codex_workspace(path: str) -> Result:
    root = Path(path).expanduser()
    if not root.is_dir():
        return Result(False, "目录不存在")
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:
        return Result(False, _error(exc))
    if top.returncode != 0:
        return Result(False, "不是 git 仓库（Codex 任务需要在 git 仓库里开工作区）")
    if Path(top.stdout.strip()).resolve() != root.resolve():
        return Result(False, f"需要仓库根目录：{top.stdout.strip()}")
    return Result(True, str(root.resolve()))


def codex_binary(path: str) -> Result:
    resolved = shutil.which(path) or (path if Path(path).is_file() else None)
    if not resolved:
        return Result(False, "找不到 codex 程序（npm i -g @openai/codex）")
    try:
        out = subprocess.run([resolved, "--version"], capture_output=True, text=True, timeout=15)
        version = (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else ""
    except Exception as exc:
        return Result(False, _error(exc))
    return Result(True, version or resolved)


# ---- chat tier ---------------------------------------------------------------


def chat_model(base_url: str, api_key: str, model: str) -> Result:
    from runner.chat_client import ChatConfig, ChatError, stream_chat

    config = ChatConfig(base_url=base_url.rstrip("/"), api_key=api_key, model=model,
                        timeout=TIMEOUT, max_tokens=20, temperature=0)
    messages = [{"role": "user", "content": "Reply with exactly: OK"}]
    started = time.monotonic()
    first: list[float] = []

    async def run() -> str:
        out = ""
        async for chunk in stream_chat(config, messages):
            if not first:
                first.append(time.monotonic() - started)
            out += chunk
        return out

    try:
        reply = asyncio.run(run()).strip()
    except ChatError as exc:
        return Result(False, _redact(str(exc)).replace(api_key, "***"))
    except Exception as exc:
        return Result(False, _error(exc).replace(api_key, "***"))
    if not reply:
        return Result(False, "模型返回了空内容")
    latency = f"首字 {first[0]:.1f}s" if first else ""
    return Result(True, f"{latency}，回复「{reply[:30]}」".lstrip("，"))


# ---- web search --------------------------------------------------------------


def web_search(backend: str, api_key: str, endpoint: str) -> Result:
    from personal_tools.web_search import search_web

    settings = settings_like(
        web_search_backend=backend,
        web_search_api_key=api_key or None,
        web_search_endpoint=endpoint or None,
    )
    try:
        results, err = search_web(settings, "Conveyor open source agent", 3)
    except Exception as exc:
        return Result(False, _error(exc))
    if err:
        return Result(False, _redact(err).replace(api_key or "\0", "***"))
    if not results:
        return Result(False, "搜索成功但没有结果")
    return Result(True, f"{len(results)} 条结果，例如「{results[0].title[:40]}」")


# ---- email -------------------------------------------------------------------


def imap_login(host: str, port: int, address: str, password: str) -> Result:
    try:
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT)
    except TypeError:  # Python < 3.9 has no timeout argument
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context())
    except Exception as exc:
        return Result(False, f"连不上 {host}:{port}（{_error(exc)}）")
    try:
        conn.login(address, password)
        status, data = conn.select("INBOX", readonly=True)
        count = data[0].decode() if status == "OK" and data and data[0] else "?"
        return Result(True, f"收件箱 {count} 封")
    except imaplib.IMAP4.error:
        return Result(False, "登录被拒：地址或授权码不对，或没开启 IMAP")
    except Exception as exc:
        return Result(False, _error(exc).replace(password, "***"))
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _smtp(host: str, port: int) -> smtplib.SMTP:
    context = ssl.create_default_context()
    if port == 465:
        return smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=context)
    server = smtplib.SMTP(host, port, timeout=TIMEOUT)
    server.ehlo()
    server.starttls(context=context)
    server.ehlo()
    return server


def smtp_login(host: str, port: int, address: str, password: str) -> Result:
    try:
        server = _smtp(host, port)
    except Exception as exc:
        return Result(False, f"连不上 {host}:{port}（{_error(exc)}；很多 VPS 封了 25 端口）")
    try:
        server.login(address, password)
        return Result(True, f"{host}:{port}")
    except smtplib.SMTPAuthenticationError:
        return Result(False, "登录被拒：地址或授权码不对，或没开启 SMTP")
    except Exception as exc:
        return Result(False, _error(exc).replace(password, "***"))
    finally:
        try:
            server.quit()
        except Exception:
            pass


def send_test_mail(host: str, port: int, address: str, password: str) -> Result:
    msg = EmailMessage()
    msg["From"] = address
    msg["To"] = address
    msg["Subject"] = "Conveyor 邮箱配置测试"
    msg.set_content("这是一封由 conveyor setup 发送的测试邮件。收到说明发信配置正常。")
    try:
        server = _smtp(host, port)
        try:
            server.login(address, password)
            server.send_message(msg)
        finally:
            server.quit()
    except Exception as exc:
        return Result(False, _error(exc).replace(password, "***"))
    return Result(True, f"已发到 {address}")


# ---- GitHub ------------------------------------------------------------------


def github(token: str, repo: str = "", api_base: str = "https://api.github.com") -> Result:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        user, resp_headers = _http_json(f"{api_base.rstrip('/')}/user", headers=headers)
    except Exception as exc:
        return Result(False, "token 无效（" + _error(exc) + "）")
    scopes = resp_headers.get("X-OAuth-Scopes") or resp_headers.get("x-oauth-scopes") or ""
    detail = f"@{user.get('login', '?')}"
    if scopes:
        detail += f"，权限：{scopes}"
    if repo:
        try:
            info, _ = _http_json(f"{api_base.rstrip('/')}/repos/{repo}", headers=headers)
        except Exception as exc:
            return Result(False, f"{detail}，但访问不了 {repo}（{_error(exc)}）")
        detail += f"，仓库 {info.get('full_name', repo)} 可访问"
    return Result(True, detail)


# ---- Feishu / Lark -------------------------------------------------------------

FEISHU_DOMAINS = {"feishu": "https://open.feishu.cn", "lark": "https://open.larksuite.com"}


def feishu(app_id: str, app_secret: str, domain: str = "feishu") -> Result:
    base = FEISHU_DOMAINS.get(domain, FEISHU_DOMAINS["feishu"])
    try:
        payload, _ = _http_json(
            f"{base}/open-apis/auth/v3/tenant_access_token/internal",
            body={"app_id": app_id, "app_secret": app_secret},
        )
    except Exception as exc:
        return Result(False, _error(exc))
    if payload.get("code") != 0:
        return Result(False, f"凭证被拒（{payload.get('msg', 'code ' + str(payload.get('code')))}）")
    return Result(True, "拿到了访问凭证")
