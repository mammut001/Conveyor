from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime

from telegram import Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                               ContextTypes, ConversationHandler, InlineQueryHandler,
                               MessageHandler, filters)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import load_settings
from config import OPERATOR_PROFILE_FIELDS, load_settings
from redaction import redact_text, truncate
from runner import CodexRunner, JobMode
from scripts.auto_maintain import run_maintenance
from scripts.diagnostics import diagnostics_report
from scripts.doctor import check_disk, check_latest_job, check_runtime_dirs, check_workspace
from scripts.edit_harness import run_edit_harness
from scripts.harness_common import check_minimax_models, check_systemd_active
from scripts.health_snapshot import health_snapshot
from scripts.job_audit import run_job_audit
from scripts.log_summary import summarize_log
from scripts.metadata_report import metadata_report
from scripts.metrics_report import metrics_report
from scripts.rate_limit_report import rate_limit_report
from scripts.security_audit import run_security_audit
from scripts.smoke import run_smoke


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)


class SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_text(arg) if isinstance(arg, str) else arg for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: redact_text(value) if isinstance(value, str) else value for key, value in record.args.items()}
        return True


for handler in logging.getLogger().handlers:
    handler.addFilter(SecretRedactingFilter())
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("codex_telegram_bot")

settings = load_settings()
runner = CodexRunner(settings)

# Plain-text prompts that start with one of these words route to the memo
# fast path (_handle_memo_fast_path below), which writes to MEMORY.md
# directly without going through codex. Used by text_cmd so users can write
# "记住 xxx" or "记一下 yyy" without needing a /memo command.
MEMORY_KEYWORD_PATTERN = re.compile(r"^\s*(memo|备忘|记下|记一下|记住|记录|记\b)", re.IGNORECASE)
# Optional explicit category tag inside the memo body, e.g. "[preference] 用 pnpm".
# The first match wins; later tags are stripped from the content.
CATEGORY_PATTERN = re.compile(r"\[(preference|fact|tool-quirk|convention)\]", re.IGNORECASE)
# YYYY-MM-DD, used to slice a specific day's archived journal.
DATE_ARG_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _detect_memory_intent(prompt: str) -> bool:
    return bool(MEMORY_KEYWORD_PATTERN.match(prompt))


async def _handle_memo_fast_path(update: Update, prompt: str) -> None:
    """Append a memo to today's MEMORY.md. Skips codex entirely.

    Fast path for "记 x" / /memo: parse optional [category] tag, fall back to
    the LLM classifier (any failure -> "unfiled"), then atomic-append to the
    per-day worktree's MEMORY.md. Replaces the old codex-via-MEMO flow.
    """
    stripped = MEMORY_KEYWORD_PATTERN.sub("", prompt, count=1).strip()
    matches = CATEGORY_PATTERN.findall(stripped)
    if not stripped:
        await _reply(update, "Usage: 记 <something>  or  /memo [category] <something>")
        return
    if matches:
        category = matches[0].lower()
        content = CATEGORY_PATTERN.sub("", stripped).strip()
    else:
        content = stripped
        if not content:
            await _reply(update, "Usage: 记 <something>  or  /memo [category] <something>")
            return
        try:
            category = await runner.classify_memo(content)
        except Exception as exc:  # never let classifier crashes break capture
            logger.exception("classify_memo failed: %s", exc)
            category = "unfiled"
    auto_ts = category == "fact"
    try:
        summary = await runner.append_memo(category, content, auto_timestamp=auto_ts)
    except Exception as exc:
        await _reply(update, f"记下来的时候出了点问题：{truncate(str(exc), 1200)}")
        return
    await _reply(update, summary)


async def _reply(
    update: Update,
    text: str,
    *,
    reply_markup=None,
) -> None:
    if not update.effective_message:
        return
    await update.effective_message.reply_text(
        truncate(text),
        disable_web_page_preview=True,
        reply_markup=reply_markup,
    )


def _allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == settings.telegram_allowed_user_id)


async def _guard(update: Update) -> bool:
    if _allowed(update):
        return True
    user = update.effective_user
    logger.warning("Rejected unauthorized Telegram user id=%s username=%s", getattr(user, "id", None), getattr(user, "username", None))
    if update.effective_message:
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
    await _start_job(update, JobMode.RUN, prompt)


async def fix_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    prompt = _prompt(context)
    if not prompt:
        await _reply(update, "Usage: /fix <prompt>")
        return
    await _start_job(update, JobMode.FIX, prompt)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _reply(update, runner.status_text())


async def diff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _reply(update, await runner.diff_text())


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _reply(update, await runner.cancel())


async def jobs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    raw_limit = context.args[0] if context.args else "8"
    try:
        limit = max(1, min(30, int(raw_limit)))
    except ValueError:
        limit = 8
    await _reply(update, runner.jobs_text(limit))


async def last_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _reply(update, runner.last_text())


async def clean_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    raw_keep = context.args[0] if context.args else "20"
    try:
        keep = max(1, min(200, int(raw_keep)))
    except ValueError:
        keep = 20
    await _reply(update, await runner.clean_old_jobs(keep))


async def maintain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    raw_keep = context.args[0] if context.args else "50"
    try:
        keep = max(1, min(500, int(raw_keep)))
    except ValueError:
        keep = 50
    try:
        outcome = await run_maintenance(".env", "codex-telegram-bot", clean_threshold=100, keep=keep)
    except Exception as exc:
        await _reply(update, f"maintain 没跑成：{truncate(str(exc), 1200)}")
        return
    await _reply(update, outcome.summary)


async def discard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _reply(update, await runner.discard_last_job())


async def apply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _reply(update, await runner.apply_last_job())


async def doctor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    results = [
        check_systemd_active("codex-telegram-bot"),
        check_workspace(settings),
        check_minimax_models(settings),
        check_disk(settings.codex_task_root),
    ]
    results.extend(check_runtime_dirs(settings))
    results.extend(check_latest_job(settings))
    lines = [result.line() for result in results]
    await _reply(update, "\n".join(lines))


async def diag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    since = " ".join(context.args) if context.args else "1 hour ago"
    try:
        text = diagnostics_report(".env", "codex-telegram-bot", since, metrics_limit=20)
    except Exception as exc:
        await _reply(update, f"diag 没跑成：{truncate(str(exc), 1200)}")
        return
    await _reply(update, text)


async def security_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    since = " ".join(context.args) if context.args else "1 hour ago"
    try:
        results = run_security_audit(".env", "codex-telegram-bot", since)
    except Exception as exc:
        await _reply(update, f"security 没跑成：{truncate(str(exc), 1200)}")
        return
    await _reply(update, "\n".join(result.line() for result in results))


async def ratelimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    raw_limit = context.args[0] if context.args else "5"
    try:
        limit = max(1, min(20, int(raw_limit)))
    except ValueError:
        limit = 5
    await _reply(update, rate_limit_report(".env", limit))


async def audit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    raw_stale = context.args[0] if context.args else "90"
    try:
        stale_minutes = max(1, min(24 * 60, int(raw_stale)))
    except ValueError:
        stale_minutes = 90
    try:
        results = run_job_audit(".env", stale_minutes)
    except Exception as exc:
        await _reply(update, f"audit 没跑成：{truncate(str(exc), 1200)}")
        return
    await _reply(update, "\n".join(result.line() for result in results))


async def log_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    selector = context.args[0] if context.args else "latest"
    try:
        text = summarize_log(".env", selector, limit=12)
    except Exception as exc:
        await _reply(update, f"log 没读成：{truncate(str(exc), 1200)}")
        return
    await _reply(update, text)


async def meta_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    selector = context.args[0] if context.args else "latest"
    try:
        text = metadata_report(".env", selector)
    except Exception as exc:
        await _reply(update, f"meta 没读成：{truncate(str(exc), 1200)}")
        return
    await _reply(update, text)


async def metrics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    raw_limit = context.args[0] if context.args else "20"
    try:
        limit = max(1, min(100, int(raw_limit)))
    except ValueError:
        limit = 20
    await _reply(update, metrics_report(".env", limit))


def _health_summary(snapshot: dict) -> str:
    latest = snapshot.get("latest_job") if isinstance(snapshot.get("latest_job"), dict) else {}
    metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
    checks = snapshot.get("checks") if isinstance(snapshot.get("checks"), dict) else {}
    offline = checks.get("offline_harnesses", [])
    offline_status = " ".join(
        f"{item.get('name')}={'ok' if item.get('ok') else 'fail'}"
        for item in offline
        if isinstance(item, dict)
    ) or "none"
    triage = snapshot.get("triage") if isinstance(snapshot.get("triage"), list) else []
    failed_checks = [
        item
        for group in checks.values()
        if isinstance(group, list)
        for item in group
        if isinstance(item, dict) and not item.get("ok")
    ]
    if not snapshot.get("ok"):
        lines = ["Health: failed"]
        if failed_checks:
            lines.append("Failing checks:")
            lines.extend(f"- {item.get('name', 'check')}: {item.get('detail', '')}" for item in failed_checks[:6])
        if triage:
            lines.append("Triage:")
            lines.extend(str(item) for item in triage[:4])
        else:
            lines.append("Triage: Run /diag for details.")
        lines.append(f"Recent: jobs={metrics.get('count', 0)} success={metrics.get('success_rate', 0)}% rate_limits={metrics.get('rate_limit_hits', 0)}")
        return "\n".join(lines)

    lines = [
        f"Health: {'ok' if snapshot.get('ok') else 'failed'}",
        f"Latest: {latest.get('id', '(none)')} · {latest.get('state', 'unknown')} · {latest.get('summary', '')}",
        f"Recent: jobs={metrics.get('count', 0)} success={metrics.get('success_rate', 0)}% rate_limits={metrics.get('rate_limit_hits', 0)}",
        f"Offline: {offline_status}",
    ]
    if triage:
        lines.append("Triage:")
        lines.extend(str(item) for item in triage[:4])
    else:
        lines.append("Triage: No failing checks.")
    return "\n".join(lines)


async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    raw_args = [arg.lower() for arg in (context.args or [])]
    compact_json = "json" in raw_args
    full = "full" in raw_args
    try:
        snapshot = health_snapshot(
            ".env",
            "codex-telegram-bot",
            "1 hour ago",
            metrics_limit=20,
            include_security=full and "nosecurity" not in raw_args,
            include_offline=full,
        )
    except Exception as exc:
        await _reply(update, f"health 没跑成：{truncate(str(exc), 1200)}")
        return
    if compact_json:
        await _reply(update, json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
        return
    await _reply(update, _health_summary(snapshot))


async def smoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _reply(update, "开始 smoke。它会跑一条最小 MiniMax/Codex 端到端测试。")
    try:
        code = await run_smoke(".env", "codex-telegram-bot", notify=False)
    except Exception as exc:
        await _reply(update, f"smoke 没跑成：{truncate(str(exc), 1200)}")
        return
    await _reply(update, "smoke 通过。" if code == 0 else "smoke 失败，发 /doctor 看细节。")


async def editcheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await _reply(update, "开始 editcheck。它会建临时 repo，让 Codex 真改一个文件，然后自动验收和清理。")
    try:
        outcome = await run_edit_harness(".env", notify=False)
    except Exception as exc:
        await _reply(update, f"editcheck 没跑成：{truncate(str(exc), 1200)}")
        return
    await _reply(update, outcome.summary)


# --- Onboarding-C: first-run /onboard conversation + /profile view ----
# When the user fires the bot for the first time (no
# codex_memory_root/operator.json yet), /onboard walks them through
# a 3-step identity setup: name (free text), language (3 button
# choices + free text fallback), style (3 button choices). Answers
# are saved to operator.json which load_settings reads on next
# startup. The conversation uses python-telegram-bot\'s
# ConversationHandler so the per-chat state lives in the bot
# runtime; settings persistence is via the JSON file (bot restart
# does not lose progress). /skip ends the conversation without
# writing operator.json — the .env defaults stay in effect.
# /profile shows the current 4-field profile and points at /onboard
# to re-run. The intent is a Hermes-style first-run experience:
# light-touch 3 questions, persistent across restarts, editable
# later, no ceremony, no forced commitment.

ONBOARDING_NAME, ONBOARDING_LANG, ONBOARDING_STYLE = range(3)
OPERATOR_PROFILE_FILENAME = "operator.json"


def _operator_profile_path() -> Path:
    return settings.codex_memory_root / OPERATOR_PROFILE_FILENAME


def _operator_profile_exists() -> bool:
    return _operator_profile_path().exists()


def _save_operator_profile(data: dict) -> bool:
    """Write the operator.json with the 4 known fields. Returns True
    on success, False on OSError. Stale or unknown fields from
    older /onboard runs are silently dropped (the loader only
    returns the 4 known keys)."""
    path = _operator_profile_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({k: data[k] for k in OPERATOR_PROFILE_FIELDS if k in data}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return True
    except OSError:
        logger.exception("Failed to write operator.json")
        return False


async def onboard_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _guard(update):
        return ConversationHandler.END
    # The same entry point serves two surfaces: the explicit
    # /onboard command and the inline "开始 onboarding" button
    # (callback_data="ob:start") on the first-run welcome. For the
    # callback case we have to answer the callback query so Telegram
    # dismisses the button's loading indicator before the next
    # message arrives; _reply uses effective_message.reply_text
    # which is the same for both Update paths so no branching
    # needed downstream.
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
    # Default standing from project assumption; /profile shows it
    # so the user can change it later if they want to.
    context.user_data["onboarding_draft"]["operator_standing"] = settings.operator_standing or "personal-scale, single operator"
    saved = _save_operator_profile(context.user_data["onboarding_draft"])
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
    if not _operator_profile_exists():
        await _reply(
            update,
            "暂无 `operator.json`，用的是 .env 默认值。\n"
            "跑 `/onboard` 创建一份（重启后生效）。",
        )
        return
    try:
        content = _operator_profile_path().read_text(encoding="utf-8")
        data = json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        await _reply(update, f"读 operator.json 失败：{exc}")
        return
    text = "当前 profile（`codex_memory_root/operator.json`）：\n"
    for key, label in (
        ("operator_name", "name"),
        ("operator_language", "language"),
        ("operator_style", "style"),
        ("operator_standing", "standing"),
    ):
        val = data.get(key)
        text += f"  {label}: {val if val is not None else '(unset)'}\n"
    text += "\n重做问卷 `/onboard`。"
    await _reply(update, text)


async def text_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    # Onboarding-C: first-run nudge. If the user types ANY message
    # before running /onboard, surface the prompt instead of
    # silently starting a job. They can still /onboard later to
    # set their identity; the prompt just doesn't let the first
    # message get lost in a job they didn't intend.
    if not _operator_profile_exists():
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
    prompt = (message.text if message and message.text else "").strip()
    if not prompt:
        return
    if _detect_memory_intent(prompt):
        await _handle_memo_fast_path(update, prompt)
        return
    await _start_job(update, JobMode.RUN, prompt)


async def memo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    prompt = _prompt(context)
    if not prompt:
        await _reply(update, "Usage: /memo <something to remember>")
        return
    await _handle_memo_fast_path(update, prompt)


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
    if not _operator_profile_exists():
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
    await _reply(update, "你好！直接发消息给我就行，我会用我的方式回。改文件用 /fix，运维相关的命令用 /help。")


async def _typing_loop(app, chat_id: int) -> None:
    """Chat-bot feel: keep showing a typing indicator while Codex is thinking."""
    try:
        while True:
            await app.send_chat_action(chat_id=chat_id, action="typing")
            await asyncio.sleep(1.5)  # chat feel: keep typing pulse alive within Telegram 5s expiry
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Typing indicator failed")


async def _start_job(update: Update, mode: JobMode, prompt: str) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    app = update.get_bot()
    message = update.effective_message
    placeholder_id: int | None = None

    typing_task: asyncio.Task | None = None
    if chat_id is not None and message is not None:
        typing_task = asyncio.create_task(_typing_loop(app, chat_id))
        # Let the typing loop run for the lifetime of the job. The
        # placeholder edits the message in place, but Telegram's chat-list
        # typing pulse needs to keep firing in the gaps between codex
        # events — otherwise a long think looks frozen. Cancellation
        # happens in the `finally` block below when the job ends.
        try:
            placeholder_msg = await message.reply_text(
                "⏳ Got it, working on it...",
                disable_web_page_preview=True,
            )
            placeholder_id = getattr(placeholder_msg, "message_id", None)
        except Exception:
            logger.exception("Failed to send placeholder")
            placeholder_id = None

    # Latch: once an edit_message_text call raises (typically Telegram's
    # 20 edits/min/message limit), stop trying to edit the same placeholder
    # for the rest of this job. Falling back to send_message for every
    # subsequent update would scatter one-off messages through the chat;
    # latching keeps it to "placeholder stuck on first edit" + a chain of
    # new messages, which is the lesser evil.
    edit_broken = False
    # Round-7 no-op guard. Telegram's editMessageText 400s with
    # "Message is not modified" when the new content is byte-identical
    # to the current content. Even short runs can hit that on a tight
    # stream of similar summaries (e.g. two consecutive tool indicators
    # for the same call, or post-truncation collisions on long prose),
    # and the existing except latch would flip the rest of this job
    # into send_message mode and scatter one-off messages through the
    # chat. Compare post-truncation (the wire format) and short-circuit
    # when it matches the last successful delivery.
    last_progress_text: str | None = None

    async def progress(message_text: str) -> None:
        nonlocal edit_broken, last_progress_text
        if chat_id is None:
            return
        outgoing = truncate(message_text)
        if outgoing == last_progress_text:
            return
        if placeholder_id is not None and not edit_broken:
            try:
                await app.edit_message_text(
                    chat_id=chat_id,
                    message_id=placeholder_id,
                    text=outgoing,
                )
                last_progress_text = outgoing
                return
            except Exception:
                logger.exception("Failed to edit placeholder; latching to send mode")
                edit_broken = True
        try:
            await app.send_message(
                chat_id=chat_id,
                text=outgoing,
                disable_web_page_preview=True,
            )
            last_progress_text = outgoing
        except Exception:
            logger.exception("Failed to send Telegram progress update")

    try:
        await runner.start(mode, prompt, progress)
    except Exception as exc:
        if typing_task is not None and not typing_task.done():
            typing_task.cancel()
        if message is not None:
            try:
                await message.reply_text(
                    f"现在不能开始：{truncate(str(exc), 1200)}",
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception("Failed to send error reply")
    finally:
        if typing_task is not None and not typing_task.done():
            typing_task.cancel()


async def post_init(application: Application) -> None:
    await runner.validate()
    await application.bot.set_my_commands(
        [
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
        ]
    )
    logger.info("Codex Telegram bot ready. Workspace=%s task_root=%s", settings.codex_workspace_root, settings.codex_task_root)


def main() -> None:
    application = Application.builder().token(settings.telegram_bot_token).post_init(post_init).build()
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
    # Onboarding-C: ConversationHandler for /onboard. The
    # CallbackQueryHandler entries pick up the button presses
    # inside each step; the MessageHandler entry takes the free-text
    # name. /skip is a fallback that ends the conversation without
    # writing operator.json (the .env defaults stay in effect).
    application.add_handler(
        ConversationHandler(
            entry_points=[
                CommandHandler("onboard", onboard_start),
                # First-run welcome button (callback_data="ob:start")
                # drives the same conversation start as the
                # /onboard command. Pattern is anchored so a typo
                # callback from another handler can't accidentally
                # enter the conversation.
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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_cmd))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        asyncio.run(runner.cancel())
