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

logger = logging.getLogger("conveyor.workers")

class ModelsUnsupportedError(Exception):
    pass


class ModelsFetchError(Exception):
    pass


class UnknownModelError(Exception):
    pass


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
        "label": "Pi",
        "bin": "pi",
        "install_command": None,
        "login_command": None,
        "auth_type": "none",
        "auth_command": None,
        "runnable": True,
        "backend": "pi",
        "models_command": None,
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
        "runnable": False,
        "backend": None,
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
        "runnable": False,
        "backend": None,
        "models_command": ["cmd", "--list-models"],
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


def detect_worker(worker_def: dict[str, Any]) -> dict[str, Any]:
    bin_name = worker_def["bin"]
    which_path = shutil.which(bin_name)
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


def _assemble_workers_payload(
    settings: Any,
    detections: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    state = load_workers_state(settings)
    order = state.get("order", [item["id"] for item in CATALOG])
    enabled_set = set(state.get("enabled", [item["id"] for item in CATALOG]))
    cooling = state.get("cooling", {})
    models_state = state.get("models", {})

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

        priority = order_index.get(wid, len(order))
        workers_list.append({
            "id": wid,
            "label": worker_def["label"],
            "installed": det.get("installed", False),
            "version": det.get("version"),
            "path": det.get("path"),
            "auth": det.get("auth", "unknown"),
            "login_command": worker_def["login_command"],
            "install_command": worker_def["install_command"],
            "install_supported": worker_def["install_command"] is not None,
            "runnable": worker_def["runnable"],
            "enabled": wid in enabled_set,
            "priority": priority,
            "quota": quota,
            "models_supported": bool(worker_def.get("models_command") or worker_def.get("models")),
            "model": models_state.get(wid),
            "login_supported": bool(worker_def.get("login_command")),
        })

    workers_list.sort(key=lambda w: w["priority"])
    active_worker = pick_worker(settings, workers_list=workers_list)
    active_id = active_worker["id"] if active_worker else None

    return {
        "enabled": getattr(settings, "workers_enabled", False),
        "install_enabled": getattr(settings, "workers_install_enabled", False),
        "workers": workers_list,
        "active": active_id,
    }


def get_workers_payload(settings: Any, force_detect: bool = False) -> dict[str, Any]:
    detections = detect_all_workers(force=force_detect)
    return _assemble_workers_payload(settings, detections)


async def async_get_workers_payload(settings: Any, force_detect: bool = False) -> dict[str, Any]:
    detections = await async_detect_all_workers(force=force_detect)
    return _assemble_workers_payload(settings, detections)


def pick_worker(
    settings: Any,
    workers_list: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if workers_list is None:
        workers_list = get_workers_payload(settings)["workers"]

    for worker in workers_list:
        if (
            worker["enabled"]
            and worker["installed"]
            and worker["runnable"]
            and worker["auth"] != "needs_login"
            and worker["quota"]["state"] != "cooling"
        ):
            return worker

    return None


async def async_pick_worker(
    settings: Any,
    workers_list: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if workers_list is None:
        payload = await async_get_workers_payload(settings)
        workers_list = payload["workers"]
    return pick_worker(settings, workers_list=workers_list)


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


def parse_cmd_models(text: str) -> list[dict[str, str]]:
    models: list[dict[str, str]] = []
    seen = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"\s{2,}|\t", line, maxsplit=1)
        if not parts:
            continue
        mid = parts[0].strip()
        if "/" not in mid:
            continue
        desc = parts[1].strip() if len(parts) > 1 else mid
        if mid and mid not in seen:
            seen.add(mid)
            models.append({"id": mid, "label": desc or mid})
    return models


def get_worker_models(worker_id: str, force: bool = False) -> tuple[list[dict[str, str]], int]:
    worker_def = get_catalog_worker(worker_id)
    if worker_def is None:
        raise ValueError(f"unknown worker '{worker_id}'")

    if not worker_def.get("models_command") and not worker_def.get("models"):
        raise ModelsUnsupportedError("models unsupported")

    now = time.time()
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
        raise ModelsFetchError(f"执行者未安装: {bin_name}")

    cmd = [which_path] + models_cmd[1:]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            msg = f"获取模型列表失败 (退出码 {proc.returncode}): {err}" if err else f"获取模型列表失败 (退出码 {proc.returncode})"
            raise ModelsFetchError(msg)
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
        raise ModelsFetchError(f"获取模型列表失败: {exc}")


def set_worker_model(settings: Any, worker_id: str, model_id: str | None) -> dict[str, Any]:
    worker_def = get_catalog_worker(worker_id)
    if worker_def is None:
        raise ValueError(f"unknown worker '{worker_id}'")

    if not worker_def.get("models_command") and not worker_def.get("models"):
        raise ModelsUnsupportedError("models unsupported")

    if model_id is not None:
        model_id = str(model_id).strip()
        models, _ = get_worker_models(worker_id)
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

    if worker_id == "agy":
        cmd = ["bash", "-c", cmd_str]
    else:
        cmd = shlex.split(cmd_str)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
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
        return {
            "ok": False,
            "output": str(exc)[-4000:],
        }
