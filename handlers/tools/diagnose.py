"""handlers/tools/diagnose.py — /diagnose mode definitions and hybrid prompts."""
from __future__ import annotations

# (tool_name, arg) pairs per diagnose mode.
DIAGNOSE_MODES: dict[str, tuple[tuple[str, str], ...]] = {
    "server": (
        ("load", ""),
        ("ps", ""),
        ("disk", ""),
        ("service_status", ""),
        ("logs", "40"),
    ),
    "bot": (
        ("service_status", ""),
        ("logs", ""),
        ("git_status", ""),
        ("disk", ""),
    ),
    "logs": (
        ("logs", ""),
        ("service_status", ""),
        ("load", ""),
    ),
    "quick": (
        ("load", ""),
        ("service_status", ""),
    ),
}

VALID_DIAGNOSE_MODES = frozenset(DIAGNOSE_MODES)


def normalize_diagnose_mode(arg: str) -> str:
    """Return a valid mode name; default server."""
    mode = (arg or "").strip().lower().split()[0] if (arg or "").strip() else "server"
    if mode in VALID_DIAGNOSE_MODES:
        return mode
    return ""


def diagnose_tool_items(mode: str) -> tuple[tuple[str, str], ...]:
    return DIAGNOSE_MODES.get(mode, DIAGNOSE_MODES["server"])


def build_hybrid_prompt(question: str, facts: str) -> str:
    return (
        f"用户问题：{question}\n\n"
        "以下是 bot 主机上的确定性采集数据（本地快照，非 Codex sandbox 猜测）：\n\n"
        f"{facts}\n\n"
        "请基于以上真实数据进行分析。要求：\n"
        "- 这些是确定性本地主机快照，不要编造未提供的主机状态\n"
        "- 给出可能原因、严重程度和下一步安全操作建议\n"
        "- 默认用中文回答\n"
        "- 若数据不足以判断，说明还需要什么信息"
    )
