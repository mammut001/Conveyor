from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from conveyor.agents import worker_usage

logger = logging.getLogger("conveyor.workers")

class ModelsUnsupportedError(Exception):
    pass


class ModelsFetchError(Exception):
    pass


class UnknownModelError(Exception):
    pass


class BadThresholdError(Exception):
    pass


_REPO_ROOT = Path(__file__).resolve().parents[2]
PI_NEEDS_PROVIDER_HINT = "先在「设置 → 模型设置」里填服务商"

DEFAULT_THRESHOLD_PERCENT = 95
SWITCH_LOG_LIMIT = 20
SWITCH_REASONS = ("threshold", "quota_error", "unavailable")


CATALOG: list[dict[str, Any]] = [
    {
        "id": "codex",
        "label": "Codex",
        "bin": "codex",
        "install_command": "npm install -g @openai/codex",
        "login_command": "codex login",
        "auth_type": "cmd",
        "auth_command": ["codex", "login", "status"],
        "runnable": True,
        "backend": "codex",
        "models_command": None,
        "models": None,
    },
    {
        "id": "claude-code",
        "label": "Claude Code",
        "bin": "claude",
        "install_command": "npm install -g @anthropic-ai/claude-code",
        "login_command": "claude auth login",
        "auth_type": "cmd",
        "auth_command": ["claude", "auth", "status"],
        "runnable": True,
        "backend": "claude-code",
        "models_command": None,
        "models": ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"],
    },
    {
        "id": "pi",
        "label": "Pi（内置）",
        "bin": "pi",
        "install_command": "bash scripts/install_pi.sh",
        # Fixed argv run from the repo root (never user input).
        "install_argv": ["bash", "scripts/install_pi.sh"],
        "login_command": None,
        "auth_type": "none",
        "auth_command": None,
        "runnable": True,
        "backend": "pi",
        "models_command": None,
        # Models come from the saved 模型设置 provider, not from the catalog.
        "models_source": "provider",
        "models": None,
    },
    {
        "id": "agy",
        "label": "Antigravity",
        "bin": "agy",
        "install_command": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
        "login_command": "agy",
        "auth_type": "cmd",
        "auth_command": ["agy", "models"],
        "runnable": True,
        "backend": "agy",
        "models_command": ["agy", "models"],
        "models": None,
    },
    {
        "id": "cmd",
        "label": "Command Code",
        "bin": "cmd",
        "install_command": "npm install -g command-code",
        "login_command": "cmd login",
        "auth_type": "cmd",
        "auth_command": ["cmd", "whoami"],
        "runnable": True,
        "backend": "cmd",
        "models_command": ["cmd", "--list-models"],
        "models": None,
    },
    {
        "id": "grok",
        "label": "Super Grok",
        "bin": "grok",
        "install_command": "curl -fsSL https://x.ai/cli/install.sh | bash",
        "login_command": "grok login",
        "auth_type": "cmd",
        "auth_command": ["grok", "models"],
        "runnable": True,
        "backend": "grok",
        "models_command": ["grok", "models"],
        "models": None,
    },
]

_CATALOG_BY_ID = {item["id"]: item for item in CATALOG}

_detection_cache: dict[str, dict[str, Any]] = {}
_detection_cached_at: float = 0.0

DEFAULT_COOLING_SECONDS = 45 * 60

QUOTA_PATTERN = re.compile(
    r"RESOURCE_EXHAUSTED|quota|usage limit|rate limit|429|credit balance",
    re.IGNORECASE,
)
RESET_IN_PATTERN = re.compile(
    r"resets?\s+in\s+([0-9dhms\s]+)",
    re.IGNORECASE,
)
CMD_AUTH_FAIL_PATTERN = re.compile(
    r"401|Authentication failed|not logged",
    re.IGNORECASE,
)
AGY_AUTH_FAIL_PATTERN = re.compile(
    r"not logged in|login|authenticate|unauthorized|401",
    re.IGNORECASE,
)


def _is_agy_error_line(line: str) -> bool:
    lower = line.strip().lower()
    if not lower:
        return True
    return "error" in lower or bool(AGY_AUTH_FAIL_PATTERN.search(lower))


def get_catalog() -> list[dict[str, Any]]:
    return list(CATALOG)


def get_catalog_ids() -> list[str]:
    return [item["id"] for item in CATALOG]


def get_catalog_worker(worker_id: str) -> dict[str, Any] | None:
    return _CATALOG_BY_ID.get(worker_id)


def _find_binary(worker_def: dict[str, Any]) -> str | None:
    bin_name = worker_def["bin"]
    if worker_def["id"] == "pi":
        # CONVEYOR_PI_BIN > vendored binary > PATH (settings.pi_bin is not
        # reachable from detection, which is cached process-wide).
        from runner.pi_agent import resolve_pi_bin
        resolved = resolve_pi_bin(None)
        if os.path.sep in resolved:
            return resolved if Path(resolved).expanduser().is_file() else None
        return shutil.which(resolved)
    return shutil.which(bin_name)


def detect_worker(worker_def: dict[str, Any]) -> dict[str, Any]:
    bin_name = worker_def["bin"]
    which_path = _find_binary(worker_def)
    if not which_path:
        return {
            "installed": False,
            "version": None,
            "path": None,
            "auth": "unknown",
        }

    installed = True
    path = which_path
    version: str | None = None
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout:
            first_line = proc.stdout.strip().splitlines()[0].strip()
            version = first_line or None
    except Exception as exc:
        logger.debug("Failed checking version for %s: %s", bin_name, exc)
        version = None

    auth_type = worker_def.get("auth_type", "none")
    auth = "unknown"
    if auth_type == "cmd":
        auth_cmd = [path] + worker_def["auth_command"][1:]
        wid = worker_def["id"]
        try:
            proc = subprocess.run(
                auth_cmd,
                capture_output=True,
                text=True,
                timeout=20,
            )
            output = (proc.stdout or "") + " " + (proc.stderr or "")
            if wid == "agy":
                if proc.returncode == 0:
                    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
                    has_valid_line = any(not _is_agy_error_line(line) for line in lines)
                    auth = "ok" if has_valid_line else "unknown"
                else:
                    if AGY_AUTH_FAIL_PATTERN.search(output):
                        auth = "needs_login"
                    else:
                        auth = "unknown"
            elif wid == "cmd":
                if proc.returncode != 0 or CMD_AUTH_FAIL_PATTERN.search(output):
                    auth = "needs_login"
                else:
                    auth = "ok"
            elif wid == "grok":
                auth_file = Path(os.path.expanduser("~")) / ".grok" / "auth.json"
                if auth_file.is_file() and proc.returncode == 0:
                    auth = "ok"
                else:
                    auth = "needs_login"
            else:
                auth = "ok" if proc.returncode == 0 else "needs_login"
        except subprocess.TimeoutExpired:
            logger.debug("Auth check timed out for %s", bin_name)
            auth = "unknown" if wid == "agy" else "needs_login"
        except Exception as exc:
            logger.debug("Failed checking auth for %s: %s", bin_name, exc)
            auth = "unknown" if wid == "agy" else "needs_login"

    return {
        "installed": installed,
        "version": version,
        "path": path,
        "auth": auth,
    }


def detect_all_workers(force: bool = False) -> dict[str, dict[str, Any]]:
    global _detection_cache, _detection_cached_at
    now = time.time()
    if not force and _detection_cache and (now - _detection_cached_at < 60.0):
        return _detection_cache

    results: dict[str, dict[str, Any]] = {}
    for worker_def in CATALOG:
        results[worker_def["id"]] = detect_worker(worker_def)

    _detection_cache = results
    _detection_cached_at = now
    return _detection_cache


async def async_detect_all_workers(force: bool = False) -> dict[str, dict[str, Any]]:
    return await asyncio.to_thread(detect_all_workers, force)


def clear_detection_cache() -> None:
    global _detection_cache, _detection_cached_at
    _detection_cache = {}
    _detection_cached_at = 0.0


def parse_reset_seconds(text: str) -> int | None:
    match = RESET_IN_PATTERN.search(text)
    if not match:
        return None
    raw = match.group(1).strip()
    days = re.search(r"(\d+)\s*d", raw, re.IGNORECASE)
    hours = re.search(r"(\d+)\s*h", raw, re.IGNORECASE)
    minutes = re.search(r"(\d+)\s*m", raw, re.IGNORECASE)
    seconds = re.search(r"(\d+)\s*s", raw, re.IGNORECASE)

    total = 0
    if days:
        total += int(days.group(1)) * 86400
    if hours:
        total += int(hours.group(1)) * 3600
    if minutes:
        total += int(minutes.group(1)) * 60
    if seconds:
        total += int(seconds.group(1))

    return total if total > 0 else None


def classify_failure(text: str) -> tuple[str, int] | None:
    raw = str(text or "")
    is_quota = bool(QUOTA_PATTERN.search(raw))
    parsed_seconds = parse_reset_seconds(raw)

    if not is_quota and parsed_seconds is None:
        return None

    reset_seconds = parsed_seconds if parsed_seconds is not None else DEFAULT_COOLING_SECONDS
    return ("quota", reset_seconds)


def get_state_path(settings: Any) -> Path:
    root = getattr(settings, "codex_memory_root", None)
    if not root:
        root = Path(os.getenv("CODEX_MEMORY_ROOT", "~/.codex")).expanduser()
    return Path(root) / "state" / "workers.json"


def load_workers_state(settings: Any) -> dict[str, Any]:
    state_path = get_state_path(settings)
    default_order = [item["id"] for item in CATALOG]
    default_enabled = [item["id"] for item in CATALOG]
    state: dict[str, Any] = {
        "order": list(default_order),
        "enabled": list(default_enabled),
        "cooling": {},
        "models": {},
        "thresholds": {},
        "switch_log": [],
        "last_picked": None,
    }
    if state_path.is_file():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw_order = data.get("order")
                if isinstance(raw_order, list):
                    # Keep valid known order items, append any missing catalog items
                    seen = set()
                    cleaned_order = []
                    for wid in raw_order:
                        if wid in _CATALOG_BY_ID and wid not in seen:
                            cleaned_order.append(wid)
                            seen.add(wid)
                    for wid in default_order:
                        if wid not in seen:
                            cleaned_order.append(wid)
                            seen.add(wid)
                    state["order"] = cleaned_order

                raw_enabled = data.get("enabled")
                if isinstance(raw_enabled, list):
                    state["enabled"] = [wid for wid in raw_enabled if wid in _CATALOG_BY_ID]

                raw_cooling = data.get("cooling")
                if isinstance(raw_cooling, dict):
                    now = time.time()
                    cleaned_cooling = {}
                    for wid, cool_info in raw_cooling.items():
                        if isinstance(cool_info, dict):
                            reset_at = cool_info.get("reset_at")
                            if reset_at is None or now < reset_at:
                                cleaned_cooling[wid] = cool_info
                    state["cooling"] = cleaned_cooling

                raw_models = data.get("models")
                if isinstance(raw_models, dict):
                    state["models"] = {
                        str(k): (str(v) if v is not None else None)
                        for k, v in raw_models.items()
                        if k in _CATALOG_BY_ID
                    }

                raw_thresholds = data.get("thresholds")
                if isinstance(raw_thresholds, dict):
                    state["thresholds"] = {
                        str(k): int(v)
                        for k, v in raw_thresholds.items()
                        if k in _CATALOG_BY_ID
                        and isinstance(v, int)
                        and not isinstance(v, bool)
                        and 1 <= v <= 100
                    }

                raw_log = data.get("switch_log")
                if isinstance(raw_log, list):
                    state["switch_log"] = [e for e in raw_log if isinstance(e, dict)][-SWITCH_LOG_LIMIT:]

                last = data.get("last_picked")
                if isinstance(last, str) and last in _CATALOG_BY_ID:
                    state["last_picked"] = last
        except Exception as exc:
            logger.warning("Failed loading workers state from %s: %s", state_path, exc)

    return state


def save_workers_state(settings: Any, state: dict[str, Any]) -> None:
    state_path = get_state_path(settings)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.parent / f"workers.json.tmp.{os.getpid()}.{time.time()}"
    try:
        tmp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, state_path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def mark_cooling(
    settings: Any,
    worker_id: str,
    reset_seconds: int | float | None = None,
    reason: str | None = None,
) -> None:
    if reset_seconds is None or reset_seconds <= 0:
        reset_seconds = DEFAULT_COOLING_SECONDS
    state = load_workers_state(settings)
    cooling = state.setdefault("cooling", {})
    reset_at = int(time.time() + reset_seconds)
    cooling[worker_id] = {
        "reset_at": reset_at,
        "reason": reason or "quota cooling",
    }
    save_workers_state(settings, state)


def reset_quota(settings: Any, worker_id: str) -> None:
    state = load_workers_state(settings)
    cooling = state.get("cooling", {})
    if worker_id in cooling:
        del cooling[worker_id]
        save_workers_state(settings, state)


def _pi_provider_info(settings: Any) -> dict[str, Any]:
    """Saved 模型设置 provider as seen by the built-in Pi (no secrets)."""
    explicit = any(
        str(value or "").strip()
        for value in (
            os.getenv("CONVEYOR_PI_PROVIDER"),
            os.getenv("CONVEYOR_PI_MODEL"),
            getattr(settings, "pi_provider", ""),
            getattr(settings, "pi_model", ""),
        )
    )
    # Inputs the legacy PiBackend path already uses (DeepSeek key / chat-tier key).
    legacy_key = any(
        str(value or "").strip()
        for value in (
            os.getenv("DEEPSEEK_API_KEY"),
            os.getenv("CONVEYOR_CHAT_API_KEY"),
            getattr(settings, "chat_api_key", None),
        )
    )
    saved = None
    try:
        from conveyor.core.provider_config import get_saved_provider
        saved = get_saved_provider(settings)
    except Exception as exc:
        logger.debug("pi provider lookup failed: %s", type(exc).__name__)
    return {
        "configured": bool(saved) or explicit or legacy_key,
        "explicit": explicit,
        "provider_name": saved["provider_name"] if saved else None,
        "model": saved["model"] if saved else None,
    }


def _assemble_workers_payload(
    settings: Any,
    detections: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    state = load_workers_state(settings)
    order = state.get("order", [item["id"] for item in CATALOG])
    enabled_set = set(state.get("enabled", [item["id"] for item in CATALOG]))
    cooling = state.get("cooling", {})
    models_state = state.get("models", {})
    thresholds = state.get("thresholds", {})

    now = time.time()
    order_index = {wid: idx for idx, wid in enumerate(order)}

    workers_list: list[dict[str, Any]] = []
    for worker_def in CATALOG:
        wid = worker_def["id"]
        det = detections.get(wid, {})
        cool_info = cooling.get(wid)

        if cool_info and (cool_info.get("reset_at") is None or now < cool_info["reset_at"]):
            quota = {
                "state": "cooling",
                "reset_at": cool_info.get("reset_at"),
                "reason": cool_info.get("reason"),
            }
        else:
            quota = {
                "state": "available",
                "reset_at": None,
                "reason": None,
            }

        auth = det.get("auth", "unknown")
        provider_info = None
        auth_hint = None
        if wid == "pi":
            provider_info = _pi_provider_info(settings)
            auth = "ok" if provider_info["configured"] else "needs_login"
            if not provider_info["configured"]:
                auth_hint = PI_NEEDS_PROVIDER_HINT

        priority = order_index.get(wid, len(order))
        usage = worker_usage.get_usage(wid)
        threshold = thresholds.get(wid, DEFAULT_THRESHOLD_PERCENT)
        workers_list.append({
            "id": wid,
            "label": worker_def["label"],
            "installed": det.get("installed", False),
            "version": det.get("version"),
            "path": det.get("path"),
            "auth": auth,
            "auth_hint": auth_hint,
            "provider": provider_info,
            "login_command": worker_def["login_command"],
            "install_command": worker_def["install_command"],
            "install_supported": worker_def["install_command"] is not None,
            "runnable": worker_def["runnable"],
            "enabled": wid in enabled_set,
            "priority": priority,
            "quota": quota,
            "models_supported": bool(
                worker_def.get("models_command") or worker_def.get("models") or worker_def.get("models_source")
            ),
            "model": models_state.get(wid),
            "login_supported": bool(worker_def.get("login_command")),
            "usage": usage,
            "threshold_percent": threshold,
            "over_threshold": usage_over_threshold(usage, threshold, now),
        })

    workers_list.sort(key=lambda w: w["priority"])
    active_worker = pick_worker(settings, workers_list=workers_list, record=False)
    active_id = active_worker["id"] if active_worker else None

    return {
        "enabled": getattr(settings, "workers_enabled", False),
        "install_enabled": getattr(settings, "workers_install_enabled", False),
        "workers": workers_list,
        "active": active_id,
        "switch_log": list(state.get("switch_log", []))[-SWITCH_LOG_LIMIT:],
    }


def get_workers_payload(settings: Any, force_detect: bool = False) -> dict[str, Any]:
    detections = detect_all_workers(force=force_detect)
    return _assemble_workers_payload(settings, detections)


async def async_get_workers_payload(settings: Any, force_detect: bool = False) -> dict[str, Any]:
    detections = await async_detect_all_workers(force=force_detect)
    return _assemble_workers_payload(settings, detections)


UNSANDBOXED_REASON = "需要在服务器上开启 CONVEYOR_WORKERS_UNSANDBOXED 才能让它改代码"
GROK_NO_RUN_REASON = "Super Grok 暂不支持只读提问"
# Workers whose FIX flags (--yolo / --always-approve) allow arbitrary shell.
_UNSANDBOXED_FIX = {"cmd", "grok"}
# Workers with no verified read-only mode for RUN jobs.
_NO_READONLY_RUN = {"grok"}


def mode_eligibility(settings: Any, worker_id: str, mode: Any) -> tuple[bool, str | None]:
    """Whether a worker may run a job of this mode ("run"/"fix" or JobMode)."""
    m = str(getattr(mode, "value", mode) or "").lower()
    if m == "run" and worker_id in _NO_READONLY_RUN:
        return False, GROK_NO_RUN_REASON
    if m == "fix" and worker_id in _UNSANDBOXED_FIX and not getattr(settings, "workers_unsandboxed", False):
        return False, UNSANDBOXED_REASON
    return True, None


def _live_windows(usage: dict[str, Any] | None, now: float) -> list[dict[str, Any]]:
    """Windows that are still running (a window past its reset is unknown/fresh)."""
    result = []
    for window in (usage or {}).get("windows") or []:
        used = window.get("used_percent")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            continue
        reset = window.get("resets_at")
        if isinstance(reset, (int, float)) and reset <= now:
            continue
        result.append(window)
    return result


def usage_over_threshold(usage: dict[str, Any] | None, threshold: int, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return any(w["used_percent"] >= threshold for w in _live_windows(usage, now))


def usage_load(usage: dict[str, Any] | None, now: float | None = None) -> float:
    now = time.time() if now is None else now
    return max((float(w["used_percent"]) for w in _live_windows(usage, now)), default=0.0)


def _is_eligible(worker: dict[str, Any]) -> bool:
    return bool(
        worker["enabled"]
        and worker["installed"]
        and worker["runnable"]
        and worker["auth"] != "needs_login"
        and worker["quota"]["state"] != "cooling"
    )


def _select_worker(workers_list: list[dict[str, Any]], now: float) -> dict[str, Any] | None:
    eligible = [w for w in workers_list if _is_eligible(w)]
    if not eligible:
        return None

    def over(worker: dict[str, Any]) -> bool:
        if "usage" in worker:
            threshold = worker.get("threshold_percent", DEFAULT_THRESHOLD_PERCENT)
            return usage_over_threshold(worker["usage"], threshold, now)
        return bool(worker.get("over_threshold"))

    for worker in eligible:
        if not over(worker):
            return worker
    # Every eligible worker is over its threshold: use the least used one
    # (ties keep the owner's order), the quota-error retry still applies.
    return min(eligible, key=lambda w: usage_load(w.get("usage"), now))


def _switch_reason(prev: dict[str, Any] | None, picked: dict[str, Any], now: float) -> tuple[str, str]:
    if prev is None:
        return "unavailable", "没有可沿用的上一个执行者"
    label = prev.get("label", prev["id"])
    if prev.get("quota", {}).get("state") == "cooling":
        return "quota_error", f"{label} 额度已用完，暂停使用"
    if "usage" in prev and usage_over_threshold(
        prev["usage"], prev.get("threshold_percent", DEFAULT_THRESHOLD_PERCENT), now
    ):
        load = int(round(usage_load(prev["usage"], now)))
        return "threshold", f"{label} 用量 {load}% 已达阈值 {prev.get('threshold_percent', DEFAULT_THRESHOLD_PERCENT)}%"
    return "unavailable", f"{label} 当前不可用"


def record_switch(settings: Any, from_id: str | None, to_id: str, reason: str, detail: str) -> None:
    state = load_workers_state(settings)
    log = state.setdefault("switch_log", [])
    log.append({
        "at": int(time.time()),
        "from": from_id,
        "to": to_id,
        "reason": reason if reason in SWITCH_REASONS else "unavailable",
        "detail": detail,
    })
    state["switch_log"] = log[-SWITCH_LOG_LIMIT:]
    state["last_picked"] = to_id
    save_workers_state(settings, state)


def pick_worker(
    settings: Any,
    workers_list: list[dict[str, Any]] | None = None,
    mode: Any = None,
    record: bool = True,
) -> dict[str, Any] | None:
    if workers_list is None:
        workers_list = get_workers_payload(settings)["workers"]

    if mode is not None:
        # Mode first (read-only RUN, sandbox-gated FIX), then usage thresholds.
        workers_list = [w for w in workers_list if mode_eligibility(settings, w["id"], mode)[0]]
    now = time.time()
    picked = _select_worker(workers_list, now)
    if picked is None or not record:
        return picked

    try:
        previous_id = load_workers_state(settings).get("last_picked")
        if previous_id != picked["id"]:
            prev = next((w for w in workers_list if w["id"] == previous_id), None)
            reason, detail = _switch_reason(prev, picked, now)
            record_switch(settings, previous_id, picked["id"], reason, detail)
    except Exception as exc:
        logger.warning("Failed recording worker switch: %s", exc)
    return picked


async def async_pick_worker(
    settings: Any,
    workers_list: list[dict[str, Any]] | None = None,
    mode: Any = None,
) -> dict[str, Any] | None:
    if workers_list is None:
        # On-demand usage refresh when the cached value is older than 10 minutes.
        try:
            await asyncio.to_thread(worker_usage.refresh_stale)
        except Exception as exc:
            logger.debug("usage refresh before pick failed: %s", exc)
        payload = await async_get_workers_payload(settings)
        workers_list = payload["workers"]
    return await asyncio.to_thread(pick_worker, settings, workers_list, mode)


def set_worker_threshold(settings: Any, worker_id: str, value: Any) -> dict[str, Any]:
    if worker_id not in _CATALOG_BY_ID:
        raise ValueError(f"unknown worker '{worker_id}'")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise BadThresholdError("threshold must be an integer between 1 and 100")
    state = load_workers_state(settings)
    state.setdefault("thresholds", {})[worker_id] = value
    save_workers_state(settings, state)
    return get_workers_payload(settings)


def refresh_usage_now(settings: Any) -> dict[str, Any]:
    worker_usage.refresh_all()
    return get_workers_payload(settings)


def update_order_and_enabled(
    settings: Any,
    order: list[str],
    enabled: list[str],
) -> dict[str, Any]:
    state = load_workers_state(settings)
    state["order"] = list(order)
    state["enabled"] = list(enabled)
    save_workers_state(settings, state)
    return get_workers_payload(settings)


_models_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


def clear_models_cache() -> None:
    global _models_cache
    _models_cache = {}


def parse_agy_models(text: str) -> list[dict[str, str]]:
    models: list[dict[str, str]] = []
    seen = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            parts = line.split("\t", 1)
            mid = parts[0].strip()
            label = parts[1].strip() or mid
        else:
            parts = re.split(r"\s{2,}|\s+", line, maxsplit=1)
            mid = parts[0].strip()
            label = parts[1].strip() if len(parts) > 1 else mid
        if mid and mid not in seen:
            seen.add(mid)
            models.append({"id": mid, "label": label})
    return models


_CMD_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:-]*$")
_CMD_MODEL_STOP_RE = re.compile(r"^(Pass the full id|Docs:|Decision models)", re.IGNORECASE)


def parse_cmd_models(text: str) -> list[dict[str, str]]:
    """Keep only real "<vendor>/<model>   <description>" rows of `cmd --list-models`.

    Help and footer text ("Pass the full id ...", "cmd --model ...", "Docs: ...")
    and the headless "Decision models" section are not selectable models.
    """
    models: list[dict[str, str]] = []
    seen = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _CMD_MODEL_STOP_RE.match(line):
            break
        parts = re.split(r"\s{2,}|\t", line, maxsplit=1)
        mid = parts[0].strip()
        if not _CMD_MODEL_ID_RE.fullmatch(mid):
            continue
        desc = parts[1].strip() if len(parts) > 1 else mid
        if mid not in seen:
            seen.add(mid)
            models.append({"id": mid, "label": desc or mid})
    return models


def _provider_models(settings: Any) -> list[dict[str, str]]:
    """Model list of the SAVED 模型设置 provider only (never a draft)."""
    if settings is None:
        raise ModelsFetchError("未配置模型服务商")
    from conveyor.core import provider_config
    try:
        fetched = provider_config.fetch_provider_models(settings)
    except provider_config.ProviderModelsError as exc:
        raise ModelsFetchError(str(exc))
    return [{"id": m["id"], "label": m["id"]} for m in fetched]


def get_worker_models(
    worker_id: str, force: bool = False, settings: Any = None
) -> tuple[list[dict[str, str]], int]:
    worker_def = get_catalog_worker(worker_id)
    if worker_def is None:
        raise ValueError(f"unknown worker '{worker_id}'")

    if (
        not worker_def.get("models_command")
        and not worker_def.get("models")
        and not worker_def.get("models_source")
    ):
        raise ModelsUnsupportedError("models unsupported")

    now = time.time()
    if worker_def.get("models_source") == "provider":
        return _provider_models(settings), int(now)

    if not force and worker_id in _models_cache:
        cached_at, models = _models_cache[worker_id]
        if now - cached_at < 600.0:
            return models, int(cached_at)

    if worker_def.get("models"):
        models = [{"id": str(m), "label": str(m)} for m in worker_def["models"]]
        _models_cache[worker_id] = (now, models)
        return models, int(now)

    models_cmd = worker_def["models_command"]
    bin_name = models_cmd[0]
    which_path = shutil.which(bin_name)
    if not which_path:
        raise ModelsFetchError("执行者未安装")

    cmd = [which_path] + models_cmd[1:]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            logger.debug("models command for %s exited %s", worker_id, proc.returncode)
            raise ModelsFetchError("获取模型列表失败，请确认执行者已安装并已登录")
        output = proc.stdout or ""
        if worker_id == "agy":
            models = parse_agy_models(output)
        elif worker_id == "cmd":
            models = parse_cmd_models(output)
        else:
            models = parse_agy_models(output)
        _models_cache[worker_id] = (now, models)
        return models, int(now)
    except subprocess.TimeoutExpired:
        raise ModelsFetchError("获取模型列表超时")
    except Exception as exc:
        if isinstance(exc, (ModelsFetchError, ModelsUnsupportedError)):
            raise
        logger.debug("models command for %s failed: %s", worker_id, exc)
        raise ModelsFetchError("获取模型列表失败")


def set_worker_model(settings: Any, worker_id: str, model_id: str | None) -> dict[str, Any]:
    worker_def = get_catalog_worker(worker_id)
    if worker_def is None:
        raise ValueError(f"unknown worker '{worker_id}'")

    if (
        not worker_def.get("models_command")
        and not worker_def.get("models")
        and not worker_def.get("models_source")
    ):
        raise ModelsUnsupportedError("models unsupported")

    if model_id is not None:
        model_id = str(model_id).strip()
        models, _ = get_worker_models(worker_id, settings=settings)
        valid_ids = {m["id"] for m in models}
        if model_id not in valid_ids:
            raise UnknownModelError(f"unknown model '{model_id}'")

    state = load_workers_state(settings)
    models_dict = state.setdefault("models", {})
    if model_id is None:
        models_dict.pop(worker_id, None)
    else:
        models_dict[worker_id] = model_id
    save_workers_state(settings, state)
    return get_workers_payload(settings)


def install_worker(settings: Any, worker_id: str) -> dict[str, Any]:
    worker_def = get_catalog_worker(worker_id)
    if worker_def is None:
        return {"ok": False, "output": f"unknown worker '{worker_id}'"}

    cmd_str = worker_def.get("install_command")
    if not cmd_str:
        return {"ok": False, "output": "install unsupported"}

    cwd: str | None = None
    if worker_def.get("install_argv"):
        # Fixed argv from the catalog, run from the repo root: never user input.
        cmd = list(worker_def["install_argv"])
        cwd = str(_REPO_ROOT)
    elif "|" in cmd_str:
        # Fixed catalog pipelines only (agy, grok): never user input.
        cmd = ["bash", "-c", cmd_str]
    else:
        cmd = shlex.split(cmd_str)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=cwd,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        clear_detection_cache()
        return {
            "ok": proc.returncode == 0,
            "output": output[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "") + "\nCommand timed out after 300s"
        return {
            "ok": False,
            "output": output[-4000:],
        }
    except Exception as exc:
        logger.warning("install of %s failed: %s", worker_id, type(exc).__name__)
        return {
            "ok": False,
            "output": "安装失败，无法运行安装命令",
        }
