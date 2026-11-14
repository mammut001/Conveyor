"""Structured task drafts parsed from read-only supervisor discussion.

The fence is data. Parsing it never executes the text, and a request that
says not to execute is not treated as permission to run.
"""
from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(
    r"```[ \t]*conveyor-task[ \t]*\r?\n(.*?)```",
    re.IGNORECASE | re.DOTALL,
)
_OPEN_FENCE_RE = re.compile(r"```[ \t]*conveyor-task\b", re.IGNORECASE)
_GOAL_LINE = re.compile(
    r"(?im)^[ \t]{0,3}(?:#{1,3}[ \t]*)?(?:任务目标|执行目标|Goal)[ \t]*[:：][ \t]*(.+?)\s*$"
)
_CRITERIA_LINE = re.compile(
    r"(?im)^[ \t]{0,3}(?:#{1,3}[ \t]*)?(?:验收标准|验收条件|Acceptance criteria)[ \t]*[:：][ \t]*(.+?)\s*$"
)
_READONLY_RE = re.compile(
    r"(先不要执行|不要执行|先别执行|不要改代码|先不要改|别改代码|不要修改代码|只讨论|仅讨论|只读|"
    r"do not execute|don't execute|do not change (?:the )?code|don't change (?:the )?code|read-only)",
    re.IGNORECASE,
)
_META_TEXT = 1800
_MAX_DRAFT_TEXT = 8000
_MAX_FENCE_CHARS = 20000


def is_non_authorization(text: str) -> bool:
    """True when the operator asked not to execute or not to change code."""
    return bool(_READONLY_RE.search(text or ""))


def _clip(text: str, limit: int = _META_TEXT) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip()


def _goal_is_only_prohibition(goal: str) -> bool:
    compact = re.sub(r"\s+", "", goal or "")
    if not compact or len(compact) > 40:
        return False
    return is_non_authorization(goal)


def strip_task_draft_fence(text: str) -> str:
    """Drop conveyor-task blocks, including one that is still streaming.

    A closed block is removed and the text after it stays. An opening
    ``conveyor-task`` marker with no closing fence hides that marker and
    everything after it, so partial JSON is not shown. Other Markdown
    fences are left as they are.
    """
    if not isinstance(text, str) or not text:
        return ""
    stripped = _FENCE_RE.sub("", text)
    opener = _OPEN_FENCE_RE.search(stripped)
    if opener is not None:
        stripped = stripped[:opener.start()]
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


def _draft_from_mapping(data: Any) -> dict[str, str] | None:
    if not isinstance(data, dict):
        return None
    goal = data.get("goal")
    if not isinstance(goal, str):
        return None
    criteria = data.get("acceptance_criteria", "")
    if criteria is None:
        criteria = ""
    if not isinstance(criteria, str):
        return None
    goal = goal.strip()
    criteria = criteria.strip()
    if not goal or len(goal) > _MAX_DRAFT_TEXT or len(criteria) > _MAX_DRAFT_TEXT:
        return None
    if _goal_is_only_prohibition(goal):
        return None
    return {"goal": goal, "acceptance_criteria": criteria}


def parse_task_draft(text: str) -> dict[str, str] | None:
    """Return ``{goal, acceptance_criteria}`` or None. Never executes ``text``."""
    if not isinstance(text, str) or not text.strip():
        return None
    matches = list(_FENCE_RE.finditer(text))
    if matches:
        raw = matches[-1].group(1).strip()
        if len(raw) > _MAX_FENCE_CHARS:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        draft = _draft_from_mapping(data)
        if draft is not None:
            return draft
    goal_match = _GOAL_LINE.search(text)
    criteria_match = _CRITERIA_LINE.search(text)
    if goal_match and criteria_match:
        return _draft_from_mapping({
            "goal": goal_match.group(1),
            "acceptance_criteria": criteria_match.group(1),
        })
    return None


def build_supervisor_task_suggestion(
    prompt: str,
    assistant_text: str,
    session_id: str,
) -> dict[str, Any] | None:
    """Build a reviewable draft. Does not authorize or start execution.

    A structured plan is preferred. A read-only request is never copied into
    the goal. An ordinary edit request keeps the operator's words so older
    clients still receive ``prompt``.
    """
    from handlers.chat import host_edit_imperative

    visible = strip_task_draft_fence(assistant_text if isinstance(assistant_text, str) else "")
    draft = parse_task_draft(assistant_text if isinstance(assistant_text, str) else "")
    read_only = is_non_authorization(prompt if isinstance(prompt, str) else "")
    if draft is not None:
        goal = _clip(draft["goal"], _MAX_DRAFT_TEXT)
        criteria = _clip(draft["acceptance_criteria"], _MAX_DRAFT_TEXT)
        return {
            "prompt": goal,
            "goal": goal,
            "acceptance_criteria": criteria,
            "session_id": session_id,
            "source": "structured_draft",
            "execution_authorized": not read_only,
            "context": _clip(visible),
        }
    if read_only:
        return {
            "prompt": "",
            "goal": "",
            "acceptance_criteria": "",
            "session_id": session_id,
            "source": "read_only_fallback",
            "execution_authorized": False,
            "context": _clip(visible),
        }
    if isinstance(prompt, str) and host_edit_imperative(prompt):
        goal = _clip(prompt)
        return {
            "prompt": goal,
            "goal": goal,
            "acceptance_criteria": "",
            "session_id": session_id,
            "source": "operator_request",
            "execution_authorized": True,
        }
    return None


def suggestion_metadata(suggestion: dict[str, Any]) -> dict[str, Any]:
    prompt = suggestion.get("prompt")
    meta: dict[str, Any] = {"prompt": prompt if isinstance(prompt, str) else ""}
    for key in ("goal", "acceptance_criteria", "source", "context"):
        value = suggestion.get(key)
        if isinstance(value, str):
            meta[key] = value
    if isinstance(suggestion.get("execution_authorized"), bool):
        meta["execution_authorized"] = suggestion["execution_authorized"]
    return meta


def task_suggestion_from_metadata(meta: Any, session_id: str) -> dict[str, Any] | None:
    if not isinstance(meta, dict) or not isinstance(meta.get("prompt"), str):
        return None
    item: dict[str, Any] = {"prompt": meta["prompt"], "session_id": session_id}
    for key in ("goal", "acceptance_criteria", "source", "context"):
        if isinstance(meta.get(key), str):
            item[key] = meta[key]
    if isinstance(meta.get("execution_authorized"), bool):
        item["execution_authorized"] = meta["execution_authorized"]
    return item
