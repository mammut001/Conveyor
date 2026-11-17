"""intent_decider.py — provider-pluggable intent classification via decision models.

Supports Gemini Decisions, Jev (TypeSafe AI), and custom decision-model
endpoints using standard-library ``urllib`` with a strict 4-second timeout.
API keys are read strictly from ``conveyor.web.console_secrets.read_secret``
and are never read from logs or echoed in errors/responses.

Every call updates one small health record per provider in ``config.toml``
(``[intent_decider_health.<provider>]``), so a provider that stopped working —
a deleted service account, an exhausted quota — is visible in 设置 › 理解指令
and mentioned once in the chat instead of failing silently.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from conveyor.core import provider_config
from conveyor.web import console_secrets

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 4.0
MAX_RESPONSE_BYTES = 256 * 1024
MAX_HEALTH_MESSAGE_CHARS = 80

# 理解指令 has to tell the owner when the provider stopped working: a rejected
# Gemini key used to fall back to normal chat without a word. One small record
# per provider lives beside the decider config (never the key itself), so the
# 设置 section can show it and the chat can mention it once.
HEALTH_SECTION_PREFIX = "intent_decider_health"
HEALTH_KINDS = ("auth", "quota", "rate_limit", "timeout", "network", "bad_request", "other")
HEALTH_FIELDS = (
    "last_ok_at",
    "last_error_at",
    "last_error_kind",
    "last_error_message",
    "consecutive_failures",
)
UNHEALTHY_CONSECUTIVE_FAILURES = 3
# Repeated identical errors only refresh the record once a minute: the decider
# runs on every ambiguous chat message and has to stay cheap.
HEALTH_WRITE_INTERVAL_SECONDS = 60.0

_HEALTH_WRITE_LOCK = threading.Lock()
_HEALTH_LAST_WRITE: dict[str, float] = {}

# Google's error vocabulary -> one short Chinese sentence each. Only this
# derived description is stored; the provider's own text never is.
_REASON_ZH: dict[str, str] = {
    "ACCOUNT_STATE_INVALID": "谷歌说绑定的账号已删除或停用",
    "ACCOUNT_DISABLED": "谷歌说绑定的账号已删除或停用",
    "API_KEY_INVALID": "谷歌说这个密钥无效",
    "API_KEY_NOT_FOUND": "谷歌说找不到这个密钥",
    "API_KEY_SERVICE_BLOCKED": "谷歌说这个项目没有开通这个服务",
    "SERVICE_DISABLED": "谷歌说这个服务没有开通",
    "PERMISSION_DENIED": "谷歌说没有权限",
    "UNAUTHENTICATED": "谷歌说没有通过身份验证",
    "RESOURCE_EXHAUSTED": "谷歌说额度用完了",
    "QUOTA_EXCEEDED": "谷歌说额度用完了",
    "RATE_LIMIT_EXCEEDED": "谷歌说请求太频繁",
    "INVALID_ARGUMENT": "谷歌说请求内容不对",
    "FAILED_PRECONDITION": "谷歌说前置条件不满足",
    "NOT_FOUND": "谷歌说找不到这个接口或模型",
    "UNAVAILABLE": "谷歌说服务暂时不可用",
    "INTERNAL": "谷歌说服务内部出错",
    "DEADLINE_EXCEEDED": "谷歌说等回复超时",
}
_KIND_MESSAGE_ZH: dict[str, str] = {
    "auth": "密钥被拒绝了",
    "quota": "服务商的额度用完了",
    "rate_limit": "请求太频繁，被限流了",
    "timeout": "等回复超时",
    "network": "连不上服务商",
    "bad_request": "服务商认为请求格式不对",
    "other": "服务商返回了错误",
}
_NOTICE_REASON_ZH: dict[str, str] = {
    "auth": "密钥被拒绝",
    "quota": "额度用完",
    "rate_limit": "请求太频繁",
    "timeout": "连接超时",
    "network": "连不上服务商",
    "bad_request": "服务商报错",
    "other": "服务商报错",
}

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/decisions"
GEMINI_MODEL = "gemini-decision-1"
GEMINI_SECRET_NAME = "GEMINI_API_KEY"

JEV_DEFAULT_MODEL = "jev-latest"
JEV_SECRET_NAME = "TYPESAFE_API_KEY"

VALID_PROVIDERS = ("off", "gemini", "jev", "custom")
_SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

DEFAULT_INSTRUCTIONS = "这是用户在一个 AI 编程助手里发的一句话。用户想让助手做哪件事？"
DEFAULT_TEST_SAMPLE = "进度怎么样了"

INTENT_OPTIONS: dict[str, str] = {
    "progress": "查看某个目标或项目现在的进度、结果或状态",
    "stop": "停止或暂停正在运行的目标",
    "resume": "继续之前做到一半的某个项目或目标",
    "create_project": "创建一个新项目或代码仓库",
    "timed_goal": "让助手按一段时间持续迭代改进某个项目",
    "chat": "普通聊天、提问、咨询、闲聊，或说的是过去的事",
}

INTENT_ACTION_LABELS: dict[str, str] = {
    "progress": "查看进度",
    "stop": "停止目标",
    "resume": "继续目标",
    "create_project": "新建项目",
    "timed_goal": "定时改进",
    "chat": "普通对话",
}


@dataclass(frozen=True)
class Decision:
    choice: str
    confidence: float
    probabilities: dict[str, float]
    provider: str
    latency_ms: int


def _safe_read_secret(settings: Any, name: str) -> str | None:
    if not settings or not name or not _SECRET_NAME_RE.fullmatch(name):
        return None
    try:
        val = console_secrets.read_secret(settings, name)
    except Exception:
        return None
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _safe_config_path(settings: Any) -> Path | None:
    if settings is None or not getattr(settings, "codex_memory_root", None):
        return None
    try:
        return provider_config.config_path(settings)
    except Exception:
        return None


def _parse_config_sections(text: str) -> dict[str, dict[str, str]]:
    """Parse every ``[section]`` of the simple TOML file into ``{section: {key: value}}``."""
    sections: dict[str, dict[str, str]] = {}
    section = ""
    for line in text.splitlines():
        sec_match = provider_config._SECTION_RE.match(line)
        if sec_match:
            section = sec_match.group(1).strip()
            continue
        if not section:
            continue
        assign = provider_config._ASSIGNMENT_RE.match(line)
        if not assign:
            continue
        key, raw = assign.groups()
        sections.setdefault(section, {})[key] = provider_config._decode_toml_string(raw)
    return sections


def _replace_config_section(text: str, section: str, values: Mapping[str, str]) -> str:
    """Return ``text`` with ``section`` rewritten from ``values``; other sections stay."""
    output: list[str] = []
    current = ""
    skip_section = False
    for line in text.splitlines():
        sec_match = provider_config._SECTION_RE.match(line)
        if sec_match:
            current = sec_match.group(1).strip()
            skip_section = current == section
            if skip_section:
                continue
        if skip_section:
            continue
        output.append(line)

    remainder = "\n".join(output).strip()
    block_lines = [f"[{section}]"]
    block_lines += [f"{key} = {provider_config._toml_string(str(value))}" for key, value in values.items()]
    block = "\n".join(block_lines)
    if remainder:
        return remainder + "\n\n" + block + "\n"
    return block + "\n"


def _write_config_section(path: Path, section: str, values: Mapping[str, str]) -> bool:
    """Atomically write one section; an unchanged rewrite leaves the file alone."""
    try:
        existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    except OSError:
        return False
    updated = _replace_config_section(existing, section, values)
    if updated == existing:
        return False
    try:
        provider_config._atomic_write(path, updated)
    except OSError:
        logger.warning("intent_decider config write failed section=%s", section)
        return False
    return True


def _read_stored_section(settings: Any) -> dict[str, str]:
    path = _safe_config_path(settings)
    if path is None or not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return dict(_parse_config_sections(text).get("intent_decider", {}))


def _replace_intent_decider_section(text: str, values: dict[str, str]) -> str:
    return _replace_config_section(
        text,
        "intent_decider",
        {
            "provider": values["provider"],
            "endpoint": values["endpoint"],
            "model": values["model"],
            "secret_name": values["secret_name"],
        },
    )


def _validate_endpoint_url(endpoint: str) -> str:
    clean = str(endpoint or "").strip().rstrip("/")
    if not clean:
        return ""
    parsed = urlparse(clean)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError("接口地址格式不正确，请填写完整的 http(s) 地址")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("远程服务接口地址必须使用 https")
    return clean


def _validate_single_line(value: str, label: str, max_len: int) -> str:
    clean = str(value or "").strip()
    if len(clean) > max_len:
        raise ValueError(f"{label}不能超过 {max_len} 个字符")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch in "\x85\u2028\u2029" for ch in clean):
        raise ValueError(f"{label}不能包含换行或控制字符")
    return clean


def resolve_decider_config(
    settings: Any,
    override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve effective provider, endpoint, model, secret status, and readiness."""
    stored = _read_stored_section(settings)
    if override is not None:
        merged = dict(stored)
        for key in ("provider", "endpoint", "model", "secret_name"):
            if key in override and override[key] is not None:
                merged[key] = str(override[key])
    else:
        merged = stored

    configured_provider = str(merged.get("provider") or "").strip().lower()
    if configured_provider == "auto":
        configured_provider = ""

    auto_selected = False
    if configured_provider == "off":
        effective_provider = "off"
    elif configured_provider in ("gemini", "jev", "custom"):
        effective_provider = configured_provider
    else:
        has_gemini = _safe_read_secret(settings, GEMINI_SECRET_NAME) is not None
        has_jev = _safe_read_secret(settings, JEV_SECRET_NAME) is not None
        if has_gemini and not has_jev:
            effective_provider = "gemini"
            auto_selected = True
        elif has_jev and not has_gemini:
            effective_provider = "jev"
            auto_selected = True
        else:
            effective_provider = "off"

    raw_endpoint = str(merged.get("endpoint") or "").strip().rstrip("/")
    raw_model = str(merged.get("model") or "").strip()
    raw_secret = str(merged.get("secret_name") or "").strip().upper()

    if effective_provider == "off":
        return {
            "provider": "off",
            "configured_provider": configured_provider,
            "auto_selected": False,
            "endpoint": "",
            "request_endpoint": "",
            "model": "",
            "secret_name": "",
            "secret_configured": False,
            "ready": False,
            "error": "",
        }

    if effective_provider == "gemini":
        secret_configured = _safe_read_secret(settings, GEMINI_SECRET_NAME) is not None
        return {
            "provider": "gemini",
            "configured_provider": configured_provider,
            "auto_selected": auto_selected,
            "endpoint": "",
            "request_endpoint": GEMINI_ENDPOINT,
            "model": GEMINI_MODEL,
            "secret_name": GEMINI_SECRET_NAME,
            "secret_configured": secret_configured,
            "ready": secret_configured,
            "error": "" if secret_configured else f"还没有在「密钥」页面添加 {GEMINI_SECRET_NAME}",
        }

    if effective_provider == "jev":
        model = raw_model or JEV_DEFAULT_MODEL
        secret_configured = _safe_read_secret(settings, JEV_SECRET_NAME) is not None
        if not raw_endpoint:
            error = "还没填 Jev 的接口地址"
        elif not secret_configured:
            error = f"还没有在「密钥」页面添加 {JEV_SECRET_NAME}"
        else:
            error = ""
        return {
            "provider": "jev",
            "configured_provider": configured_provider,
            "auto_selected": auto_selected,
            "endpoint": raw_endpoint,
            "request_endpoint": raw_endpoint,
            "model": model,
            "secret_name": JEV_SECRET_NAME,
            "secret_configured": secret_configured,
            "ready": bool(raw_endpoint and secret_configured),
            "error": error,
        }

    # custom
    secret_configured = bool(
        raw_secret
        and _SECRET_NAME_RE.fullmatch(raw_secret)
        and _safe_read_secret(settings, raw_secret) is not None
    )
    if not raw_endpoint:
        error = "还没填自定义服务商的接口地址"
    elif not raw_model:
        error = "还没填自定义服务商的模型名称"
    elif not raw_secret:
        error = "还没填自定义服务商的密钥名称"
    elif not _SECRET_NAME_RE.fullmatch(raw_secret):
        error = "密钥名称格式不正确：只能用大写字母、数字和下划线，并以字母开头"
    elif not secret_configured:
        error = f"还没有在「密钥」页面添加 {raw_secret}"
    else:
        error = ""

    return {
        "provider": "custom",
        "configured_provider": configured_provider,
        "auto_selected": False,
        "endpoint": raw_endpoint,
        "request_endpoint": raw_endpoint,
        "model": raw_model,
        "secret_name": raw_secret,
        "secret_configured": secret_configured,
        "ready": bool(raw_endpoint and raw_model and raw_secret and _SECRET_NAME_RE.fullmatch(raw_secret) and secret_configured),
        "error": error,
    }


def _safe_available_secrets(settings: Any) -> list[str]:
    if not settings:
        return []
    try:
        items = console_secrets.list_secrets(settings)
    except Exception:
        return []
    return [str(item["name"]) for item in items if item.get("set") and item.get("name")]


def _public_config_payload(settings: Any, resolved: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": resolved["provider"],
        "configured_provider": resolved["configured_provider"],
        "auto_selected": resolved["auto_selected"],
        "endpoint": resolved["endpoint"],
        "model": resolved["model"],
        "secret_name": resolved["secret_name"],
        "secret_configured": resolved["secret_configured"],
        "available_secrets": _safe_available_secrets(settings),
        "ready": resolved["ready"],
        "error": resolved["error"],
        "health": _health_payload(settings, resolved),
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _health_section(provider: str) -> str:
    return f"{HEALTH_SECTION_PREFIX}.{provider}"


def _config_fingerprint(cfg: Mapping[str, Any]) -> str:
    """Non-secret fingerprint of the provider that is actually called."""
    payload = "|".join([
        str(cfg.get("provider") or ""),
        str(cfg.get("request_endpoint") or ""),
        str(cfg.get("model") or ""),
        str(cfg.get("secret_name") or ""),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _empty_health() -> dict[str, Any]:
    return {
        "last_ok_at": "",
        "last_error_at": "",
        "last_error_kind": "",
        "last_error_message": "",
        "consecutive_failures": 0,
    }


def _short_health_message(kind: str, message: str) -> str:
    clean = " ".join(str(message or "").split())
    if len(clean) > MAX_HEALTH_MESSAGE_CHARS:
        clean = clean[: MAX_HEALTH_MESSAGE_CHARS - 1].rstrip() + "…"
    return clean or _KIND_MESSAGE_ZH.get(kind, _KIND_MESSAGE_ZH["other"])


def _read_health_record(settings: Any, provider: str, fingerprint: str) -> dict[str, Any]:
    """Stored health of one provider, or an empty record when none applies."""
    record = _empty_health()
    path = _safe_config_path(settings)
    if path is None or not path.exists():
        return record
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return record
    stored = _parse_config_sections(text).get(_health_section(provider), {})
    for field in HEALTH_FIELDS:
        if field in stored:
            record[field] = stored[field]
    try:
        record["consecutive_failures"] = max(0, int(float(record["consecutive_failures"])))
    except (TypeError, ValueError):
        record["consecutive_failures"] = 0
    kind = str(record["last_error_kind"] or "")
    record["last_error_kind"] = kind if kind in HEALTH_KINDS else ""
    # Another endpoint / model / key: the old record does not describe this call.
    if str(stored.get("fingerprint") or "") != fingerprint:
        return _empty_health()
    return record


def _record_health(
    settings: Any,
    cfg: Mapping[str, Any],
    *,
    ok: bool,
    kind: str = "",
    message: str = "",
) -> None:
    """Persist one small health record per provider (never the key itself)."""
    provider = str(cfg.get("provider") or "")
    if provider not in VALID_PROVIDERS or provider == "off":
        return
    fingerprint = _config_fingerprint(cfg)
    try:
        previous = _read_health_record(settings, provider, fingerprint)
    except Exception:
        logger.debug("intent_decider health read failed", exc_info=True)
        return

    now_iso = _utc_now_iso()
    if ok:
        kind = ""
        message = ""
        record = {
            "last_ok_at": now_iso,
            "last_error_at": str(previous.get("last_error_at") or ""),
            "last_error_kind": "",
            "last_error_message": "",
            "consecutive_failures": 0,
        }
    else:
        kind = kind if kind in HEALTH_KINDS else "other"
        record = {
            "last_ok_at": str(previous.get("last_ok_at") or ""),
            "last_error_at": now_iso,
            "last_error_kind": kind,
            "last_error_message": _short_health_message(kind, message),
            "consecutive_failures": int(previous.get("consecutive_failures") or 0) + 1,
        }

    previous_failures = int(previous.get("consecutive_failures") or 0)
    same_outcome = (
        record["last_error_kind"] == str(previous.get("last_error_kind") or "")
        and record["last_error_message"] == str(previous.get("last_error_message") or "")
    )
    failures = int(record["consecutive_failures"])
    if ok:
        # A healthy call matters when it clears a failure; repeats only refresh 上次成功.
        state_changed = not same_outcome
    else:
        # A different failure, or a count still climbing to the unhealthy line, is a
        # real state change; identical errors past that wait for the next minute.
        state_changed = not same_outcome or (
            failures != previous_failures and failures <= UNHEALTHY_CONSECUTIVE_FAILURES
        )
    key = f"{provider}:{fingerprint}"
    now = time.time()
    with _HEALTH_WRITE_LOCK:
        due = (now - _HEALTH_LAST_WRITE.get(key, 0.0)) >= HEALTH_WRITE_INTERVAL_SECONDS
    if not state_changed and not due:
        return

    path = _safe_config_path(settings)
    if path is None:
        return
    values: dict[str, str] = {
        "last_ok_at": str(record["last_ok_at"]),
        "last_error_at": str(record["last_error_at"]),
        "last_error_kind": str(record["last_error_kind"]),
        "last_error_message": str(record["last_error_message"]),
        "consecutive_failures": str(int(record["consecutive_failures"])),
        "fingerprint": fingerprint,
    }
    with _HEALTH_WRITE_LOCK:
        if not _write_config_section(path, _health_section(provider), values):
            return
        _HEALTH_LAST_WRITE[key] = time.time()


def _health_payload(settings: Any, resolved: Mapping[str, Any]) -> dict[str, Any]:
    provider = str(resolved.get("provider") or "off")
    if provider == "off":
        return _public_health("off", _empty_health())
    try:
        record = _read_health_record(settings, provider, _config_fingerprint(resolved))
    except Exception:
        logger.debug("intent_decider health read failed", exc_info=True)
        record = _empty_health()
    return _public_health(provider, record)


def _public_health(provider: str, record: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(record.get("last_error_kind") or "") or None
    failures = int(record.get("consecutive_failures") or 0)
    return {
        "provider": provider,
        "consecutive_failures": failures,
        "last_error_kind": kind,
        "last_error_at": str(record.get("last_error_at") or "") or None,
        "last_ok_at": str(record.get("last_ok_at") or "") or None,
        "last_error_message": str(record.get("last_error_message") or ""),
        "unhealthy": bool(kind == "auth" or failures >= UNHEALTHY_CONSECUTIVE_FAILURES),
    }


def get_decider_health(settings: Any) -> dict[str, Any]:
    """Public health record of the provider 理解指令 would call right now."""
    return _health_payload(settings, resolve_decider_config(settings))


def decider_fallback_notice(health: Mapping[str, Any] | None) -> str:
    """One short Chinese sentence for the chat when the decider fell back."""
    if not health or not health.get("unhealthy"):
        return ""
    reason = _NOTICE_REASON_ZH.get(str(health.get("last_error_kind") or ""), "服务商报错")
    return f"理解指令暂时用不了（{reason}），这次按普通聊天回答。可以到 设置 › 理解指令 看看。"


def get_intent_decider_config(settings: Any) -> dict[str, Any]:
    """Return the safe public config payload (never includes secrets)."""
    return _public_config_payload(settings, resolve_decider_config(settings))


def save_intent_decider_config(settings: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and atomically persist ``[intent_decider]`` in ``config.toml``."""
    if not isinstance(payload, Mapping):
        raise ValueError("请求格式不正确")

    provider = str(payload.get("provider") or "off").strip().lower()
    if provider not in VALID_PROVIDERS:
        raise ValueError("不支持的服务商选项")

    endpoint = _validate_endpoint_url(str(payload.get("endpoint") or ""))
    model = _validate_single_line(str(payload.get("model") or ""), "模型名称", 120)
    secret_name = _validate_single_line(str(payload.get("secret_name") or ""), "密钥名称", 64).upper()
    if secret_name and not _SECRET_NAME_RE.fullmatch(secret_name):
        raise ValueError("密钥名称格式不正确：只能用大写字母、数字和下划线，并以字母开头")

    if provider == "off":
        endpoint = ""
        model = ""
        secret_name = ""
    elif provider == "gemini":
        endpoint = ""
        model = GEMINI_MODEL
        secret_name = GEMINI_SECRET_NAME
    elif provider == "jev":
        model = model or JEV_DEFAULT_MODEL
        secret_name = JEV_SECRET_NAME

    path = _safe_config_path(settings)
    if path is None:
        raise ValueError("配置目录未就绪，无法保存")

    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    updated = _replace_intent_decider_section(
        existing,
        {
            "provider": provider,
            "endpoint": endpoint,
            "model": model,
            "secret_name": secret_name,
        },
    )
    provider_config._atomic_write(path, updated)
    return get_intent_decider_config(settings)


def is_configured(settings: Any) -> bool:
    """Return True when an intent decider provider is active and ready to call."""
    return bool(resolve_decider_config(settings)["ready"])


def _build_criteria(options: Mapping[str, str] | Any) -> dict[str, str]:
    if isinstance(options, Mapping):
        raw_map = dict(options)
    else:
        raw_map = dict(options)
    criteria: dict[str, str] = {}
    for k, v in raw_map.items():
        key = str(k or "").strip()
        val = str(v or "").strip()
        if key and val:
            criteria[key] = val
    return criteria


def _parse_decision_response(
    data: Any,
    *,
    criteria: dict[str, str],
    provider: str,
    latency_ms: int,
) -> Decision | None:
    if not isinstance(data, dict):
        return None
    answers = data.get("answers")
    if not isinstance(answers, dict):
        return None
    intent_ans = answers.get("intent")
    if not isinstance(intent_ans, dict):
        return None

    choice = intent_ans.get("choice")
    if not isinstance(choice, str) or choice not in criteria:
        return None

    raw_probs = intent_ans.get("probabilities")
    probabilities: dict[str, float] = {}
    if isinstance(raw_probs, dict):
        for k, v in raw_probs.items():
            if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool):
                probabilities[k] = max(0.0, min(1.0, float(v)))

    raw_conf = intent_ans.get("confidence")
    if isinstance(raw_conf, (int, float)) and not isinstance(raw_conf, bool):
        confidence = max(0.0, min(1.0, float(raw_conf)))
    elif choice in probabilities:
        confidence = probabilities[choice]
    else:
        return None

    if not probabilities:
        probabilities = {choice: confidence}

    return Decision(
        choice=choice,
        confidence=confidence,
        probabilities=probabilities,
        provider=provider,
        latency_ms=max(0, int(latency_ms)),
    )


def _classify_http_error(exc: urllib.error.HTTPError) -> tuple[str, str]:
    """Return ``(kind, chinese_message)`` for one provider HTTP error.

    Google answers with ``{"error": {"status", "message", "details":[{"reason"}]}}``;
    the reason is what makes the failure explainable to the owner. Only the
    derived Chinese sentence is kept — never the body, never the key.
    """
    code = int(getattr(exc, "code", 0) or 0)
    payload: Any = None
    try:
        raw = exc.read(MAX_RESPONSE_BYTES)
        payload = json.loads(raw.decode("utf-8", errors="replace")) if raw else None
    except Exception:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        error = {}
    reason = str(error.get("reason") or "")
    details = error.get("details")
    if isinstance(details, list):
        for item in details:
            if isinstance(item, dict) and item.get("reason") and not reason:
                reason = str(item["reason"])
    status = str(error.get("status") or "")
    message = str(error.get("message") or "")
    text = f"{reason} {status} {message}".upper()

    if code in (401, 403):
        kind = "auth"
    elif "RESOURCE_EXHAUSTED" in text or "QUOTA" in text:
        # Google reports an exhausted quota as 429 + RESOURCE_EXHAUSTED; the body wins.
        kind = "quota"
    elif code == 429 or "RATE_LIMIT" in text or "TOO MANY REQUESTS" in text:
        kind = "rate_limit"
    elif code in (408, 504) or "DEADLINE_EXCEEDED" in text:
        kind = "timeout"
    elif code == 400 or "INVALID_ARGUMENT" in text or "FAILED_PRECONDITION" in text:
        kind = "bad_request"
    else:
        kind = "other"

    detail = _REASON_ZH.get(reason.upper()) or _REASON_ZH.get(status.upper()) or ""
    head = _KIND_MESSAGE_ZH.get(kind, _KIND_MESSAGE_ZH["other"])
    suffix = f"（HTTP {code}）" if code else ""
    chinese = f"{head}（{detail}）{suffix}" if detail else f"{head}{suffix}"
    return kind, chinese


def _decide_with_error(
    settings: Any,
    text: str,
    options: Mapping[str, str],
    *,
    override: Mapping[str, Any] | None = None,
) -> tuple[Decision | None, str, int]:
    """Run one decision call and return ``(Decision | None, chinese_error, http_status)``."""
    clean_text = str(text or "").strip()
    if not clean_text:
        return None, "请输入要测试的句子", int(HTTPStatus.BAD_REQUEST)

    criteria = _build_criteria(options)
    if not criteria:
        return None, "没有可用的意图选项", int(HTTPStatus.BAD_REQUEST)

    cfg = resolve_decider_config(settings, override=override)
    provider = cfg["provider"]
    if provider == "off":
        return None, "还没有开启理解指令，请先选择一个服务商", int(HTTPStatus.BAD_REQUEST)
    if not cfg["ready"]:
        return None, cfg["error"] or "理解指令还没有配置完整", int(HTTPStatus.BAD_REQUEST)

    endpoint = cfg["request_endpoint"]
    try:
        endpoint = _validate_endpoint_url(endpoint)
    except ValueError as exc:
        return None, str(exc), int(HTTPStatus.BAD_REQUEST)

    secret = _safe_read_secret(settings, cfg["secret_name"])
    if not secret:
        return None, f"还没有在「密钥」页面添加 {cfg['secret_name']}", int(HTTPStatus.BAD_REQUEST)

    body_dict = {
        "model": cfg["model"],
        "input": clean_text,
        "questions": {
            "intent": {
                "type": "choice",
                "instructions": DEFAULT_INSTRUCTIONS,
                "criteria": criteria,
            }
        },
    }
    encoded_body = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Conveyor/1.0",
    }
    if provider == "gemini":
        headers["x-goog-api-key"] = secret
    else:
        headers["Authorization"] = f"Bearer {secret}"

    req = urllib.request.Request(endpoint, data=encoded_body, headers=headers, method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES)
        latency_ms = int(round((time.monotonic() - started) * 1000))
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        kind, health_message = _classify_http_error(exc)
        logger.warning("intent_decider HTTP error provider=%s status=%s kind=%s", provider, code, kind)
        _record_health(settings, cfg, ok=False, kind=kind, message=health_message)
        if code in (401, 403):
            return None, f"服务商拒绝了这个密钥（HTTP {code}），请检查「密钥」里的配置", int(HTTPStatus.BAD_GATEWAY)
        if code == 400:
            return None, "服务商认为请求格式不对（HTTP 400），请检查接口地址和模型名称", int(HTTPStatus.BAD_GATEWAY)
        return None, f"服务商返回了错误（HTTP {code}），请稍后再试", int(HTTPStatus.BAD_GATEWAY)
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", None)
        is_timeout = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError) or "timed out" in str(reason or exc).lower()
        logger.warning("intent_decider transport failure provider=%s timeout=%s", provider, is_timeout)
        if is_timeout:
            _record_health(settings, cfg, ok=False, kind="timeout", message="连接服务商超时（超过 4 秒）")
            return None, "连接服务商超时（超过 4 秒），请检查网络或接口地址", int(HTTPStatus.BAD_GATEWAY)
        _record_health(settings, cfg, ok=False, kind="network", message="连不上服务商，请检查网络和接口地址")
        return None, "连不上服务商，请检查网络和接口地址", int(HTTPStatus.BAD_GATEWAY)
    except (ValueError, json.JSONDecodeError):
        logger.warning("intent_decider invalid JSON response provider=%s", provider)
        _record_health(settings, cfg, ok=False, kind="other", message="服务商返回的内容无法识别")
        return None, "服务商返回的内容无法识别", int(HTTPStatus.BAD_GATEWAY)

    decision = _parse_decision_response(
        data,
        criteria=criteria,
        provider=provider,
        latency_ms=latency_ms,
    )
    if decision is None:
        logger.warning("intent_decider unexpected response shape provider=%s", provider)
        _record_health(settings, cfg, ok=False, kind="other", message="服务商返回的结果格式不对，无法识别判断结果")
        return None, "服务商返回的结果格式不对，无法识别判断结果", int(HTTPStatus.BAD_GATEWAY)
    _record_health(settings, cfg, ok=True)
    return decision, "", int(HTTPStatus.OK)


def decide_intent(
    settings: Any,
    text: str,
    options: Mapping[str, str],
) -> Decision | None:
    """Ask the configured decision model which option best matches ``text``.

    Returns ``None`` when disabled, unconfigured, or on any error/timeout so the
    caller safely falls back to normal chat.
    """
    try:
        decision, _err, _status = _decide_with_error(settings, text, options)
        return decision
    except Exception:
        logger.warning("intent_decider unexpected failure")
        try:
            _record_health(
                settings,
                resolve_decider_config(settings),
                ok=False,
                kind="other",
                message="理解指令内部出错",
            )
        except Exception:
            logger.debug("intent_decider health write failed", exc_info=True)
        return None


def test_intent_decider(
    settings: Any,
    body: Mapping[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run one real decision call on a sample sentence for the Settings UI."""
    sample = DEFAULT_TEST_SAMPLE
    override: Mapping[str, Any] | None = None
    if isinstance(body, Mapping):
        if isinstance(body.get("text"), str) and body["text"].strip():
            sample = body["text"].strip()
        if any(k in body for k in ("provider", "endpoint", "model", "secret_name")):
            override = body

    decision, error, status = _decide_with_error(
        settings,
        sample,
        INTENT_OPTIONS,
        override=override,
    )
    if decision is None:
        return status, {"ok": False, "error": error or "测试失败，请稍后再试"}

    return int(HTTPStatus.OK), {
        "ok": True,
        "choice": decision.choice,
        "choice_label": INTENT_ACTION_LABELS.get(decision.choice, decision.choice),
        "confidence": decision.confidence,
        "probabilities": decision.probabilities,
        "provider": decision.provider,
        "latency_ms": decision.latency_ms,
        "sample_text": sample,
    }
