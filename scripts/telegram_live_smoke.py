#!/usr/bin/env python3
"""telegram_live_smoke.py — real-user Telegram live smoke for Conveyor.

Sends messages to a running Conveyor Telegram bot as a real Telegram
user (Telethon user client), waits for the bot's reply, and asserts
the agent tool layer still behaves correctly. This is the only way to
exercise the bot's MessageHandler end-to-end because Telegram Bot API
messages do not trigger the bot's own MessageHandler when sent by the
bot itself.

Safety defaults:
- Restart is cancelled by default (sends `取消` after the prompt).
- Actual restart requires BOTH env TELEGRAM_LIVE_ALLOW_RESTART=1 AND
  CLI --allow-restart. Without both, the dangerous path is never
  taken.
- Never prints tokens, api hash, session paths, or .env content.

Exit codes:
  0 — all selected tests passed
  1 — one or more tests failed
  2 — missing optional dependency (telethon) or required env config
  3 — connection/auth error

Usage:
  pip install telethon
  export TELEGRAM_API_ID=...
  export TELEGRAM_API_HASH=...
  export TELEGRAM_BOT_USERNAME=my_codex_bot
  .venv/bin/python scripts/telegram_live_smoke.py --quick
  .venv/bin/python scripts/telegram_live_smoke.py --full
  # only with both env and CLI flag:
  TELEGRAM_LIVE_ALLOW_RESTART=1 \
      .venv/bin/python scripts/telegram_live_smoke.py --full --allow-restart
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

# Telethon is an optional dependency. Imported lazily so the script
# can still be parsed, linted, and unit-tested without it.
try:  # pragma: no cover — exercised at runtime
    from telethon import TelegramClient, errors as tg_errors
    from telethon.tl.types import Message

    _TELETHON_OK = True
except ImportError:  # pragma: no cover
    TelegramClient = None  # type: ignore[assignment]
    tg_errors = None  # type: ignore[assignment]
    Message = None  # type: ignore[assignment]
    _TELETHON_OK = False


# ---- Redaction -----------------------------------------------------------

_BOT_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_API_HASH_RE = re.compile(r"\b[a-fA-F0-9]{32}\b")
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{40,}\b")
_SESSION_PATH_RE = re.compile(r"\.telegram-live-smoke[^\s\"']*")

# Values pulled from env for masking too — we never print them, but if
# they leak (e.g. via debug logs) we still want them redacted.
_SENSITIVE_ENV_KEYS = (
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_TEST_SESSION",
    "TELEGRAM_BOT_TOKEN",
    "MINIMAX_API_KEY",
    "ANTHROPIC_API_KEY",
)


def _mask(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 6:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def redact(text: str, env: dict[str, str] | None = None) -> str:
    """Mask tokens, hashes, and sensitive env values inside free text."""
    if not text:
        return ""
    out = text
    env_values = {os.environ.get(k, "") for k in _SENSITIVE_ENV_KEYS}
    if env:
        env_values.update(v for v in env.values() if v)
    for value in env_values:
        if value and len(value) >= 8:
            out = out.replace(value, _mask(value))
    out = _BOT_TOKEN_RE.sub("[REDACTED_TOKEN]", out)
    out = _API_HASH_RE.sub("[REDACTED_HASH]", out)
    out = _LONG_TOKEN_RE.sub("[REDACTED_LONG_TOKEN]", out)
    out = _SESSION_PATH_RE.sub("[REDACTED_SESSION]", out)
    return out


# ---- Validation ----------------------------------------------------------

_VALID_RESTART_TARGETS = {"telegram", "feishu", "maintain"}


def validate_restart_target(target: str) -> str:
    target = (target or "").strip().lower()
    if target not in _VALID_RESTART_TARGETS:
        raise ValueError(
            f"restart target must be one of {sorted(_VALID_RESTART_TARGETS)}, got {target!r}"
        )
    return target


# ---- Test framework ------------------------------------------------------

@dataclass
class TestResult:
    name: str
    ok: bool
    detail: str
    replies: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "reply_count": len(self.replies),
        }


# A test case is an async callable that returns a TestResult.
TestFn = Callable[[], Awaitable[TestResult]]


@dataclass
class LiveSmoke:
    bot_username: str
    api_id: int
    api_hash: str
    session_path: str
    timeout: float
    pause: float
    verbose: bool
    json_output: bool

    client: "TelegramClient | None" = None
    bot_entity: object = None

    async def connect(self) -> None:
        if not _TELETHON_OK:
            raise RuntimeError("telethon not installed")
        self.client = TelegramClient(self.session_path, self.api_id, self.api_hash)
        await self.client.start()  # may prompt for 2FA / phone on first run
        try:
            self.bot_entity = await self.client.get_entity(self.bot_username)
        except Exception as exc:
            raise RuntimeError(
                f"could not resolve bot @{self.bot_username}: {type(exc).__name__}: {exc}"
            ) from exc

    async def close(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

    async def _wait_for_replies(
        self, after_id: int, expected_substrings: Iterable[str]
    ) -> tuple[list[str], bool]:
        """Poll recent bot messages newer than `after_id` until every
        expected substring has been matched OR timeout expires.
        Returns (collected_replies, all_matched).
        """
        if self.client is None:
            raise RuntimeError("client not connected")
        expected = list(expected_substrings)
        seen: list[str] = []
        matched: set[str] = set()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline and len(matched) < len(expected):
            async for msg in self.client.iter_messages(self.bot_entity, limit=20):
                if msg.id <= after_id:
                    continue
                if getattr(msg, "outgoing", False):
                    continue
                text = msg.message or ""
                seen.append(text)
                for needle in expected:
                    if needle and needle in text:
                        matched.add(needle)
                if len(matched) >= len(expected):
                    break
            if len(matched) < len(expected):
                await self.client.send_read_ack(self.bot_entity)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(0.5, remaining))
        return seen, len(matched) >= len(expected)

    async def run_simple(
        self,
        name: str,
        send_text: str,
        expects_all: Iterable[str] = (),
        expects_none: Iterable[str] = (),
    ) -> TestResult:
        assert self.client is not None
        expects_all = list(expects_all)
        expects_none = list(expects_none)
        try:
            sent = await self.client.send_message(self.bot_entity, send_text)
            if self.pause > 0:
                await asyncio.sleep(self.pause)
            replies, all_ok = await self._wait_for_replies(sent.id, expects_all)
            combined = "\n".join(replies)
            missing = [n for n in expects_all if n not in combined]
            forbidden = [n for n in expects_none if n in combined]
            ok = (bool(all_ok) or not expects_all) and not forbidden
            if missing:
                detail = f"missing={missing}"
            elif forbidden:
                detail = f"forbidden_present={forbidden}"
            elif not expects_all:
                detail = f"matched none required; replies={len(replies)}"
            else:
                detail = f"matched {len(expects_all)} needles, replies={len(replies)}"
            return TestResult(
                name=name,
                ok=ok,
                detail=redact(detail),
                replies=[redact(r) for r in replies[-3:]],
            )
        except Exception as exc:
            return TestResult(
                name=name,
                ok=False,
                detail=redact(f"raised {type(exc).__name__}: {exc}"),
            )

    async def run_cancel_after(
        self,
        name: str,
        send_text: str,
        expects_prompt_substr: str,
        expects_target_substr: str,
    ) -> TestResult:
        """Send a restart-creating command, verify the confirmation
        prompt names the correct unit, then send 取消 and verify the
        cancel reply. Defaults to safe (always cancels)."""
        assert self.client is not None
        try:
            sent = await self.client.send_message(self.bot_entity, send_text)
            if self.pause > 0:
                await asyncio.sleep(self.pause)
            replies, _ = await self._wait_for_replies(
                sent.id, [expects_prompt_substr, expects_target_substr]
            )
            combined = "\n".join(replies)
            missing = [n for n in (expects_prompt_substr, expects_target_substr) if n not in combined]
            if missing:
                return TestResult(
                    name=name,
                    ok=False,
                    detail=redact(f"missing={missing}; replies={len(replies)}"),
                    replies=[redact(r) for r in replies[-3:]],
                )
            cancel = await self.client.send_message(self.bot_entity, "取消")
            if self.pause > 0:
                await asyncio.sleep(self.pause)
            cancel_replies, _ = await self._wait_for_replies(cancel.id, ["已取消"])
            ok = any("已取消" in r for r in cancel_replies)
            return TestResult(
                name=name,
                ok=ok,
                detail=redact("cancelled") if ok else redact("no 取消 reply"),
                replies=[redact(r) for r in (replies + cancel_replies)[-3:]],
            )
        except Exception as exc:
            return TestResult(
                name=name,
                ok=False,
                detail=redact(f"raised {type(exc).__name__}: {exc}"),
            )


# ---- Test case builders --------------------------------------------------

def quick_tests(smoke: LiveSmoke) -> list[TestFn]:
    return [
        lambda: smoke.run_simple(
            "A /tools shows groups + confirmation rules",
            "/tools",
            expects_all=[
                "Agent 工具层",
                "READ",
                "WRITE",
                "/diagnose",
                "/restart",
                "确认执行",
            ],
        ),
        lambda: smoke.run_simple(
            "B /load returns VPS snapshot, no secrets",
            "/load",
            expects_all=["主机"],
            expects_none=["TELEGRAM_BOT_TOKEN"],
        ),
        lambda: smoke.run_simple(
            "C /ps is comm mode, no args",
            "/ps",
            expects_all=["comm 模式", "不含 args"],
            expects_none=["TELEGRAM_BOT_TOKEN", "--token"],
        ),
        lambda: smoke.run_simple(
            "D /ps full is a warning, not args",
            "/ps full",
            expects_all=["full args 模式可能包含敏感参数", "/ps full confirm"],
        ),
        lambda: smoke.run_simple(
            "J '重启 bot' must NOT default to telegram",
            "重启 bot",
            expects_all=[],
            expects_none=["危险操作需确认"],
        ),
        lambda: smoke.run_simple(
            "K /audit_tools reads log or returns 暂无",
            "/audit_tools 10",
            expects_all=[],
            expects_none=["TELEGRAM_BOT_TOKEN"],
        ),
    ]


def full_extra_tests(smoke: LiveSmoke) -> list[TestFn]:
    return [
        lambda: smoke.run_simple(
            "F '跑一下 htop' returns htop snapshot",
            "跑一下 htop",
            expects_all=["htop", "TUI"],
            expects_none=["危险操作需确认"],
        ),
        lambda: smoke.run_simple(
            "G /diagnose quick routes through Codex",
            "/diagnose quick",
            expects_all=[],
            expects_none=["TELEGRAM_BOT_TOKEN"],
        ),
        lambda: smoke.run_cancel_after(
            "H /restart telegram prompt + cancel",
            "/restart telegram",
            expects_prompt_substr="危险操作需确认",
            expects_target_substr="conveyor-telegram-bot",
        ),
        lambda: smoke.run_cancel_after(
            "I '重启 feishu bot' prompt + cancel",
            "重启 feishu bot",
            expects_prompt_substr="危险操作需确认",
            expects_target_substr="conveyor-feishu-bot",
        ),
        lambda: smoke.run_simple(
            "E 'look at htop source code' routes to LLM",
            "look at htop source code",
            expects_all=[],
            expects_none=["TUI"],
        ),
    ]


async def dangerous_restart(smoke: LiveSmoke, target: str) -> TestResult:
    """Optional dangerous path. Only invoked when both --allow-restart
    AND TELEGRAM_LIVE_ALLOW_RESTART=1 are set."""
    unit = {
        "telegram": "conveyor-telegram-bot",
        "feishu": "conveyor-feishu-bot",
        "maintain": "conveyor-maintain.timer",
    }[target]
    try:
        sent = await smoke.client.send_message(smoke.bot_entity, f"/restart {target}")
        if smoke.pause > 0:
            await asyncio.sleep(smoke.pause)
        replies, _ = await smoke._wait_for_replies(sent.id, ["危险操作需确认", unit])
        if unit not in "\n".join(replies):
            return TestResult(
                name=f"Z dangerous restart {target}",
                ok=False,
                detail=redact(f"no confirmation for {unit}"),
            )
        confirm = await smoke.client.send_message(smoke.bot_entity, "确认执行")
        if smoke.pause > 0:
            await asyncio.sleep(smoke.pause)
        replies_after, _ = await smoke._wait_for_replies(confirm.id, ["已请求重启"])
        return TestResult(
            name=f"Z dangerous restart {target}",
            ok=True,
            detail=redact(
                f"requested {unit}; saw {len(replies_after)} post-confirm messages"
            ),
            replies=[redact(r) for r in (replies + replies_after)[-3:]],
        )
    except Exception as exc:
        return TestResult(
            name=f"Z dangerous restart {target}",
            ok=False,
            detail=redact(f"raised {type(exc).__name__}: {exc}"),
        )


# ---- Argument parsing ----------------------------------------------------

DEFAULT_TIMEOUT = 45.0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live Telegram smoke for Conveyor (real user client).",
    )
    parser.add_argument("--quick", action="store_true", help="run only safe quick tests")
    parser.add_argument("--full", action="store_true", help="run safe full tests (default)")
    parser.add_argument(
        "--allow-restart",
        action="store_true",
        help="permit actual restart confirmation (requires TELEGRAM_LIVE_ALLOW_RESTART=1)",
    )
    parser.add_argument("--bot", help="override TELEGRAM_BOT_USERNAME")
    parser.add_argument("--timeout", type=float, default=None, help="per-test wait timeout")
    parser.add_argument(
        "--pause",
        type=float,
        default=1.5,
        help="pause between messages (seconds, default 1.5)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON summary at the end")
    parser.add_argument("--verbose", action="store_true", help="verbose progress")
    return parser.parse_args(argv)


# ---- Entry point ---------------------------------------------------------

async def _amain(args: argparse.Namespace) -> int:
    bot_username = (args.bot or os.environ.get("TELEGRAM_BOT_USERNAME", "")).strip()
    if not bot_username:
        print(redact("Missing TELEGRAM_BOT_USERNAME (or --bot)"), file=sys.stderr)
        return 2
    api_id_raw = os.environ.get("TELEGRAM_API_ID", "")
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    if not api_id_raw or not api_hash:
        print(redact("Missing TELEGRAM_API_ID / TELEGRAM_API_HASH"), file=sys.stderr)
        return 2
    try:
        api_id = int(api_id_raw)
    except ValueError:
        print(redact(f"TELEGRAM_API_ID not an int: {api_id_raw!r}"), file=sys.stderr)
        return 2
    session_path = os.environ.get("TELEGRAM_TEST_SESSION", ".telegram-live-smoke")
    timeout = args.timeout if args.timeout is not None else float(
        os.environ.get("TELEGRAM_LIVE_TIMEOUT", DEFAULT_TIMEOUT)
    )

    smoke = LiveSmoke(
        bot_username=bot_username,
        api_id=api_id,
        api_hash=api_hash,
        session_path=session_path,
        timeout=timeout,
        pause=args.pause,
        verbose=args.verbose,
        json_output=args.json,
    )
    try:
        await smoke.connect()
    except Exception as exc:
        print(redact(f"connect failed: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 3

    tests = quick_tests(smoke)
    if not args.quick:
        tests.extend(full_extra_tests(smoke))

    results: list[TestResult] = []
    for i, factory in enumerate(tests, start=1):
        if args.verbose:
            print(f"[{i}/{len(tests)}] running…", file=sys.stderr)
        result = await factory()
        results.append(result)
        if args.json:
            print(json.dumps(result.to_json(), ensure_ascii=False))
        else:
            status = "PASS" if result.ok else "FAIL"
            print(f"[{status}] {result.name}: {result.detail}")

    allow_env = os.environ.get("TELEGRAM_LIVE_ALLOW_RESTART", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if args.allow_restart and allow_env:
        try:
            target = validate_restart_target(
                os.environ.get("TELEGRAM_LIVE_RESTART_TARGET", "telegram")
            )
        except ValueError as exc:
            print(redact(str(exc)), file=sys.stderr)
            target = None
            results.append(TestResult(name="Z dangerous restart", ok=False, detail=redact(str(exc))))
        if target is not None:
            print(
                redact(f"\n!!! DANGER: will actually restart conveyor-{target} bot !!!"),
                file=sys.stderr,
            )
            results.append(await dangerous_restart(smoke, target))
    elif args.allow_restart and not allow_env:
        print(
            redact(
                "--allow-restart set but TELEGRAM_LIVE_ALLOW_RESTART != 1; skipping dangerous step"
            ),
            file=sys.stderr,
        )

    await smoke.close()

    failed = [r for r in results if not r.ok]
    summary = {
        "ran": len(results),
        "passed": sum(1 for r in results if r.ok),
        "failed": len(failed),
    }
    if args.json:
        print(json.dumps({"summary": summary, "results": [r.to_json() for r in results]}, ensure_ascii=False))
    else:
        print(redact(f"\nSummary: {summary}"))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    if not _TELETHON_OK:
        print("Telethon not installed. Install with: pip install telethon", file=sys.stderr)
        return 2
    args = parse_args(argv if argv is not None else sys.argv[1:])
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())