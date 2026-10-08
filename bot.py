from __future__ import annotations

import asyncio
import logging
import re
import warnings
from dataclasses import replace
from datetime import datetime

from telegram import Update
from telegram.warnings import PTBUserWarning
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                               ContextTypes, ConversationHandler,
                               MessageHandler, filters)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from channel import InboundMessage
from channel.auth import is_allowed
from channel.telegram import (
    inbound_from_update,
    make_outbound,
    send_text,
)
from config import load_settings
from handlers import dispatch
from handlers.context import is_addressed_to_bot
from handlers.onboarding import (
    operator_profile_exists,
    operator_profile_path,
    profile_text,
    save_operator_profile,
)
from handlers.tools.runner import cancel_pending, execute_confirmed, parse_tool_callback
from logging_setup import configure_logging
from runner import CodexRunner


configure_logging(
    service_name="conveyor.telegram",
    level=logging.INFO,
    fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("conveyor.telegram")

settings = load_settings()
runner = CodexRunner(settings)

from handlers.job_queue import get_job_queue
get_job_queue().configure(settings, runner)

# YYYY-MM-DD, used to slice a specific day's archived journal.
DATE_ARG_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---- Channel adapter shim (P2.1: adapter moved to channel/telegram.py) ----
# Inbound conversion and OutboundPort live in channel.telegram. bot.py
# only wires Telegram framework handlers and lifecycle. Job execution
# goes through handlers.dispatch → handlers.jobs (channel-agnostic).


# ---- Channel-agnostic handler shims ---------------------------------------

async def _dispatch_text(update: Update, text: str) -> None:
    """Send a free-text message through the shared dispatcher."""
    inbound = inbound_from_update(update, text=text)
    await dispatch(inbound, make_outbound(update), settings, runner)


async def _dispatch_command(update: Update, *, arg_text: str | None = None) -> None:
    """Route a Telegram /<cmd> through the shared dispatcher.

    If arg_text is None, we use the command's natural arg (the rest of
    the message after /<cmd>); otherwise the caller supplies the slice.
    """
    message = update.effective_message
    if message is None or not message.text:
        return
    inbound = inbound_from_update(update, text=message.text)
    if arg_text is not None:
        # Substitute the (already-mutated) text the dispatcher will see.
        # We re-parse via parse_command by rebuilding the full /<cmd> arg.
        cmd_name, _sep, _rest = message.text.partition(" ")
        rebuilt = f"{cmd_name} {arg_text}".strip()
        inbound = inbound_from_update(update, text=rebuilt)
    await dispatch(inbound, make_outbound(update), settings, runner)


async def _reply(
    update: Update,
    text: str,
    *,
    reply_markup=None,
) -> None:
    """Legacy fire-and-forget reply. Use send_text when you need the
    message_id (placeholder for in-place edit)."""
    await send_text(update, text, reply_markup=reply_markup)


async def _guard(update: Update) -> bool:
    """Auth gate for Telegram command handlers. Uses channel/auth.is_allowed
    for the actual check; adds Telegram-specific logging."""
    inbound = inbound_from_update(update)
    if is_allowed(inbound, settings):
        return True
    user = update.effective_user
    logger.warning("Rejected unauthorized Telegram user id=%s username=%s", getattr(user, "id", None), getattr(user, "username", None))
    # In groups, stay silent: other members mentioning the bot should not
    # get a reply they can use to probe it or spam the chat.
    if update.effective_message and inbound.chat_type == "p2p":
        await update.effective_message.reply_text("Unauthorized.")
    return False


def _prompt(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args or []).strip()


async def run_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    prompt = _prompt(context)
    if not prompt:
        await _reply(update, "Usage: /run <prompt>")
        return
    # /run shares the channel-agnostic codex-job path. 003 P1.
    await _dispatch_text(update, f"/run {prompt}")


async def fix_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    prompt = _prompt(context)
    if not prompt:
        await _reply(update, "Usage: /fix <prompt>")
        return
    await _dispatch_text(update, f"/fix {prompt}")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _dispatch_text(update, "/status")


async def diff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _dispatch_text(update, "/diff")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _dispatch_text(update, "/cancel")


async def jobs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    arg = _prompt(context)
    await _dispatch_command(update, arg_text=arg)


async def last_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _dispatch_text(update, "/last")


async def clean_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    arg = _prompt(context)
    await _dispatch_command(update, arg_text=arg)


async def maintain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    arg = _prompt(context)
    await _dispatch_command(update, arg_text=arg)


async def discard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _dispatch_text(update, "/discard")


async def apply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _dispatch_text(update, "/apply")


async def doctor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _dispatch_text(update, "/doctor")


async def diag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    arg = _prompt(context)
    await _dispatch_command(update, arg_text=arg)


async def security_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    arg = _prompt(context)
    await _dispatch_command(update, arg_text=arg)


async def ratelimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    arg = _prompt(context)
    await _dispatch_command(update, arg_text=arg)


async def audit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    arg = _prompt(context)
    await _dispatch_command(update, arg_text=arg)


async def log_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    arg = _prompt(context)
    await _dispatch_command(update, arg_text=arg)


async def meta_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    arg = _prompt(context)
    await _dispatch_command(update, arg_text=arg)


async def metrics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    arg = _prompt(context)
    await _dispatch_command(update, arg_text=arg)


async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    arg = _prompt(context)
    await _dispatch_command(update, arg_text=arg)


async def smoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _dispatch_text(update, "/smoke")


async def editcheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _dispatch_text(update, "/editcheck")


# --- Onboarding (P2.3): Telegram-specific handlers --------------------
# Pure profile helpers live in handlers/onboarding.py (no Telegram SDK).
# The ConversationHandler steps stay here because they need Telegram
# types (Update, CallbackQuery, InlineKeyboard*).

ONBOARDING_NAME, ONBOARDING_LANG, ONBOARDING_STYLE = range(3)


async def onboard_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _guard(update):
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data["onboarding_draft"] = {}
    await _reply(
        update,
        "1/3 怎么称呼你？\n"
        "(直接回复名字，或 `/skip` 跳过全部问卷)",
    )
    return ONBOARDING_NAME


async def onboard_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.effective_message.text or "").strip()
    if not name:
        await _reply(update, "名字不能为空。直接回复名字，或 `/skip` 跳过：")
        return ONBOARDING_NAME
    context.user_data["onboarding_draft"]["operator_name"] = name
    await _reply(
        update,
        f"好的，{name}。\n\n2/3 用什么语言？",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("zh-CN", callback_data="ob:lang:zh-CN")],
            [InlineKeyboardButton("en", callback_data="ob:lang:en")],
            [InlineKeyboardButton("ja", callback_data="ob:lang:ja")],
        ]),
    )
    return ONBOARDING_LANG


async def onboard_lang_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = query.data.split(":", 2)[2]
    context.user_data["onboarding_draft"]["operator_language"] = lang
    await query.edit_message_text(
        f"已选 {lang}。\n\n3/3 想要啥风格？",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("terse", callback_data="ob:style:terse")],
            [InlineKeyboardButton("balanced", callback_data="ob:style:balanced")],
            [InlineKeyboardButton("detailed", callback_data="ob:style:detailed")],
        ]),
    )
    return ONBOARDING_STYLE


async def onboard_style_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    style = query.data.split(":", 2)[2]
    context.user_data["onboarding_draft"]["operator_style"] = style
    context.user_data["onboarding_draft"]["operator_standing"] = (
        settings.operator_standing or "personal-scale, single operator"
    )
    saved = save_operator_profile(settings, context.user_data["onboarding_draft"])
    if not saved:
        await query.edit_message_text("保存失败了，重启 bot 后再试。")
        return ConversationHandler.END
    draft = context.user_data["onboarding_draft"]
    await query.edit_message_text(
        f"✅ 已保存到 `operator.json`：\n"
        f"  name: {draft.get('operator_name', '?')}\n"
        f"  language: {draft.get('operator_language', '?')}\n"
        f"  style: {draft.get('operator_style', '?')}\n"
        f"  standing: {draft.get('operator_standing', '?')}\n\n"
        f"生效需要重启 bot（runner 启动时读这份 JSON）。\n"
        f"改用 /profile；重做问卷 /onboard。"
    )
    return ConversationHandler.END


async def onboard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _reply(update, "Onboarding 取消。继续用 .env 默认值。")
    return ConversationHandler.END


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    text = profile_text(settings)
    if text is None:
        await _reply(
            update,
            "暂无 `operator.json`，用的是 .env 默认值。\n"
            "跑 `/onboard` 创建一份（重启后生效）。",
        )
    else:
        await _reply(update, text)


async def tool_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline-button confirmation for dangerous agent tools."""
    if not await _guard(update):
        return
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    parsed = parse_tool_callback(query.data or "")
    if parsed is None:
        return
    action, token = parsed
    from agents import AgentError, conversation_for_chat
    inbound = inbound_from_update(update, text="")
    try:
        inbound = replace(inbound, chat_id=conversation_for_chat(settings, inbound.channel, inbound.chat_id))
    except AgentError as exc:
        await _reply(update, str(exc))
        return
    port = make_outbound(update)
    if action == "confirm":
        await execute_confirmed(inbound, port, settings, token)
    else:
        await cancel_pending(inbound, port, settings, token)


async def relay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline-button confirmation for cross-channel approval relay."""
    query = update.callback_query
    if query is None:
        return
    user = update.effective_user
    user_id = str(getattr(user, "id", "") or "")
    if not await _guard(update):
        try:
            await query.answer("Unauthorized.", show_alert=True)
        except Exception:
            pass
        chat = update.effective_chat
        chat_id = str(getattr(chat, "id", "") or "")
        from handlers.tools.audit import audit_tool_event
        audit_tool_event(
            settings,
            operator_id=user_id,
            chat_id=chat_id,
            channel="telegram",
            tool_name="approval_relay",
            arg="",
            danger="high",
            action="relay_rejected_unauthorized",
            token="relay_unauthorized",
        )
        return

    if not getattr(settings, "approval_relay_enabled", False):
        await query.answer("Approval relay is disabled.", show_alert=True)
        return

    import approval_relay
    data = query.data or ""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "relay" or parts[1] not in ("approve", "reject"):
        await query.answer("Invalid relay action.")
        return

    action, token = parts[1], parts[2]
    approve = (action == "approve")

    outcome = await asyncio.to_thread(
        approval_relay.decide,
        settings,
        token,
        approve=approve,
        via="telegram",
        decided_by=user_id or "telegram",
    )

    if outcome == "won":
        msg = "已批准，正在原端执行…" if approve else "已拒绝。"
        await query.answer(msg)
    elif outcome == "already_decided":
        await query.answer("已被其他端处理", show_alert=True)
    elif outcome == "expired":
        await query.answer("已过期", show_alert=True)
    else:
        await query.answer("未找到该审批或已失效", show_alert=True)


async def deep_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """"🔍 用 Codex 处理" button under a chat-tier answer → /deep."""
    if not await _guard(update):
        return
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    from agents import AgentError, conversation_for_chat
    from channel.telegram_identity import context_tag
    inbound = inbound_from_update(update, text="/deep")
    try:
        routed = conversation_for_chat(settings, inbound.channel, inbound.chat_id)
    except AgentError as exc:
        await _reply(update, str(exc))
        return
    data = query.data or ""
    expected = f"deep:{context_tag(routed)}"
    if data != expected and not (data == "deep" and ":agent:" not in routed):
        await _reply(update, "这个按钮属于之前的 Agent 对话。请切回原 Agent 后重试，或在当前对话发送 /deep。")
        return
    inbound = replace(inbound, chat_id=routed)
    await dispatch(inbound, make_outbound(update), settings, runner)


async def workers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _dispatch_command(update)


async def workers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Server token for Workers navigation, captured-session tasks, and translated confirms."""
    if not await _guard(update):
        return
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""
    if len(data.encode("utf-8")) > 64 or not data.startswith("wk:") or not data[3:]:
        await query.answer()
        await _reply(update, "这个按钮已失效。")
        return
    await query.answer()
    from handlers.workers import handle_workers_token
    inbound = inbound_from_update(update, text="")
    await handle_workers_token(inbound, make_outbound(update), settings, runner, data[3:])


async def agent_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    from handlers.agent_selection import handle_agent_callback
    await handle_agent_callback(
        inbound_from_update(update, text="/agent"), make_outbound(update),
        runner, settings, query.data or "",
    )


async def text_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Groups: act only when @mentioned or replied to (Grok-style), so
    # ordinary group conversation is neither answered nor rejected.
    if not is_addressed_to_bot(inbound_from_update(update)):
        return
    if not await _guard(update):
        return
    # Onboarding-C: first-run nudge. If the user types ANY message
    # before running /onboard, surface the prompt instead of
    # silently starting a job. They can still /onboard later to
    # set their identity; the prompt just doesn't let the first
    # message get lost in a job they didn't intend.
    if not operator_profile_exists(settings):
        # First-run nudge: same button as /start so the user does
        # not have to type /onboard after reading the hint.
        await _reply(
            update,
            "第一次用先告诉我你是谁：\n"
            "`/onboard` 走 3 步问卷，或 `/skip` 用默认。",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("开始 onboarding", callback_data="ob:start")],
            ]),
        )
        return
    message = update.effective_message
    prompt = ((message.text or message.caption) if message else "") or ""
    prompt = prompt.strip()
    # A photo without a caption is still a request ("what is this?").
    has_image = bool(message and (message.photo or message.document))
    if not prompt and not has_image:
        return
    # Delegate to the shared channel-agnostic dispatcher (003 P0.2).
    await _dispatch_text(update, prompt)


async def memo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    prompt = _prompt(context)
    if not prompt:
        await _reply(update, "Usage: /memo <something to remember>")
        return
    # Delegate to the shared channel-agnostic handler (003 P0.2).
    await _dispatch_text(update, f"记 {prompt}")


async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    # Optional positional args: [date] [category] (any order).
    # /memory            -> today MEMORY.md
    # /memory preference -> only the ## preference section of today
    # /memory 2026-06-03 -> archived journal for that day
    # /memory 2026-06-03 fact -> archived journal, fact section only
    date_str: str | None = None
    category: str | None = None
    for raw in context.args or []:
        arg = raw.strip()
        if not arg:
            continue
        if date_str is None and DATE_ARG_PATTERN.match(arg):
            date_str = arg
            continue
        lowered = arg.lower()
        if category is None and lowered in runner.MEMO_CATEGORIES:
            category = lowered
            continue
    if date_str is not None:
        text = runner.read_journal(date_str, category)
        if not text:
            if category:
                await _reply(
                    update,
                    f"没找到或为空：Journal {date_str} 的 ## {category} 段不存在。",
                )
            else:
                await _reply(update, f"没找到或为空：Journal {date_str} 还没有。")
            return
        header = f"Journal {date_str}"
        if category:
            header += f" · {category}"
        await _reply(update, f"{header}\n{text}")
        return
    text = runner.read_memory(category)
    if not text:
        if category:
            await _reply(
                update,
                f"今天的 MEMORY.md 里没有 ## {category} 段。",
            )
        else:
            await _reply(update, "今天的 MEMORY.md 还是空的。直接发「记 xxx」就能写。")
        return
    header = f"MEMORY.md @ {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    if category:
        header += f" · {category}"
    await _reply(update, f"{header}\n{text}")


async def journal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    raw_limit = context.args[0] if context.args else "10"
    try:
        limit = max(1, min(50, int(raw_limit)))
    except ValueError:
        limit = 10
    files = runner.list_journal(limit)
    if not files:
        await _reply(update, "JOURNAL/ 还没有条目。首次 12 点刷新后会出现。")
        return
    lines = [f"Journal (most recent {len(files)}):"]
    for path in files:
        size = path.stat().st_size
        lines.append(f"  {path.name}  ({size} bytes)")
    await _reply(update, "\n".join(lines))


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    # Onboarding-C: when no operator.json exists yet, replace the
    # canned greeting with a first-run prompt that nudges the user
    # to /onboard. The .env defaults still work (operator.json is
    # optional) but the user gets a clear "first time" experience
    # mirroring Hermes-style personal agent onboarding.
    if not operator_profile_exists(settings):
        # First-run path: show the welcome with a one-tap button so
        # the user does not have to remember the /onboard command.
        # The button drives the same ConversationHandler entry
        # point (callback_data="ob:start" -> onboard_start). /skip
        # remains available as a text command for users who want
        # to skip without going through the Q&A.
        await _reply(
            update,
            "你好！看起来这是第一次用。\n\n"
            "`/onboard` 告诉我怎么称呼你、用啥语言、想要啥风格，"
            "之后每次都会按这个走。\n"
            "不想设的话 `/skip` 跳过（用默认）。",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("开始 onboarding", callback_data="ob:start")],
            ]),
        )
        return
    await _reply(update, "你好！直接发消息就行，我会像对话一样处理（shell、查资料、改文件都可以）。运维命令用 /help。")


async def generic_command_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fallback for slash commands registered in COMMAND_TABLE but not
    wired to explicit CommandHandler entries (e.g. /load /tools /disk)."""
    if not await _guard(update):
        return
    await _dispatch_command(update)


async def post_init(application: Application) -> None:
    await runner.validate()
    await application.bot.set_my_commands(
        [
            ("agent", "查看或绑定项目 Agent"),
            ("workers", "列出或继续 Worker 会话"),
            ("fix", "改文件：/fix <需求>"),
            ("jobs", "看最近任务"),
            ("last", "看最近结果"),
            ("diff", "看最近改动"),
            ("apply", "应用最近改动"),
            ("discard", "丢弃最近 worktree"),
            ("clean", "清理旧任务"),
            ("maintain", "自维护检查和清理"),
            ("diag", "一键诊断包"),
            ("health", "健康快照摘要"),
            ("doctor", "后端体检"),
            ("audit", "任务和 worktree 审计"),
            ("log", "安全查看最近 job 日志摘要"),
            ("meta", "查看 job.json 结构状态"),
            ("metrics", "最近任务趋势和 token 用量"),
            ("security", "安全审计"),
            ("ratelimit", "查看最近 429 限流"),
            ("smoke", "端到端验收"),
            ("editcheck", "临时 repo 真改文件验收"),
            ("memo", "显式记一条：/memo <内容>"),
            ("memory", "看今天 MEMORY.md"),
            ("journal", "看已归档的 journal 列表"),
            ("cancel", "中止当前任务"),
            ("load", "本机负载快照"),
            ("vps", "同上 (alias /load)"),
            ("htop", "top 风格进程帧"),
            ("ps", "进程快照 (comm 模式)"),
            ("tools", "列出 agent 工具"),
            ("disk", "磁盘使用快照"),
            ("logs", "Conveyor 服务日志"),
            ("service_status", "Conveyor 服务状态"),
            ("git_status", "Workspace git status"),
            ("diagnose", "Hybrid 主机诊断"),
            ("restart", "重启 Conveyor 服务"),
            ("audit_tools", "危险工具审计"),
        ]
    )
    await get_job_queue().start_pending()
    if getattr(settings, "approval_relay_enabled", False):
        try:
            import approval_relay
            from channel.telegram import TelegramChatOutbound

            def make_tg_port(chat_id: str):
                return TelegramChatOutbound(application.bot, chat_id=chat_id)

            consumer = approval_relay.RelayConsumer(settings, channel="telegram", port_factory=make_tg_port)
            application.bot_data["approval_relay_consumer"] = consumer
            consumer.start()
        except Exception:
            logger.exception("Failed to start approval relay consumer in telegram bot")
    logger.info("Codex Telegram bot ready. Workspace=%s task_root=%s", settings.codex_workspace_root, settings.codex_task_root)


async def post_shutdown(application: Application) -> None:
    consumer = application.bot_data.get("approval_relay_consumer")
    if consumer is not None:
        consumer.stop()


def main() -> None:
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("run", run_cmd))
    application.add_handler(CommandHandler("fix", fix_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("diff", diff_cmd))
    application.add_handler(CommandHandler("cancel", cancel_cmd))
    application.add_handler(CommandHandler("jobs", jobs_cmd))
    application.add_handler(CommandHandler("last", last_cmd))
    application.add_handler(CommandHandler("clean", clean_cmd))
    application.add_handler(CommandHandler("maintain", maintain_cmd))
    application.add_handler(CommandHandler("discard", discard_cmd))
    application.add_handler(CommandHandler("apply", apply_cmd))
    application.add_handler(CommandHandler("diag", diag_cmd))
    application.add_handler(CommandHandler("health", health_cmd))
    application.add_handler(CommandHandler("doctor", doctor_cmd))
    application.add_handler(CommandHandler("audit", audit_cmd))
    application.add_handler(CommandHandler("log", log_cmd))
    application.add_handler(CommandHandler("meta", meta_cmd))
    application.add_handler(CommandHandler("metrics", metrics_cmd))
    application.add_handler(CommandHandler("security", security_cmd))
    application.add_handler(CommandHandler("ratelimit", ratelimit_cmd))
    application.add_handler(CommandHandler("smoke", smoke_cmd))
    application.add_handler(CommandHandler("editcheck", editcheck_cmd))
    application.add_handler(CommandHandler("memo", memo_cmd))
    application.add_handler(CommandHandler("memory", memory_cmd))
    application.add_handler(CommandHandler("journal", journal_cmd))
    # Onboarding (P2.3): ConversationHandler for /onboard.  Profile
    # helpers live in handlers/onboarding.py; Telegram-specific steps
    # stay in bot.py because they need Update / CallbackQuery types.
    # per_message=False is intended here (the flow mixes commands, text and
    # one callback button); PTB warns about that combination on every start.
    warnings.filterwarnings("ignore", message="If 'per_message=False'", category=PTBUserWarning)
    application.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("onboard", onboard_start),
                CallbackQueryHandler(onboard_start, pattern=r"^ob:start$"),
            ],
            states={
                ONBOARDING_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, onboard_name),
                ],
                ONBOARDING_LANG: [
                    CallbackQueryHandler(onboard_lang_button, pattern=r"^ob:lang:"),
                ],
                ONBOARDING_STYLE: [
                    CallbackQueryHandler(onboard_style_button, pattern=r"^ob:style:"),
                ],
            },
            fallbacks=[CommandHandler("skip", onboard_cancel)],
        )
    )
    application.add_handler(CommandHandler("profile", profile_cmd))
    application.add_handler(CommandHandler("workers", workers_cmd))
    application.add_handler(CallbackQueryHandler(workers_callback, pattern=r"^wk:"))
    application.add_handler(CallbackQueryHandler(tool_callback, pattern=r"^tool:"))
    application.add_handler(CallbackQueryHandler(relay_callback, pattern=r"^relay:"))
    application.add_handler(CallbackQueryHandler(deep_callback, pattern=r"^deep(?::[a-f0-9]{16})?$"))
    application.add_handler(CallbackQueryHandler(agent_callback, pattern=r"^agent:"))
    # Catch-all for COMMAND_TABLE entries without explicit CommandHandler above.
    application.add_handler(MessageHandler(filters.COMMAND, generic_command_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_cmd))
    # Photos / image files (caption = the question; "/fix …" captions work
    # too because dispatch parses the caption like text).
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, text_cmd))
    # Defense-in-depth: any unhandled exception in a handler is
    # logged by PTB with "No error handlers are registered,
    # logging exception." The user sees nothing. The recent
    # onboarding-C NameError was a 20-minute hunt for the same
    # reason. error_handler catches the exception, logs the
    # full traceback, and surfaces a short, non-leaky user
    # message so the operator knows something is broken
    # before ssh-ing into the VPS. The reply itself is
    # wrapped in try/except so a failure in the error path
    # does not loop back into the error handler.
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Not inside an except block here, so pass the error explicitly;
        # logger.exception alone logs "NoneType: None" and loses the traceback.
        logger.error("Unhandled exception in handler", exc_info=context.error)
        try:
            if isinstance(update, Update) and update.effective_message:
                await update.effective_message.reply_text(
                    "Bot 内部错误，看下 logs。\n（最近一次自动运行的 progress_smoke 在 /opt/conveyor/scripts/。）",
                )
        except Exception:
            logger.exception("Failed to send error reply")
    application.add_error_handler(error_handler)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        asyncio.run(runner.cancel())
