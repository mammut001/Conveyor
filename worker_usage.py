"""Live usage (quota) readers for the worker pool.

Adapted from Kanjo (MIT), github.com/mammut001/kanjo, quota_fetch.py.

Each fetcher returns
    {"windows": [{"name": "5h"|"weekly", "used_percent": float, "resets_at": epoch}],
     "plan": str | None, "fetched_at": epoch}
or None when the provider is unavailable (no credentials, no local server,
network failure, unparsable answer).

Security rules (same as Kanjo):
* Tokens stay in memory, are only sent to the provider's own https host and are
  never logged or stored. Redirects to any other host are refused.
* 10 second timeout, response size cap, never raise out of a fetcher.
* The Antigravity language server is only ever contacted on 127.0.0.1.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("conveyor.workers.usage")

REQUEST_TIMEOUT = 10
MAX_RESPONSE_BYTES = 256 * 1024
STALE_AFTER_SECONDS = 600.0  # on-demand refresh before picking a worker
MIN_INTERVAL_SECONDS = 300
DEFAULT_INTERVAL_SECONDS = 3600

USAGE_WORKER_IDS = ("codex", "claude-code", "agy", "grok")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _pct(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(min(max(float(value), 0.0), 100.0), 1)


def _percentage(value: Any) -> float | None:
    """Claude style utilization: already percent (0..100)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    val = float(value)
    if not math.isfinite(val) or val < 0:
        return None
    if 0 < val <= 1.0:
        val *= 100.0
    return _pct(val)


def _parse_iso(text: Any) -> float | None:
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _roll_forward(reset: float, hours: int, now: float) -> float:
    period = hours * 3600
    return reset + period * (math.floor((now - reset) / period) + 1)


def _window(name: str, used: float | None, resets_at: float | None) -> dict[str, Any] | None:
    if used is None:
        return None
    return {
        "name": name,
        "used_percent": used,
        "resets_at": int(resets_at) if resets_at is not None else None,
    }


def _result(windows: list[dict[str, Any] | None], plan: Any, now: float) -> dict[str, Any] | None:
    clean = [w for w in windows if w]
    if not clean:
        return None
    plan_text = plan.strip() if isinstance(plan, str) and plan.strip() else None
    return {"windows": clean, "plan": plan_text, "fetched_at": int(now)}


def _read_json_file(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect that leaves the original https host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        old = urllib.parse.urlparse(req.full_url)
        new = urllib.parse.urlparse(newurl)
        if new.scheme != "https" or new.hostname != old.hostname:
            raise urllib.error.URLError("redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_get_json(
    url: str,
    host: str,
    headers: dict[str, str],
    secret_headers: dict[str, str],
    timeout: float = REQUEST_TIMEOUT,
) -> Any:
    """GET json from https://<host>/... only. Raises on any problem."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != host:
        raise ValueError("host not allowed")
    req = urllib.request.Request(url, headers=headers)
    for key, value in secret_headers.items():
        req.add_unredirected_header(key, value)
    opener = urllib.request.build_opener(_SameHostRedirect())
    with opener.open(req, timeout=timeout) as res:
        final = urllib.parse.urlparse(res.geturl())
        if final.scheme != "https" or final.hostname != host:
            raise ValueError("host not allowed")
        return json.loads(res.read(MAX_RESPONSE_BYTES).decode("utf-8"))


def _run_text(cmd: list[str], timeout: float = 3) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        ).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


# --------------------------------------------------------------------------
# grok
# --------------------------------------------------------------------------

def _load_grok_auth() -> str | None:
    data = _read_json_file(_home() / ".grok" / "auth.json")
    if not isinstance(data, dict):
        return None
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        token = entry.get("key") or entry.get("access_token")
        if isinstance(token, str) and token.strip():
            expires_at = entry.get("expires_at")
            if isinstance(expires_at, str):
                exp = _parse_iso(expires_at)
                if exp and exp <= time.time():
                    continue
            return token.strip()
    return None


def parse_grok_usage(data: Any, now: float) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    cfg = data.get("config")
    if not isinstance(cfg, dict):
        return None
    period = cfg.get("currentPeriod") or {}
    if not isinstance(period, dict):
        period = {}
    reset_ts = _parse_iso(period.get("end") or cfg.get("billingPeriodEnd"))
    pct_raw = cfg.get("creditUsagePercent")
    if "creditUsagePercent" not in cfg:
        has_period = bool(period.get("start") and reset_ts is not None)
        if cfg.get("isUnifiedBillingUser") is True and has_period:
            pct_raw = 0.0
    used = _pct(pct_raw)
    if reset_ts is None and used is None:
        return None
    if reset_ts is not None and reset_ts <= now:
        reset_ts = _roll_forward(reset_ts, 168, now)
        used = None
    plan = data.get("subscriptionTier") or data.get("plan") or (
        "SuperGrok" if cfg.get("isUnifiedBillingUser") else None
    )
    return _result([_window("weekly", used, reset_ts)], plan, now)


def fetch_grok_usage(now: float) -> dict[str, Any] | None:
    token = _load_grok_auth()
    if not token:
        return None
    try:
        data = _http_get_json(
            "https://cli-chat-proxy.grok.com/v1/billing?format=credits",
            "cli-chat-proxy.grok.com",
            {"Accept": "application/json", "User-Agent": "Conveyor"},
            {"Authorization": f"Bearer {token}"},
        )
    except Exception:
        return None
    return parse_grok_usage(data, now)


# --------------------------------------------------------------------------
# codex
# --------------------------------------------------------------------------

def _load_codex_auth() -> tuple[str, str | None] | None:
    data = _read_json_file(_home() / ".codex" / "auth.json")
    if not isinstance(data, dict):
        return None
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else data
    access = tokens.get("access_token") or tokens.get("accessToken")
    if not isinstance(access, str) or not access.strip():
        return None
    account = tokens.get("account_id") or tokens.get("accountId")
    return access.strip(), (str(account).strip() if account else None)


def _codex_window(raw: Any, default_hours: int, now: float, keep_expired: bool) -> tuple[float | None, float | None]:
    if not isinstance(raw, dict) or not raw:
        return None, None
    used = _pct(raw.get("used_percent"))
    secs = raw.get("limit_window_seconds")
    try:
        hours = int(round(float(secs) / 3600)) if secs else default_hours
    except (TypeError, ValueError):
        hours = default_hours
    hours = hours or default_hours
    reset = raw.get("reset_at")
    if reset is None and raw.get("reset_after_seconds") is not None:
        try:
            reset = now + float(raw["reset_after_seconds"])
        except (TypeError, ValueError):
            reset = None
    elif reset is not None:
        try:
            reset = float(reset)
        except (TypeError, ValueError):
            reset = None
    if reset is not None and reset <= now:
        if keep_expired:
            reset = _roll_forward(reset, hours, now)
        else:
            reset = None
        used = None
    return used, reset


def parse_codex_usage(data: Any, now: float) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    rate = data.get("rate_limit") or {}
    if not isinstance(rate, dict):
        return None
    primary = rate.get("primary_window") or {}
    secondary = rate.get("secondary_window") or {}
    if not primary and not secondary:
        return None
    sec_used, sec_reset = _codex_window(secondary, 168, now, True)
    pri_used, pri_reset = _codex_window(primary, 5, now, False)
    return _result(
        [_window("5h", pri_used, pri_reset), _window("weekly", sec_used, sec_reset)],
        data.get("plan_type"),
        now,
    )


def fetch_codex_usage(now: float) -> dict[str, Any] | None:
    auth = _load_codex_auth()
    if not auth:
        return None
    access, account = auth
    secret = {"Authorization": f"Bearer {access}"}
    if account:
        secret["ChatGPT-Account-Id"] = account
    try:
        data = _http_get_json(
            "https://chatgpt.com/backend-api/wham/usage",
            "chatgpt.com",
            {"Accept": "application/json", "User-Agent": "Conveyor"},
            secret,
        )
    except Exception:
        return None
    return parse_codex_usage(data, now)


# --------------------------------------------------------------------------
# claude code
# --------------------------------------------------------------------------

def _claude_token_from_blob(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth") or {}
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        return None
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        exp = float(expires_at)
        if exp < 10_000_000_000:
            exp *= 1000
        if exp <= (time.time() + 30) * 1000:
            return None
    return token.strip()


def _load_claude_credentials() -> str | None:
    """macOS Keychain first, then the credentials file used on Linux."""
    if sys.platform == "darwin":
        out = _run_text(
            ["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials", "-w"]
        ).strip()
        if out:
            try:
                token = _claude_token_from_blob(json.loads(out))
            except ValueError:
                token = None
            if token:
                return token
    token = _claude_token_from_blob(_read_json_file(_home() / ".claude" / ".credentials.json"))
    if token:
        return token
    return _claude_token_from_blob(_read_json_file(_home() / ".claude.json"))


def parse_claude_usage(data: Any, now: float) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    seven = data.get("seven_day") or {}
    five = data.get("five_hour") or {}
    if not isinstance(seven, dict) or not isinstance(five, dict) or (not seven and not five):
        return None
    sec_used = _percentage(seven.get("utilization"))
    sec_reset = _parse_iso(seven.get("resets_at"))
    if sec_reset is not None and sec_reset <= now:
        sec_reset = _roll_forward(sec_reset, 168, now)
        sec_used = None
    pri_used = _percentage(five.get("utilization"))
    pri_reset = _parse_iso(five.get("resets_at"))
    if pri_reset is not None and pri_reset <= now:
        pri_reset = None
        pri_used = None
    return _result([_window("5h", pri_used, pri_reset), _window("weekly", sec_used, sec_reset)], None, now)


def _claude_from_config(now: float) -> dict[str, Any] | None:
    data = _read_json_file(_home() / ".claude.json")
    if not isinstance(data, dict):
        return None
    cached = data.get("cachedUsageUtilization")
    util = cached.get("utilization") if isinstance(cached, dict) else None
    if not isinstance(util, dict):
        return None

    def _reset(key: str) -> float | None:
        item = util.get(key)
        return _parse_iso(item.get("resets_at")) if isinstance(item, dict) else None

    seven, five = _reset("seven_day"), _reset("five_hour")
    if (seven is None or seven <= now) and (five is None or five <= now):
        return None
    return parse_claude_usage(util, now)


def fetch_claude_usage(now: float) -> dict[str, Any] | None:
    token = _load_claude_credentials()
    if token:
        try:
            data = _http_get_json(
                "https://api.anthropic.com/api/oauth/usage?at_wall=1&skip_spend=1",
                "api.anthropic.com",
                {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "anthropic-beta": "oauth-2025-04-20",
                    "User-Agent": "claude-code/2.1.0",
                },
                {"Authorization": f"Bearer {token}"},
            )
            result = parse_claude_usage(data, now)
            if result:
                return result
        except Exception:
            pass
    return _claude_from_config(now)


# --------------------------------------------------------------------------
# antigravity (agy): local language server on 127.0.0.1
# --------------------------------------------------------------------------

_AGY_SUMMARY_PATH = "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
_AGY_STATUS_PATH = "/exa.language_server_pb.LanguageServerService/GetUserStatus"


def _ps_listing() -> str:
    if sys.platform == "darwin":
        return _run_text(["/bin/ps", "-ax", "-o", "pid=,command="])
    return _run_text(["ps", "-eo", "pid=,args="])


def find_antigravity_processes() -> list[dict[str, Any]]:
    env_token = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
    env_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
    if env_token:
        port = None
        if env_addr and ":" in env_addr:
            try:
                port = int(env_addr.split(":")[-1])
            except ValueError:
                port = None
        return [{"pid": 0, "csrf_token": env_token, "port": port}]

    found: list[dict[str, Any]] = []
    for line in _ps_listing().splitlines():
        match = re.match(r"^\s*(\d+)\s+(.+)$", line)
        if not match:
            continue
        pid, cmd = int(match.group(1)), match.group(2)
        lower = cmd.lower()
        if "language_server" not in lower and "language-server" not in lower:
            continue
        if "antigravity" not in lower and "gemini" not in lower and "codeium" not in lower:
            continue
        csrf_match = re.search(r"--csrf_token(?:=|\s+)([^\s]+)", cmd, re.IGNORECASE)
        port_match = re.search(r"--extension_server_port(?:=|\s+)(\d+)", cmd, re.IGNORECASE)
        csrf = csrf_match.group(1) if csrf_match else ""
        port = int(port_match.group(1)) if port_match else None
        if csrf or port:
            found.append({"pid": pid, "csrf_token": csrf, "port": port})
    return found


def _ports_from_lsof(pid: int) -> list[int]:
    for lsof in ("/usr/sbin/lsof", "/usr/bin/lsof"):
        if os.path.isfile(lsof) and os.access(lsof, os.X_OK):
            out = _run_text([lsof, "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", str(pid)], timeout=2)
            return [int(v) for v in re.findall(r":(\d+)\s+\(LISTEN\)", out)]
    return []


def _ports_from_ss(pid: int) -> list[int]:
    out = _run_text(["ss", "-ltnpH"], timeout=2)
    ports: list[int] = []
    for line in out.splitlines():
        if f"pid={pid}," not in line and f"pid={pid})" not in line:
            continue
        match = re.search(r"\s(?:\[[0-9a-fA-F:]+\]|[0-9.*]+):(\d+)\s", line)
        if match:
            ports.append(int(match.group(1)))
    return ports


def _ports_from_proc(pid: int) -> list[int]:
    """Listening TCP ports owned by pid, read from /proc (no external tools)."""
    try:
        inodes = set()
        fd_dir = Path(f"/proc/{pid}/fd")
        for entry in fd_dir.iterdir():
            try:
                target = os.readlink(entry)
            except OSError:
                continue
            match = re.match(r"socket:\[(\d+)\]", target)
            if match:
                inodes.add(match.group(1))
        if not inodes:
            return []
        ports: list[int] = []
        for name in ("tcp", "tcp6"):
            try:
                lines = Path(f"/proc/{pid}/net/{name}").read_text().splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                cols = line.split()
                if len(cols) > 9 and cols[3] == "0A" and cols[9] in inodes:
                    ports.append(int(cols[1].rsplit(":", 1)[1], 16))
        return ports
    except OSError:
        return []


def antigravity_ports(pid: int, known_port: int | None) -> list[int]:
    ports: list[int] = [known_port] if known_port else []
    if pid > 0:
        if sys.platform == "darwin":
            extra = _ports_from_lsof(pid)
        else:
            extra = _ports_from_ss(pid) or _ports_from_proc(pid)
        for port in extra:
            if port not in ports:
                ports.append(port)
    return ports


def _post_local_json(scheme: str, port: int, path: str, body: bytes, csrf: str, timeout: float) -> Any:
    """POST json to 127.0.0.1 only. Raises on any problem."""
    url = f"{scheme}://127.0.0.1:{int(port)}{path}"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Connect-Protocol-Version", "1")
    req.add_header("Content-Type", "application/json")
    if csrf:
        req.add_header("X-Codeium-Csrf-Token", csrf)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx)
    )
    with opener.open(req, timeout=min(timeout, 3)) as res:
        return json.loads(res.read(MAX_RESPONSE_BYTES).decode("utf-8"))


def parse_antigravity_summary(data: Any, now: float, plan_name: str | None = None) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    groups = data.get("groups")
    if not isinstance(groups, list):
        resp = data.get("response")
        groups = resp.get("groups") if isinstance(resp, dict) else None
    if not isinstance(groups, list):
        return None

    weekly: dict[str, Any] | None = None
    five: dict[str, Any] | None = None
    for group in groups:
        if not isinstance(group, dict):
            continue
        display = str(group.get("displayName") or "").lower()
        if "gemini" not in display:
            continue
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict) or bucket.get("disabled") is True:
                continue
            cadence = " ".join(
                str(bucket.get(k) or "") for k in ("window", "bucketId", "displayName")
            ).lower()
            remaining = bucket.get("remainingFraction")
            if remaining is None and isinstance(bucket.get("remaining"), dict):
                remaining = bucket["remaining"].get("remainingFraction")
            used = None
            if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
                used = _pct((1.0 - float(remaining)) * 100.0)
            reset_raw = bucket.get("resetTime")
            reset = None
            if isinstance(reset_raw, (int, float)) and not isinstance(reset_raw, bool):
                reset = float(reset_raw)
            elif isinstance(reset_raw, str):
                reset = _parse_iso(reset_raw)
            minutes = None
            for key in ("windowMinutes", "window_minutes"):
                if isinstance(bucket.get(key), (int, float)):
                    minutes = float(bucket[key])
                    break
            if any(k in cadence for k in ("weekly", "week", "7d")):
                weekly = {"reset": reset, "used": used, "hours": int(round(minutes / 60)) if minutes else 168}
            elif any(k in cadence for k in ("5h", "5-hour", "five hour", "five-hour")):
                five = {"reset": reset, "used": used, "hours": int(round(minutes / 60)) if minutes else 5}

    windows: list[dict[str, Any] | None] = []
    if weekly:
        reset, used = weekly["reset"], weekly["used"]
        if reset is not None and reset <= now:
            reset = _roll_forward(reset, weekly["hours"], now)
            used = None
        windows.append(_window("weekly", used, reset))
    if five:
        reset, used = five["reset"], five["used"]
        if reset is not None and reset <= now:
            reset, used = None, None
        windows.append(_window("5h", used, reset))
    return _result(windows, plan_name, now)


def fetch_antigravity_usage(now: float) -> dict[str, Any] | None:
    processes = find_antigravity_processes()
    if not processes:
        return None
    meta_body = json.dumps({"metadata": {
        "ideName": "antigravity", "extensionName": "antigravity",
        "ideVersion": "unknown", "locale": "en",
    }}).encode("utf-8")
    summary_body = json.dumps({"forceRefresh": True}).encode("utf-8")
    for proc in processes:
        csrf = proc.get("csrf_token") or ""
        for port in antigravity_ports(int(proc.get("pid") or 0), proc.get("port")):
            for scheme in ("http", "https"):
                try:
                    data = _post_local_json(scheme, port, _AGY_SUMMARY_PATH, summary_body, csrf, REQUEST_TIMEOUT)
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                plan_name = None
                try:
                    status = _post_local_json(scheme, port, _AGY_STATUS_PATH, meta_body, csrf, REQUEST_TIMEOUT)
                    if isinstance(status, dict):
                        tier = status.get("userTier") or {}
                        info = (status.get("planStatus") or {}).get("planInfo") or {}
                        plan_name = next(
                            (
                                v.strip()
                                for v in (
                                    tier.get("name"), info.get("planName"),
                                    info.get("planDisplayName"), info.get("displayName"),
                                )
                                if isinstance(v, str) and v.strip()
                            ),
                            None,
                        )
                except Exception:
                    pass
                result = parse_antigravity_summary(data, now, plan_name)
                if result:
                    return result
    return None


# --------------------------------------------------------------------------
# cache, refresh, background loop
# --------------------------------------------------------------------------

FETCHERS: dict[str, Callable[[float], dict[str, Any] | None]] = {
    "grok": fetch_grok_usage,
    "codex": fetch_codex_usage,
    "claude-code": fetch_claude_usage,
    "agy": fetch_antigravity_usage,
}

# worker id -> {"data": dict | None, "attempted_at": float}
_cache: dict[str, dict[str, Any]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def fetch_usage(worker_id: str, now: float | None = None) -> dict[str, Any] | None:
    fetcher = FETCHERS.get(worker_id)
    if fetcher is None:
        return None
    try:
        return fetcher(now if now is not None else time.time())
    except Exception:
        logger.debug("usage fetch failed for %s", worker_id, exc_info=False)
        return None


def get_usage(worker_id: str) -> dict[str, Any]:
    """Cached usage in the Worker payload shape (never touches the network)."""
    with _cache_lock:
        entry = _cache.get(worker_id)
        data = entry.get("data") if entry else None
    if not data:
        return {"windows": [], "plan": None, "fetched_at": None, "available": False}
    return {
        "windows": [dict(w) for w in data.get("windows", [])],
        "plan": data.get("plan"),
        "fetched_at": data.get("fetched_at"),
        "available": bool(data.get("windows")),
    }


def refresh_worker(worker_id: str) -> None:
    """Fetch now; a failed fetch keeps the previous value."""
    now = time.time()
    data = fetch_usage(worker_id, now)
    with _cache_lock:
        entry = _cache.setdefault(worker_id, {"data": None, "attempted_at": 0.0})
        entry["attempted_at"] = now
        if data:
            entry["data"] = data


def refresh_all() -> None:
    for worker_id in USAGE_WORKER_IDS:
        refresh_worker(worker_id)


def refresh_stale(worker_ids: list[str] | None = None, max_age: float = STALE_AFTER_SECONDS) -> None:
    """Refresh workers whose last attempt is older than max_age."""
    now = time.time()
    for worker_id in worker_ids or USAGE_WORKER_IDS:
        if worker_id not in FETCHERS:
            continue
        with _cache_lock:
            attempted = (_cache.get(worker_id) or {}).get("attempted_at", 0.0)
        if now - attempted > max_age:
            refresh_worker(worker_id)


def normalize_interval(value: Any) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = DEFAULT_INTERVAL_SECONDS
    return max(MIN_INTERVAL_SECONDS, seconds)


async def _refresh_loop(settings: Any) -> None:
    interval = normalize_interval(getattr(settings, "workers_usage_interval_seconds", DEFAULT_INTERVAL_SECONDS))
    while True:
        try:
            await asyncio.to_thread(refresh_all)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("usage refresh failed", exc_info=False)
        await asyncio.sleep(interval)


def start_background_refresh(loop: asyncio.AbstractEventLoop, settings: Any) -> "asyncio.Future[Any] | None":
    """Start the periodic refresh on `loop`; None when the worker pool is off."""
    if not getattr(settings, "workers_enabled", False):
        return None
    return asyncio.run_coroutine_threadsafe(_refresh_loop(settings), loop)
