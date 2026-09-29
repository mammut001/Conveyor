#!/usr/bin/env python3
"""setup_wizard_smoke.py — `conveyor setup` contract (env-free, no network).

Drives the wizard with scripted answers (non-TTY mode) and fake live
checks. Pins:
  - .env updates keep comments / unrelated keys, replace in place, append
    new keys, remove keys, quote unsafe values, back up (0600, capped)
  - telegram / email / chat / search flows: format validation, live-check
    failure → re-enter / retry / skip, masked secrets in all output
  - dashboard: required modules first, then the menu; --status / --check
  - restart report written for the `conveyor` wrapper
  - arrow-key menu works on a real pseudo-terminal
  - email sending uses implicit TLS on port 465

Run: .venv/bin/python scripts/setup_wizard_smoke.py
"""
from __future__ import annotations

import io
import os
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.harness_common import CheckResult, print_results  # noqa: E402
from setup_wizard import checks, cli, modules  # noqa: E402
from setup_wizard.envfile import EnvFile, parse  # noqa: E402
from setup_wizard.ui import UI  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="conveyor-setup-smoke-"))
TOKEN = "123456789:AAH-realistic_token_value_abcdefghij"
MAIL_PW = "qqauthcode-secret-9876"


def _ui(*answers: str) -> tuple[UI, io.StringIO]:
    out = io.StringIO()
    return UI(io.StringIO("".join(a + "\n" for a in answers)), out, interactive=False), out


@contextmanager
def fake(**replacements):
    saved = {k: getattr(checks, k) for k in replacements}
    for k, v in replacements.items():
        setattr(checks, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(checks, k, v)


def _env(content: str = "") -> EnvFile:
    path = TMP / f"env-{os.urandom(4).hex()}" / ".env"
    path.parent.mkdir(parents=True)
    if content:
        path.write_text(content, encoding="utf-8")
    return EnvFile(path)


# ---- envfile ---------------------------------------------------------------------


def check_envfile_update():
    ef = _env("# my notes\nTELEGRAM_BOT_TOKEN=old\nOTHER=keep # comment\nCODEX_MODEL=x\n")
    backup = ef.write({"TELEGRAM_BOT_TOKEN": "new", "NEW_KEY": "a b#c"}, {"CODEX_MODEL"})
    text = ef.path.read_text()
    values = parse(text)
    mode = stat.S_IMODE(ef.path.stat().st_mode)
    bmode = stat.S_IMODE(backup.stat().st_mode) if backup else 0
    ok = (
        text.startswith("# my notes\nTELEGRAM_BOT_TOKEN=new\nOTHER=keep # comment\n")
        and "CODEX_MODEL" not in text and 'NEW_KEY="a b#c"' in text
        and values["NEW_KEY"] == "a b#c" and mode == 0o600 and bmode == 0o600
        and "TELEGRAM_BOT_TOKEN=old" in backup.read_text()
    )
    for _ in range(8):
        ef.write({"X": "1"})
    backups = list(ef.path.parent.glob(".env.bak-*"))
    ok = ok and len(backups) == 5
    return CheckResult("envfile: in-place update, removal, quoting, 0600, capped backups", ok, text.replace("\n", "|"))


def check_envfile_dotenv_compatible():
    from dotenv import dotenv_values

    ef = _env()
    ef.write({"A": 'has "quotes" and spaces', "B": "plain", "C": "x#y"})
    loaded = dotenv_values(ef.path)
    ok = loaded == {"A": 'has "quotes" and spaces', "B": "plain", "C": "x#y"}
    return CheckResult("envfile: python-dotenv reads back what we write", ok, f"{loaded}")


# ---- modules ---------------------------------------------------------------------


def check_telegram_flow():
    ef = _env()
    ui, out = _ui(
        "not-a-token",            # format rejected
        TOKEN,                    # accepted
        "2",                      # user id: manual
        "abc", "4242",            # non-digit rejected, then ok
        "y",                      # save
    )
    with fake(telegram_bot=lambda t: checks.Result(True, "@conveyor_bot", {"username": "conveyor_bot"})):
        change = cli.run_module(ui, ef, modules.BY_KEY["telegram"])
    env = ef.read()
    text = out.getvalue()
    ok = (
        env.get("TELEGRAM_BOT_TOKEN") == TOKEN and env.get("TELEGRAM_ALLOWED_USER_ID") == "4242"
        and "格式不像 token" in text and "应该是纯数字" in text
        and TOKEN not in text and "••••" in text and change and "telegram" in change.services
    )
    return CheckResult("telegram: validation, manual id, saved; token never printed", ok, "")


def check_telegram_auto_discovery():
    ef = _env()
    ui, _ = _ui(TOKEN, "1", "y", "y")  # auto-detect, confirm user, save
    with fake(
        telegram_bot=lambda t: checks.Result(True, "@b", {"username": "b"}),
        telegram_find_user=lambda t, **kw: checks.Result(True, "alice (777)", {"user_id": "777"}),
    ):
        cli.run_module(ui, ef, modules.BY_KEY["telegram"])
    return CheckResult("telegram: auto-detects the operator from a DM", ef.read().get("TELEGRAM_ALLOWED_USER_ID") == "777", "")


def check_email_flow_with_reentry():
    ef = _env("GMAIL_BACKEND=\n")
    calls = {"imap": 0}

    def imap(host, port, address, password):
        calls["imap"] += 1
        if password == "wrong":
            return checks.Result(False, "登录被拒")
        return checks.Result(True, "收件箱 3 封")

    ui, out = _ui(
        "2",                          # QQ 邮箱
        "bad-address", "me@qq.com",   # format rejected, then ok
        "wrong",                      # password → imap fails
        "1",                          # 重新输入
        "", MAIL_PW,                  # address kept (default), new password
        "n",                          # no test mail
        "y",                          # save
    )
    with fake(imap_login=imap, smtp_login=lambda *a: checks.Result(True, "smtp.qq.com:465")):
        cli.run_module(ui, ef, modules.BY_KEY["email"])
    env = ef.read()
    text = out.getvalue()
    ok = (
        env.get("GMAIL_ADDRESS") == "me@qq.com" and env.get("GMAIL_APP_PASSWORD") == MAIL_PW
        and env.get("GMAIL_SMTP_HOST") == "smtp.qq.com" and env.get("GMAIL_SMTP_PORT") == "465"
        and env.get("GMAIL_BACKEND") == "imap_smtp" and calls["imap"] == 2
        and "授权码获取" in text and MAIL_PW not in text and "邮箱格式不对" in text
    )
    return CheckResult("email: preset, format check, failed login → re-enter, saved", ok, f"{env}")


def check_chat_flow_retry_and_vision():
    ef = _env()
    attempts = {"n": 0}

    def chat(base, key, model):
        attempts["n"] += 1
        return checks.Result(attempts["n"] > 1, "首字 0.4s" if attempts["n"] > 1 else "HTTP 503")

    ui, _ = _ui("1", "", "sk-deepseek-secret-key", "2", "n", "y")  # DeepSeek, default model, key, retry, no vision, save
    with fake(chat_model=chat):
        cli.run_module(ui, ef, modules.BY_KEY["chat"])
    env = ef.read()
    ok = (
        env.get("CONVEYOR_CHAT_MODE") == "auto" and env.get("CONVEYOR_CHAT_BASE_URL") == "https://api.deepseek.com"
        and env.get("CONVEYOR_CHAT_MODEL") == "deepseek-chat" and env.get("CONVEYOR_CHAT_VISION") == "false"
        and attempts["n"] == 2
    )
    return CheckResult("chat: preset, failed probe → retry, saved with mode=auto", ok, f"{env}")


def check_skip_writes_nothing():
    ef = _env("WEB_SEARCH_BACKEND=disabled\n")
    before = ef.path.read_text()
    ui, out = _ui("1", "brave-key-123456789", "4")  # brave, key, check fails → 跳过
    with fake(web_search=lambda *a: checks.Result(False, "HTTP 401")):
        change = cli.run_module(ui, ef, modules.BY_KEY["search"])
    ok = change is None and ef.path.read_text() == before and "已跳过" in out.getvalue()
    return CheckResult("search: skip after a failed check leaves .env untouched", ok, "")


def check_declined_save():
    ef = _env()
    ui, _ = _ui("y", "", "n")  # enable web console, default port, decline save
    cli.run_module(ui, ef, modules.BY_KEY["web"])
    return CheckResult("web: declining the preview writes nothing", not ef.path.exists(), "")


# ---- cli -------------------------------------------------------------------------


def check_dashboard_required_first_and_restart_file():
    ef = _env()
    restart_file = TMP / "restart.txt"
    os.environ["CONVEYOR_SETUP_RESTART_FILE"] = str(restart_file)
    ws = TMP / "repo"
    ws.mkdir(exist_ok=True)
    answers = [
        TOKEN, "2", "4242", "y",                                  # telegram (required)
        "1", "sk-openai-secret-key-xyz", str(ws), "codex", "", "y",  # codex (required)
        str(len(modules.MODULES) + 3),                            # menu: ✔ 完成
    ]
    ui, out = _ui(*answers)
    try:
        with fake(
            telegram_bot=lambda t: checks.Result(True, "@b", {"username": "b"}),
            codex_workspace=lambda p: checks.Result(True, p),
            codex_binary=lambda p: checks.Result(True, "codex 1.0"),
        ):
            code = cli.main(["--env", str(ef.path)], ui=ui)
    finally:
        os.environ.pop("CONVEYOR_SETUP_RESTART_FILE", None)
    env = ef.read()
    text = out.getvalue()
    ok = (
        code == 0 and env.get("OPENAI_API_KEY") == "sk-openai-secret-key-xyz"
        and env.get("CODEX_WORKSPACE_ROOT") == str(ws)
        and "先完成必填的" in text and "sk-openai-secret-key-xyz" not in text
        and restart_file.read_text().split() == ["telegram", "feishu", "web"]
    )
    return CheckResult("dashboard: required modules first, then menu; restart list written", ok, f"code={code}")


def check_status_and_check_modes():
    ef = _env(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_ALLOWED_USER_ID=1\nWEB_SEARCH_BACKEND=brave\nWEB_SEARCH_API_KEY=k\n")
    ui, out = _ui()
    cli.main(["--status", "--env", str(ef.path)], ui=ui)
    status = out.getvalue()
    ui2, out2 = _ui()
    with fake(
        telegram_bot=lambda t: checks.Result(True, "@b"),
        web_search=lambda *a: checks.Result(False, "HTTP 401"),
    ):
        code = cli.main(["--check", "--env", str(ef.path)], ui=ui2)
    report = out2.getvalue()
    ok = (
        "✅ Telegram" in status and "⬜ 邮箱" in status
        and code == 1 and "HTTP 401" in report and "@b" in report and "邮箱" not in report
    )
    return CheckResult("--status / --check: table, live tests only configured modules, exit code", ok, f"code={code}")


def check_abort_is_clean():
    ef = _env()
    ui, out = _ui()  # EOF right away
    code = cli.main(["telegram", "--env", str(ef.path)], ui=ui)
    ok = code == 130 and "已退出" in out.getvalue() and not ef.path.exists()
    return CheckResult("Ctrl-D / Ctrl-C exits cleanly with code 130, nothing written", ok, f"code={code}")


def check_arrow_menu_on_pty():
    import pty
    import select
    import subprocess

    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from setup_wizard.ui import UI, Option\n"
        "ui = UI()\n"
        "v = ui.select('pick', [Option('a','A'), Option('b','B'), Option('c','C')])\n"
        "print('CHOSEN=' + v)\n" % str(REPO)
    )
    master, slave = pty.openpty()
    proc = subprocess.Popen([sys.executable, "-c", script], stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    buf = b""

    def read_until(marker: bytes, timeout: float = 5.0) -> None:
        nonlocal buf
        import time
        end = time.time() + timeout
        while marker not in buf and time.time() < end:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    buf += os.read(master, 4096)
                except OSError:
                    break

    read_until("回车确认".encode())
    os.write(master, b"\x1b[B")  # down
    os.write(master, b"\x1b[B")  # down
    os.write(master, b"\x1b[A")  # up → B
    os.write(master, b"\r")
    read_until(b"CHOSEN=")
    read_until(b"\n", 2.0)
    proc.wait(timeout=5)
    os.close(master)
    ok = b"CHOSEN=b" in buf
    return CheckResult("ui: arrow keys pick an option on a real pseudo-terminal", ok, buf[-60:].decode(errors="replace"))


def check_smtp_implicit_tls():
    import smtplib
    from unittest.mock import patch

    import config
    from personal_tools import email_smtp

    used: list[str] = []

    class _Fake:
        def __init__(self, *a, **k):
            used.append(self.__class__.__name__)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self):
            used.append("starttls")

        def login(self, *a):
            pass

        def send_message(self, *a):
            pass

    class SSL(_Fake):
        pass

    class PLAIN(_Fake):
        pass

    base = checks.settings_like(gmail_backend="imap_smtp", gmail_address="me@qq.com", gmail_app_password="x",
                                gmail_smtp_host="smtp.qq.com")
    fn = next(getattr(email_smtp, n) for n in dir(email_smtp) if n.startswith("send") and callable(getattr(email_smtp, n)))
    with patch.object(smtplib, "SMTP_SSL", SSL), patch.object(smtplib, "SMTP", PLAIN):
        base.gmail_smtp_port = 465
        r1 = fn(base, "a@b.com", "s", "b")
        first = list(used)
        used.clear()
        base.gmail_smtp_port = 587
        r2 = fn(base, "a@b.com", "s", "b")
    ok = first == ["SSL"] and used == ["PLAIN", "starttls"] and r1.ok and r2.ok
    return CheckResult("email: port 465 uses implicit TLS, 587 STARTTLS", ok, f"{first} {used} {config.__name__}")


def check_chat_side_views():
    import dataclasses

    from config import load_settings
    from personal_tools import setup as setup_tools

    settings = dataclasses.replace(
        load_settings(), gmail_address=None, gmail_app_password=None,
        web_search_backend="brave", web_search_api_key="brave-secret-key-1234567",
        github_token=None,
    )
    status = setup_tools.setup_status(settings, "op").text
    calls: list[str] = []
    with fake(web_search=lambda *a: (calls.append("search"), checks.Result(False, "HTTP 401"))[1],
              telegram_bot=lambda t: (calls.append("tg"), checks.Result(True, "@b"))[1],
              codex_workspace=lambda p: checks.Result(True, p),
              codex_binary=lambda p: checks.Result(True, "codex 1.0"),
              chat_model=lambda *a: checks.Result(True, "ok"),
              feishu=lambda *a: checks.Result(True, "ok"),
              github=lambda *a: checks.Result(True, "ok")):
        offline = setup_tools.setup_check(settings, "op").text
        live = setup_tools.setup_check(settings, "op", live=True).text
    ok = (
        "sudo conveyor setup email" in status and "✅ 联网搜索" in status
        and "brave-secret-key-1234567" not in status + live
        and "连接测试" not in offline and "❌ 联网搜索：HTTP 401" in live and "✅ Telegram 机器人：@b" in live
        and calls.count("search") == 1
    )
    return CheckResult("chat /setup: module checklist with server commands; /setup_check live tests", ok, "")


CHECKS = [
    check_envfile_update,
    check_envfile_dotenv_compatible,
    check_telegram_flow,
    check_telegram_auto_discovery,
    check_email_flow_with_reentry,
    check_chat_flow_retry_and_vision,
    check_skip_writes_nothing,
    check_declined_save,
    check_dashboard_required_first_and_restart_file,
    check_status_and_check_modes,
    check_abort_is_clean,
    check_arrow_menu_on_pty,
    check_smtp_implicit_tls,
    check_chat_side_views,
]


def main() -> int:
    results = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as exc:
            import traceback
            traceback.print_exc()
            results.append(CheckResult(check.__name__, False, f"raised: {exc!r}"))
    print_results(results)
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
