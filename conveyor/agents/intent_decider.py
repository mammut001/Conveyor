"""intent_decider.py — provider-pluggable intent classification via decision models.

Supports Gemini Decisions, Jev (TypeSafe AI), and custom decision-model
endpoints using standard-library ``urllib`` with a strict 4-second timeout.
API keys are read strictly from ``conveyor.web.console_secrets.read_secret``
and are never read from logs or echoed in errors/responses.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from conveyor.core import provider_config
from conveyor.web import console_secrets

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 4.0
MAX_RESPONSE_BYTES = 256 * 1024

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


def _read_stored_section(settings: Any) -> dict[str, str]:
    path = _safe_config_path(settings)
    if path is None or not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    section = ""
    stored: dict[str, str] = {}
    for line in text.splitlines():
        sec_match = provider_config._SECTION_RE.match(line)
        if sec_match:
            section = sec_match.group(1).strip()
            continue
        if section != "intent_decider":
            continue
        assign = provider_config._ASSIGNMENT_RE.match(line)
        if not assign:
            continue
        key, raw = assign.groups()
        stored[key] = provider_config._decode_toml_string(raw)
    return stored


def _replace_intent_decider_section(text: str, values: dict[str, str]) -> str:
    output: list[str] = []
    section = ""
    skip_section = False
    for line in text.splitlines():
        sec_match = provider_config._SECTION_RE.match(line)
        if sec_match:
            section = sec_match.group(1).strip()
            skip_section = section == "intent_decider"
            if skip_section:
                continue
        if skip_section:
            continue
        output.append(line)

    remainder = "\n".join(output).strip()
    block_lines = [
        "[intent_decider]",
        f"provider = {provider_config._toml_string(values['provider'])}",
        f"endpoint = {provider_config._toml_string(values['endpoint'])}",
        f"model = {provider_config._toml_string(values['model'])}",
        f"secret_name = {provider_config._toml_string(values['secret_name'])}",
    ]
    block = "\n".join(block_lines)
    if remainder:
        return remainder + "\n\n" + block + "\n"
    return block + "\n"


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
    }


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
        logger.warning("intent_decider HTTP error provider=%s status=%s", provider, code)
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
            return None, "连接服务商超时（超过 4 秒），请检查网络或接口地址", int(HTTPStatus.BAD_GATEWAY)
        return None, "连不上服务商，请检查网络和接口地址", int(HTTPStatus.BAD_GATEWAY)
    except (ValueError, json.JSONDecodeError):
        logger.warning("intent_decider invalid JSON response provider=%s", provider)
        return None, "服务商返回的内容无法识别", int(HTTPStatus.BAD_GATEWAY)

    decision = _parse_decision_response(
        data,
        criteria=criteria,
        provider=provider,
        latency_ms=latency_ms,
    )
    if decision is None:
        logger.warning("intent_decider unexpected response shape provider=%s", provider)
        return None, "服务商返回的结果格式不对，无法识别判断结果", int(HTTPStatus.BAD_GATEWAY)
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
