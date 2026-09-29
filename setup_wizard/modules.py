"""setup_wizard/modules.py — one interactive flow per integration.

Each module knows its `.env` keys, reports a status from the current values,
and runs an interactive flow that verifies the input live before returning
the changes to write. Checks live in ``setup_wizard.checks`` so tests can
replace them.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Callable

from setup_wizard import checks
from setup_wizard.ui import UI, Option, mask

OK, PARTIAL, MISSING = "ok", "partial", "missing"
STATUS_ICON = {OK: "✅", PARTIAL: "🟡", MISSING: "⬜"}
SECRET_HINTS = ("TOKEN", "KEY", "SECRET", "PASSWORD")


def is_secret(key: str) -> bool:
    return any(h in key.upper() for h in SECRET_HINTS)


@dataclass
class Change:
    updates: dict[str, str] = field(default_factory=dict)
    removals: set[str] = field(default_factory=set)
    services: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.updates or self.removals)


@dataclass(frozen=True)
class Module:
    key: str
    title: str
    blurb: str
    required: bool
    status: Callable[[dict[str, str]], tuple[str, str]]
    run: Callable[[UI, dict[str, str]], Change | None]
    verify: Callable[[dict[str, str]], checks.Result | None]


def _retry(ui: UI, text: str, check: Callable[[], checks.Result]) -> checks.Result | None:
    """Run a check with a spinner; on failure offer retry / re-enter / skip.

    Returns the passing result, or None when the operator wants to re-enter
    the values (caller loops). Raises Skip when they give up on the module.
    """
    while True:
        with ui.spinner(text) as step:
            result = check()
            step.done(result.ok, result.detail)
        if result.ok:
            return result
        choice = ui.select("怎么办？", [
            Option("edit", "重新输入"),
            Option("retry", "再试一次", "网络抖动时用"),
            Option("save", "先保存不验证", "确定信息没错、只是现在连不上时用"),
            Option("skip", "跳过这个模块", "不做任何修改"),
        ])
        if choice == "retry":
            continue
        if choice == "save":
            return checks.Result(True, "未验证")
        if choice == "skip":
            raise Skip()
        return None


class Skip(Exception):
    pass


# ---- telegram ------------------------------------------------------------------


def _telegram_status(env: dict[str, str]) -> tuple[str, str]:
    token, uid = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_ALLOWED_USER_ID")
    if token and uid and uid != "0":
        return OK, f"用户 {uid}"
    if token:
        return PARTIAL, "还缺你的 Telegram 用户 ID"
    return MISSING, "还没配置"


def _telegram_run(ui: UI, env: dict[str, str]) -> Change | None:
    ui.hint("1. 在 Telegram 打开 @BotFather，发 /newbot，按提示起名\n"
            "2. 它会回你一串形如 123456:ABC-DEF… 的 token，复制过来")
    while True:
        token = ui.secret("机器人 token", env.get("TELEGRAM_BOT_TOKEN"))
        if not re.match(r"^\d+:[\w-]{20,}$", token):
            ui.fail("格式不像 token（应为 数字:字母数字串）")
            continue
        bot = _retry(ui, "验证 token", lambda: checks.telegram_bot(token))
        if bot is not None:
            break
    username = (bot.data or {}).get("username", "")
    current = env.get("TELEGRAM_ALLOWED_USER_ID", "")
    options = [Option("auto", "自动识别", f"用手机给 @{username or '你的机器人'} 发一条消息")]
    if current and current != "0":
        options.insert(0, Option("keep", f"保留当前 ID {current}"))
    options.append(Option("manual", "手动输入", "可以问 @userinfobot 要"))
    how = ui.select("只允许谁使用这个机器人？", options)
    if how == "keep":
        uid = current
    elif how == "manual":
        uid = ui.ask("你的 Telegram 用户 ID", required=True,
                     validate=lambda v: None if v.isdigit() else "应该是纯数字")
    else:
        uid = ""
        while not uid:
            ui.info(f"现在用手机给 @{username} 发任意一条私聊消息（例如 /start），我等 90 秒…")
            with ui.spinner("等待你的消息") as step:
                found = checks.telegram_find_user(token)
                step.done(found.ok, found.detail)
            if found.ok:
                uid = (found.data or {})["user_id"]
                if not ui.confirm(f"只允许 {found.detail} 使用？"):
                    uid = ""
            elif not ui.confirm("没收到，再等一次？"):
                uid = ui.ask("那就手动输入用户 ID", required=True,
                             validate=lambda v: None if v.isdigit() else "应该是纯数字")
    return Change(
        {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_ALLOWED_USER_ID": uid},
        services=("telegram",),
    )


def _telegram_verify(env: dict[str, str]) -> checks.Result | None:
    token = env.get("TELEGRAM_BOT_TOKEN")
    return checks.telegram_bot(token) if token else None


# ---- codex -----------------------------------------------------------------------


def codex_logged_in(env: dict[str, str]) -> bool:
    """`codex login` (ChatGPT account) stores credentials in CODEX_HOME."""
    import os
    from pathlib import Path

    home = env.get("CODEX_HOME") or os.environ.get("CODEX_HOME") or "~/.codex"
    return (Path(home).expanduser() / "auth.json").is_file()


def _codex_status(env: dict[str, str]) -> tuple[str, str]:
    has_key = env.get("OPENAI_API_KEY") or env.get("MINIMAX_API_KEY") or codex_logged_in(env)
    ws = env.get("CODEX_WORKSPACE_ROOT")
    if has_key and ws:
        if env.get("OPENAI_API_KEY"):
            provider = "OpenAI"
        elif env.get("MINIMAX_API_KEY"):
            provider = "MiniMax"
        else:
            provider = "codex login"
        return OK, f"{provider} · {ws}"
    if has_key or ws:
        return PARTIAL, "缺模型密钥或工作目录"
    return MISSING, "还没配置"


def _codex_run(ui: UI, env: dict[str, str]) -> Change | None:
    ui.hint("Codex 是真正动手的 Agent：改代码、跑命令都靠它。")
    options = [
        Option("openai", "OpenAI API key", "platform.openai.com"),
        Option("minimax", "MiniMax API key", "platform.minimaxi.com"),
        Option("login", "已用 codex login 登录", "ChatGPT 账号，不需要 key"),
    ]
    default = 1 if env.get("MINIMAX_API_KEY") and not env.get("OPENAI_API_KEY") else 0
    if not (env.get("OPENAI_API_KEY") or env.get("MINIMAX_API_KEY")) and codex_logged_in(env):
        default = 2
    provider = ui.select("Codex 怎么访问模型？", options, default=default)
    updates: dict[str, str] = {}
    if provider == "login":
        if not codex_logged_in(env):
            ui.warn("没找到 Codex 登录信息（~/.codex/auth.json）。请用服务账号运行 codex login 后再试。")
    else:
        key_name = "OPENAI_API_KEY" if provider == "openai" else "MINIMAX_API_KEY"
        updates[key_name] = ui.secret(
            f"{'OpenAI' if provider == 'openai' else 'MiniMax'} API key", env.get(key_name))
    ws = env.get("CODEX_WORKSPACE_ROOT")
    while True:
        ws = ui.ask("Codex 工作的 git 仓库（根目录）", ws, required=True)
        if _retry(ui, "检查仓库", lambda: checks.codex_workspace(ws)) is not None:
            break
    updates["CODEX_WORKSPACE_ROOT"] = ws
    binary = env.get("CODEX_BIN") or "codex"
    while True:
        binary = ui.ask("codex 程序", binary, required=True)
        if _retry(ui, "检查 codex 程序", lambda: checks.codex_binary(binary)) is not None:
            break
    updates["CODEX_BIN"] = binary
    model = ui.ask("指定模型（留空用 Codex 默认）", env.get("CODEX_MODEL"))
    change = Change(updates, services=("telegram", "feishu", "web"))
    if model:
        change.updates["CODEX_MODEL"] = model
    elif env.get("CODEX_MODEL"):
        change.removals.add("CODEX_MODEL")
    return change


def _codex_verify(env: dict[str, str]) -> checks.Result | None:
    ws = env.get("CODEX_WORKSPACE_ROOT")
    if not ws:
        return None
    result = checks.codex_workspace(ws)
    if not result.ok:
        return result
    return checks.codex_binary(env.get("CODEX_BIN") or "codex")


# ---- chat tier ---------------------------------------------------------------------

CHAT_PRESETS = {
    "deepseek": ("DeepSeek", "https://api.deepseek.com", "deepseek-chat", "platform.deepseek.com → API keys"),
    "minimax": ("MiniMax", "https://api.minimaxi.com/v1", "minimax-text-01", "platform.minimaxi.com → 接口密钥"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini", "platform.openai.com → API keys"),
}


def _chat_status(env: dict[str, str]) -> tuple[str, str]:
    mode = (env.get("CONVEYOR_CHAT_MODE") or "off").lower()
    model = env.get("CONVEYOR_CHAT_MODEL") or env.get("MINIMAX_CHAT_MODEL")
    key = env.get("CONVEYOR_CHAT_API_KEY") or env.get("MINIMAX_API_KEY")
    if mode == "auto" and model and key:
        return OK, f"{model}" + (" · 可看图" if env.get("CONVEYOR_CHAT_VISION", "").lower() in ("true", "1", "yes", "on") else "")
    if mode == "auto":
        return PARTIAL, "已开启但缺模型或密钥"
    return MISSING, "未开启（所有请求都走 Codex）"


def _chat_run(ui: UI, env: dict[str, str]) -> Change | None:
    ui.hint("对话层负责秒回聊天、问答、看图；需要动手时自动转给 Codex。")
    choice = ui.select("对话模型用哪家？", [
        *(Option(k, v[0], v[3]) for k, v in CHAT_PRESETS.items()),
        Option("custom", "其他 OpenAI 兼容接口", "自己填地址和模型名"),
        Option("off", "关闭对话层", "所有请求都走 Codex"),
    ])
    if choice == "off":
        return Change({"CONVEYOR_CHAT_MODE": "off"}, services=("telegram", "feishu", "web"))
    if choice == "custom":
        base = ui.ask("接口地址（到 /v1 为止）", env.get("CONVEYOR_CHAT_BASE_URL"), required=True,
                      validate=lambda v: None if v.startswith(("http://", "https://")) else "要以 http(s):// 开头")
        model = ui.ask("模型名", env.get("CONVEYOR_CHAT_MODEL"), required=True)
    else:
        _, base, default_model, _ = CHAT_PRESETS[choice]
        current = env.get("CONVEYOR_CHAT_MODEL") if env.get("CONVEYOR_CHAT_BASE_URL") == base else None
        model = ui.ask("模型名", current or default_model, required=True)
    existing_key = env.get("CONVEYOR_CHAT_API_KEY") if env.get("CONVEYOR_CHAT_BASE_URL") == base else None
    while True:
        api_key = ui.secret("API key", existing_key)
        if _retry(ui, f"用 {model} 试聊一句", lambda: checks.chat_model(base, api_key, model)) is not None:
            break
        existing_key = None
    vision = ui.confirm("这个模型支持图片输入吗？（不确定选 n，看图会交给 Codex）", default=False)
    return Change({
        "CONVEYOR_CHAT_MODE": "auto",
        "CONVEYOR_CHAT_BASE_URL": base,
        "CONVEYOR_CHAT_API_KEY": api_key,
        "CONVEYOR_CHAT_MODEL": model,
        "CONVEYOR_CHAT_VISION": "true" if vision else "false",
    }, services=("telegram", "feishu", "web"))


def _chat_verify(env: dict[str, str]) -> checks.Result | None:
    if (env.get("CONVEYOR_CHAT_MODE") or "off").lower() != "auto":
        return None
    base = env.get("CONVEYOR_CHAT_BASE_URL") or env.get("MINIMAX_BASE_URL") or "https://api.minimaxi.com/v1"
    key = env.get("CONVEYOR_CHAT_API_KEY") or env.get("MINIMAX_API_KEY") or ""
    model = env.get("CONVEYOR_CHAT_MODEL") or env.get("MINIMAX_CHAT_MODEL") or ""
    if not key or not model:
        return checks.Result(False, "缺模型或密钥")
    return checks.chat_model(base, key, model)


# ---- web search ----------------------------------------------------------------------

SEARCH_BACKENDS = [
    Option("brave", "Brave Search", "brave.com/search/api，有免费额度"),
    Option("tavily", "Tavily", "tavily.com，面向 AI 的搜索，有免费额度"),
    Option("serper", "Serper", "serper.dev，Google 结果"),
    Option("searxng", "SearXNG", "自建实例，不需要 key"),
    Option("disabled", "不用联网搜索"),
]


def _search_status(env: dict[str, str]) -> tuple[str, str]:
    backend = (env.get("WEB_SEARCH_BACKEND") or "disabled").lower()
    if backend == "disabled":
        return MISSING, "未开启（核实、时效问题、话题关注需要它）"
    needs = env.get("WEB_SEARCH_ENDPOINT") if backend == "searxng" else env.get("WEB_SEARCH_API_KEY")
    return (OK, backend) if needs else (PARTIAL, f"{backend} 缺 key/地址")


def _search_run(ui: UI, env: dict[str, str]) -> Change | None:
    current = (env.get("WEB_SEARCH_BACKEND") or "brave").lower()
    idx = next((i for i, o in enumerate(SEARCH_BACKENDS) if o.value == current), 0)
    backend = ui.select("联网搜索用哪个服务？", SEARCH_BACKENDS, default=idx)
    if backend == "disabled":
        return Change({"WEB_SEARCH_BACKEND": "disabled"}, services=("telegram", "feishu", "web"))
    while True:
        key = endpoint = ""
        if backend == "searxng":
            endpoint = ui.ask("SearXNG 地址", env.get("WEB_SEARCH_ENDPOINT"), required=True,
                              validate=lambda v: None if v.startswith(("http://", "https://")) else "要以 http(s):// 开头")
        else:
            key = ui.secret(f"{backend} API key", env.get("WEB_SEARCH_API_KEY") if backend == current else None)
        if _retry(ui, "试搜一次", lambda: checks.web_search(backend, key, endpoint)) is not None:
            break
    updates = {"WEB_SEARCH_BACKEND": backend}
    if key:
        updates["WEB_SEARCH_API_KEY"] = key
    if endpoint:
        updates["WEB_SEARCH_ENDPOINT"] = endpoint
    return Change(updates, services=("telegram", "feishu", "web"))


def _search_verify(env: dict[str, str]) -> checks.Result | None:
    backend = (env.get("WEB_SEARCH_BACKEND") or "disabled").lower()
    if backend == "disabled":
        return None
    return checks.web_search(backend, env.get("WEB_SEARCH_API_KEY", ""), env.get("WEB_SEARCH_ENDPOINT", ""))


# ---- email ---------------------------------------------------------------------------

EMAIL_PRESETS = {
    "gmail": ("Gmail", "imap.gmail.com", 993, "smtp.gmail.com", 587,
              "先开两步验证，再到 myaccount.google.com/apppasswords 生成 16 位应用专用密码"),
    "qq": ("QQ 邮箱", "imap.qq.com", 993, "smtp.qq.com", 465,
           "网页版 设置 → 账号 → 开启 IMAP/SMTP 服务 → 生成授权码"),
    "163": ("网易 163", "imap.163.com", 993, "smtp.163.com", 465,
            "网页版 设置 → POP3/SMTP/IMAP → 开启 IMAP/SMTP → 新增授权密码"),
    "126": ("网易 126", "imap.126.com", 993, "smtp.126.com", 465,
            "网页版 设置 → POP3/SMTP/IMAP → 开启 IMAP/SMTP → 新增授权密码"),
    "icloud": ("iCloud", "imap.mail.me.com", 993, "smtp.mail.me.com", 587,
               "appleid.apple.com → 登录与安全 → App 专用密码"),
}


def _email_status(env: dict[str, str]) -> tuple[str, str]:
    if env.get("GMAIL_BACKEND") == "imap_smtp" and env.get("GMAIL_ADDRESS") and env.get("GMAIL_APP_PASSWORD"):
        return OK, env["GMAIL_ADDRESS"]
    if env.get("GMAIL_ADDRESS") or env.get("GMAIL_APP_PASSWORD"):
        return PARTIAL, "填了一半"
    return MISSING, "未配置"


def _port(v: str) -> str | None:
    return None if v.isdigit() and 0 < int(v) < 65536 else "端口应为 1-65535"


def _email_run(ui: UI, env: dict[str, str]) -> Change | None:
    ui.hint("用 IMAP 读信、SMTP 发信。密码填邮箱的「授权码 / 应用专用密码」，不是登录密码。")
    choice = ui.select("哪种邮箱？", [
        *(Option(k, v[0]) for k, v in EMAIL_PRESETS.items()),
        Option("custom", "其他邮箱", "自己填服务器"),
    ])
    if choice == "custom":
        imap_host = ui.ask("IMAP 服务器", env.get("GMAIL_IMAP_HOST"), required=True)
        imap_port = int(ui.ask("IMAP 端口（SSL）", env.get("GMAIL_IMAP_PORT") or "993", validate=_port))
        smtp_host = ui.ask("SMTP 服务器", env.get("GMAIL_SMTP_HOST"), required=True)
        smtp_port = int(ui.ask("SMTP 端口（465=SSL，587=STARTTLS）", env.get("GMAIL_SMTP_PORT") or "465", validate=_port))
    else:
        name, imap_host, imap_port, smtp_host, smtp_port, how = EMAIL_PRESETS[choice]
        ui.hint(f"{name} 授权码获取：{how}")
    address = env.get("GMAIL_ADDRESS")
    while True:
        address = ui.ask("邮箱地址", address, required=True,
                         validate=lambda v: None if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v) else "邮箱格式不对")
        password = ui.secret("授权码 / 应用专用密码", env.get("GMAIL_APP_PASSWORD"))
        if _retry(ui, f"登录收信服务器 {imap_host}",
                  lambda: checks.imap_login(imap_host, imap_port, address, password)) is None:
            continue
        if _retry(ui, f"登录发信服务器 {smtp_host}",
                  lambda: checks.smtp_login(smtp_host, smtp_port, address, password)) is None:
            continue
        break
    if ui.confirm("给自己发一封测试邮件？", default=True):
        with ui.spinner("发送测试邮件") as step:
            sent = checks.send_test_mail(smtp_host, smtp_port, address, password)
            step.done(sent.ok, sent.detail)
    return Change({
        "GMAIL_BACKEND": "imap_smtp",
        "GMAIL_ADDRESS": address,
        "GMAIL_APP_PASSWORD": password,
        "GMAIL_IMAP_HOST": imap_host,
        "GMAIL_IMAP_PORT": str(imap_port),
        "GMAIL_SMTP_HOST": smtp_host,
        "GMAIL_SMTP_PORT": str(smtp_port),
    }, services=("telegram", "feishu", "web"))


def _email_verify(env: dict[str, str]) -> checks.Result | None:
    if env.get("GMAIL_BACKEND") != "imap_smtp":
        return None
    address, password = env.get("GMAIL_ADDRESS", ""), env.get("GMAIL_APP_PASSWORD", "")
    imap = checks.imap_login(env.get("GMAIL_IMAP_HOST", "imap.gmail.com"),
                             int(env.get("GMAIL_IMAP_PORT") or 993), address, password)
    if not imap.ok:
        return checks.Result(False, "收信：" + imap.detail)
    smtp = checks.smtp_login(env.get("GMAIL_SMTP_HOST", "smtp.gmail.com"),
                             int(env.get("GMAIL_SMTP_PORT") or 587), address, password)
    if not smtp.ok:
        return checks.Result(False, "发信：" + smtp.detail)
    return checks.Result(True, f"{address}（{imap.detail}）")


# ---- feishu ------------------------------------------------------------------------


def _feishu_status(env: dict[str, str]) -> tuple[str, str]:
    if env.get("LARK_APP_ID") and env.get("LARK_APP_SECRET"):
        if env.get("LARK_ALLOWED_OPEN_ID"):
            return OK, env["LARK_APP_ID"]
        return PARTIAL, "缺允许使用的 open_id"
    return MISSING, "未配置"


def _feishu_run(ui: UI, env: dict[str, str]) -> Change | None:
    ui.hint("open.feishu.cn → 开发者后台 → 创建企业自建应用 → 凭证与基础信息 里有 App ID / App Secret。\n"
            "还要开启「机器人」能力、订阅 im.message.receive_v1 事件（长连接）。")
    app_id = env.get("LARK_APP_ID")
    while True:
        app_id = ui.ask("App ID", app_id, required=True,
                        validate=lambda v: None if v.startswith("cli_") else "App ID 以 cli_ 开头")
        app_secret = ui.secret("App Secret", env.get("LARK_APP_SECRET"))
        if _retry(ui, "向飞书换取访问凭证", lambda: checks.feishu(app_id, app_secret)) is not None:
            break
    ui.hint("允许使用的人用 open_id 表示（ou_ 开头）。不知道的话先留空：\n"
            "启动飞书机器人后私聊它，它会回复你的 open_id，再回来填。")
    open_id = ui.ask("你的 open_id（可留空）", env.get("LARK_ALLOWED_OPEN_ID"),
                     validate=lambda v: None if v.startswith("ou_") else "open_id 以 ou_ 开头")
    change = Change({"LARK_APP_ID": app_id, "LARK_APP_SECRET": app_secret}, services=("feishu",))
    if open_id:
        change.updates["LARK_ALLOWED_OPEN_ID"] = open_id
    else:
        change.notes.append("飞书机器人在填好 LARK_ALLOWED_OPEN_ID 前只会回复你的 open_id。")
    return change


def _feishu_verify(env: dict[str, str]) -> checks.Result | None:
    if not (env.get("LARK_APP_ID") and env.get("LARK_APP_SECRET")):
        return None
    return checks.feishu(env["LARK_APP_ID"], env["LARK_APP_SECRET"])


# ---- github ------------------------------------------------------------------------


def _github_status(env: dict[str, str]) -> tuple[str, str]:
    if env.get("GITHUB_TOKEN"):
        return OK, env.get("GITHUB_DEFAULT_REPO") or "已配置 token"
    return MISSING, "未配置"


def _github_run(ui: UI, env: dict[str, str]) -> Change | None:
    ui.hint("github.com/settings/personal-access-tokens → Fine-grained token，\n"
            "选要看的仓库，给 Contents / Issues / Pull requests / Actions 的只读权限即可。")
    repo = env.get("GITHUB_DEFAULT_REPO")
    while True:
        token = ui.secret("GitHub token", env.get("GITHUB_TOKEN"))
        repo = ui.ask("默认仓库 owner/name（可留空）", repo,
                      validate=lambda v: None if re.match(r"^[\w.-]+/[\w.-]+$", v) else "格式是 owner/name")
        if _retry(ui, "验证 token", lambda: checks.github(token, repo)) is not None:
            break
    change = Change({"GITHUB_TOKEN": token}, services=("telegram", "feishu", "web"))
    if repo:
        change.updates["GITHUB_DEFAULT_REPO"] = repo
    return change


def _github_verify(env: dict[str, str]) -> checks.Result | None:
    token = env.get("GITHUB_TOKEN")
    return checks.github(token, env.get("GITHUB_DEFAULT_REPO", "")) if token else None


# ---- web console -------------------------------------------------------------------


def _web_status(env: dict[str, str]) -> tuple[str, str]:
    enabled = (env.get("CONVEYOR_WEB_ENABLED") or "false").lower() in ("true", "1", "yes", "on")
    if enabled and env.get("CONVEYOR_WEB_TOKEN"):
        return OK, f"{env.get('CONVEYOR_WEB_HOST') or '127.0.0.1'}:{env.get('CONVEYOR_WEB_PORT') or '8787'}"
    if enabled:
        return PARTIAL, "已开启但没有访问令牌"
    return MISSING, "未开启"


def _web_run(ui: UI, env: dict[str, str]) -> Change | None:
    if not ui.confirm("开启 Web 控制台？", default=True):
        return Change({"CONVEYOR_WEB_ENABLED": "false"}, services=("web",))
    port = ui.ask("端口", env.get("CONVEYOR_WEB_PORT") or "8787", validate=_port)
    host = env.get("CONVEYOR_WEB_HOST") or "127.0.0.1"
    if host not in ("127.0.0.1", "localhost", "::1"):
        ui.warn(f"当前监听 {host}，不是只限本机。建议改回 127.0.0.1，用 SSH 隧道访问。")
        if ui.confirm("改回 127.0.0.1？", default=True):
            host = "127.0.0.1"
    token = env.get("CONVEYOR_WEB_TOKEN")
    if not token or ui.confirm("重新生成访问令牌？（旧令牌会失效）", default=False):
        token = secrets.token_urlsafe(32)
    ui.section("访问方式")
    ui.info(f"在你电脑上：ssh -L {port}:127.0.0.1:{port} <你的服务器>")
    ui.info(f"然后打开 http://127.0.0.1:{port} ，令牌：{token}")
    ui.hint("令牌只在这里完整显示一次，请存进密码管理器。")
    return Change({
        "CONVEYOR_WEB_ENABLED": "true",
        "CONVEYOR_WEB_HOST": host,
        "CONVEYOR_WEB_PORT": port,
        "CONVEYOR_WEB_TOKEN": token,
    }, services=("web",))


def _web_verify(env: dict[str, str]) -> checks.Result | None:
    status, text = _web_status(env)
    if status == MISSING:
        return None
    return checks.Result(status == OK, text)


MODULES: list[Module] = [
    Module("telegram", "Telegram 机器人", "你和 Conveyor 说话的入口", True,
           _telegram_status, _telegram_run, _telegram_verify),
    Module("codex", "Codex Agent", "真正动手干活的 Agent", True,
           _codex_status, _codex_run, _codex_verify),
    Module("chat", "对话层模型", "秒回聊天和问答", False, _chat_status, _chat_run, _chat_verify),
    Module("search", "联网搜索", "核实、时效问题、话题关注", False,
           _search_status, _search_run, _search_verify),
    Module("email", "邮箱", "读信、搜信、发信", False, _email_status, _email_run, _email_verify),
    Module("feishu", "飞书", "第二个聊天入口", False, _feishu_status, _feishu_run, _feishu_verify),
    Module("github", "GitHub", "Issue / PR / CI", False, _github_status, _github_run, _github_verify),
    Module("web", "Web 控制台", "浏览器里看任务和审批", False, _web_status, _web_run, _web_verify),
]

BY_KEY = {m.key: m for m in MODULES}


def describe_change(change: Change) -> list[str]:
    lines = []
    for key, value in change.updates.items():
        shown = mask(value) if is_secret(key) else value
        lines.append(f"{key} = {shown}")
    for key in sorted(change.removals):
        lines.append(f"{key}（删除）")
    return lines
