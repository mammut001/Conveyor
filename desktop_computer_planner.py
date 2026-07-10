"""desktop_computer_planner.py — decides the next desktop action (P5.6).

The planner is the "brain" of the Codex action loop. It receives the
goal, the latest observation, and the redacted trajectory, and returns
the NEXT single action as a JSON object:

    {"action": "observe"}
    {"action": "click", "x": 123, "y": 456}
    {"action": "type", "text": "..."}
    {"action": "hotkey", "keys": ["cmd", "l"]}
    {"action": "scroll", "dx": 0, "dy": -500}
    {"action": "wait", "seconds": 1}
    {"action": "done", "summary": "..."}
    {"action": "stop", "reason": "..."}

Two implementations:
- ``CodexPlanner``: the real path. Drives ``codex exec --json`` with a
  strict one-action instruction and parses the model's JSON reply.
- ``ScriptedPlanner``: deterministic, network-free. Used by the smoke
  suite and as a fallback when Codex is unavailable.

Simple single-digit Calculator goals (e.g. “点击数字 1”) are handled by
``maybe_simple_digit_action`` *before* Codex, so the product path does
not thrash multiple AX buttons (which produced displays like ``113``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from config import Settings

logger = logging.getLogger(__name__)


_ALLOWED = ("observe", "click", "type", "hotkey", "scroll", "wait", "done", "stop")

# Preferred Clear button labels on macOS Calculator (short, safe).
_CLEAR_LABELS = ("all clear", "clear", "ac", "c")


def extract_single_digit_click_goal(goal: str) -> str | None:
    """If goal asks to click exactly one digit 0-9, return that digit, else None."""
    g = (goal or "").strip()
    if not g:
        return None
    found: list[str] = []
    patterns = (
        r"点击数字\s*([0-9])",
        r"点(?:击)?\s*数字\s*([0-9])",
        r"(?:点|按)\s*([0-9])(?:\s|$|，|,|。|然后|并|完成)",
        r"click\s+(?:the\s+)?(?:digit\s+|number\s+)?([0-9])\b",
        r"press\s+(?:the\s+)?(?:digit\s+|number\s+)?([0-9])\b",
    )
    for pat in patterns:
        found.extend(re.findall(pat, g, flags=re.IGNORECASE))
    # De-dupe preserving order.
    unique = list(dict.fromkeys(found))
    if len(unique) != 1:
        return None
    # Reject multi-digit arithmetic goals ("1+2", "11") when extra digits appear.
    other_digits = re.findall(r"[0-9]", g)
    if len(other_digits) > 1 and not re.search(
        r"(?:点击数字|数字|digit|number)\s*" + re.escape(unique[0]),
        g,
        flags=re.IGNORECASE,
    ):
        # Goal text may include step numbers; only allow if the sole
        # "click target" pattern matched once and other digits are not
        # also click targets. Conservative: if >1 digit chars and more
        # than one unique digit, refuse.
        if len(set(other_digits)) > 1:
            return None
    return unique[0]


def _hint_label(h: dict) -> str:
    return str(h.get("label") or "").strip()


def _find_hint(
    observation: dict,
    *,
    label: str | None = None,
    labels: tuple[str, ...] | None = None,
) -> dict | None:
    hints = observation.get("element_hints") if isinstance(observation, dict) else None
    if not isinstance(hints, list):
        return None
    wanted: list[str] = []
    if label is not None:
        wanted.append(label)
    if labels:
        wanted.extend(labels)
    wanted_l = [w.lower() for w in wanted if w]
    for h in hints:
        if not isinstance(h, dict):
            continue
        lab = _hint_label(h)
        if not lab:
            continue
        if lab.lower() in wanted_l or lab in wanted:
            return h
    return None


def _ax_click_from_hint(observation: dict, hint: dict) -> dict | None:
    try:
        pid = int(observation.get("pid"))
        window_id = int(observation.get("window_id"))
        element_index = int(hint.get("element_index"))
    except (TypeError, ValueError):
        return None
    action: dict[str, Any] = {
        "action": "click",
        "pid": pid,
        "window_id": window_id,
        "element_index": element_index,
    }
    lab = _hint_label(hint)
    if lab:
        action["_target_label"] = lab
    token = hint.get("element_token")
    if isinstance(token, str) and token.strip():
        action["element_token"] = token.strip()
    return action


def _trajectory_labels(trajectory: list[dict]) -> list[str]:
    labels: list[str] = []
    for entry in trajectory or []:
        if not isinstance(entry, dict):
            continue
        if not entry.get("result_ok", True):
            continue
        if entry.get("action_type") != "click":
            continue
        lab = entry.get("clicked_label")
        if isinstance(lab, str) and lab.strip():
            labels.append(lab.strip())
            continue
        red = entry.get("action_redacted") or {}
        if isinstance(red, dict):
            tl = red.get("_target_label") or red.get("label")
            if isinstance(tl, str) and tl.strip():
                labels.append(tl.strip())
    return labels


def maybe_simple_digit_action(
    *,
    goal: str,
    observation: dict,
    trajectory: list[dict],
) -> dict | None:
    """Deterministic plan for single-digit click goals (no Codex thrash).

    Sequence: observe (if needed) → Clear/All Clear as needed → click digit
    once → done. Calculator may expose ``Clear`` first and only reveal
    ``All Clear`` after the first click while an expression is active.
    Returns None when the goal is not a simple single-digit click.
    """
    digit = extract_single_digit_click_goal(goal)
    if digit is None:
        return None

    labels_done = [lab.lower() for lab in _trajectory_labels(trajectory)]
    digit_clicked = digit in labels_done or any(
        lab == digit for lab in _trajectory_labels(trajectory)
    )
    if digit_clicked:
        return {
            "action": "done",
            "summary": f"clicked digit {digit}",
        }

    obs = observation if isinstance(observation, dict) else {}
    hints = obs.get("element_hints")
    pid = obs.get("pid")
    window_id = obs.get("window_id")
    if not isinstance(hints, list) or not hints or pid is None or window_id is None:
        return {"action": "observe"}

    # A Calculator expression can expose "Clear" first; that clears only the
    # active operand and then exposes "All Clear". Treat only a successful
    # All Clear/AC click as fully cleared, using the fresh post-action hints.
    fully_cleared = any(lab in ("all clear", "ac") for lab in labels_done)
    if not fully_cleared:
        clear_hint = _find_hint(obs, labels=("All Clear", "Clear", "AC"))
        if clear_hint is not None:
            click = _ax_click_from_hint(obs, clear_hint)
            if click is not None:
                return click

    digit_hint = _find_hint(obs, label=digit)
    if digit_hint is None:
        return {
            "action": "stop",
            "reason": f"digit_{digit}_not_in_element_hints",
        }
    click = _ax_click_from_hint(obs, digit_hint)
    if click is None:
        return {"action": "stop", "reason": f"digit_{digit}_ax_incomplete"}
    return click


def resolve_clicked_label(action: dict, observation: dict) -> str | None:
    """Best-effort label for a click (from action or matching element_hints)."""
    if not isinstance(action, dict) or action.get("action") != "click":
        return None
    for key in ("_target_label", "label"):
        val = action.get(key)
        if isinstance(val, str) and val.strip() and len(val.strip()) <= 32:
            return val.strip()
    try:
        idx = int(action.get("element_index"))
    except (TypeError, ValueError):
        return None
    hints = observation.get("element_hints") if isinstance(observation, dict) else None
    if not isinstance(hints, list):
        return None
    for h in hints:
        if not isinstance(h, dict):
            continue
        try:
            if int(h.get("element_index")) == idx:
                lab = _hint_label(h)
                return lab or None
        except (TypeError, ValueError):
            continue
    return None


class Planner(ABC):
    @abstractmethod
    async def next_action(
        self,
        *,
        goal: str,
        observation: dict,
        trajectory: list[dict],
        steps_used: int,
        max_steps: int,
    ) -> dict:
        """Return the next action dict (or done/stop)."""


def _obs_summary(observation: dict) -> str:
    if not isinstance(observation, dict):
        return "no observation"
    parts: list[str] = []
    sid = observation.get("screenshot_id") or observation.get("sha256")
    if sid:
        parts.append(
            f"screenshot {sid} ({observation.get('width')}x{observation.get('height')})"
        )
    active = observation.get("active_app")
    if isinstance(active, str) and active.strip():
        parts.append(f"active_app={active.strip()[:64]}")
    ax_app = observation.get("ax_app")
    if isinstance(ax_app, str) and ax_app.strip():
        parts.append(f"ax_app={ax_app.strip()[:64]}")
    # Surface AX / element / action hints so the planner can prefer them.
    for key in (
        "pid", "window_id", "element_index", "element_token",
        "elements", "element_hints", "action_hints", "ax_hints",
        "click_method",
    ):
        val = observation.get(key)
        if val is None or val == "" or val == []:
            continue
        if isinstance(val, (list, dict)):
            try:
                snippet = json.dumps(val, ensure_ascii=False)
            except Exception:
                snippet = str(val)
            # element_hints can be longer — keep more for digit matching.
            limit = 900 if key == "element_hints" else 240
            if len(snippet) > limit:
                snippet = snippet[: limit - 1] + "…"
            parts.append(f"{key}={snippet}")
        else:
            parts.append(f"{key}={val}")
    return "; ".join(parts) if parts else "no screenshot yet"


def _trajectory_summary(trajectory: list[dict]) -> str:
    if not trajectory:
        return "(none)"
    lines = []
    for entry in trajectory[-8:]:
        if not isinstance(entry, dict):
            continue
        act = entry.get("action_type") or entry.get("action") or "?"
        ok = "ok" if entry.get("result_ok", True) else "fail"
        extra = ""
        lab = entry.get("clicked_label")
        if isinstance(lab, str) and lab.strip():
            extra = f" label={lab.strip()[:16]}"
        else:
            red = entry.get("action_redacted") or {}
            if isinstance(red, dict) and red.get("element_index") is not None:
                extra = f" element_index={red.get('element_index')}"
        lines.append(f"- {act} ({ok}){extra}")
    return "\n".join(lines) if lines else "(none)"


class ScriptedPlanner(Planner):
    """Replay a fixed action list, then emit done. For smokes/tests."""

    def __init__(self, actions: list[dict]) -> None:
        self._actions = list(actions)

    async def next_action(
        self,
        *,
        goal: str,
        observation: dict,
        trajectory: list[dict],
        steps_used: int,
        max_steps: int,
    ) -> dict:
        if steps_used < len(self._actions):
            return dict(self._actions[steps_used])
        return {"action": "done", "summary": "scripted sequence complete"}


class CodexPlanner(Planner):
    """Real planner: asks Codex for the next single action.

    Mirrors the project's ``codex exec --json`` invocation (see
    runner/operators/run.py) but feeds a strict one-action prompt and
    reads the final message file for the JSON reply. The model is told
    to output ONLY the JSON object — no prose.
    """

    def __init__(self, settings: Settings, *, sandbox: str = "danger-full-access") -> None:
        self.settings = settings
        self.sandbox = sandbox

    def _build_prompt(
        self,
        *,
        goal: str,
        observation: dict,
        trajectory: list[dict],
        steps_used: int,
        max_steps: int,
    ) -> str:
        allowed = ", ".join(_ALLOWED)
        has_ax = any(
            observation.get(k) is not None
            for k in ("pid", "window_id", "element_hints", "elements")
        ) if isinstance(observation, dict) else False
        ax_rule = (
            "硬性规则：当前观察已提供 AX 信息（pid/window_id/element_hints）。"
            "下一次 click 必须使用 "
            '{"action":"click","pid":…,"window_id":…,"element_index":…}；'
            "禁止只输出 x/y 坐标 click。\n"
            "从 element_hints 里按 label 匹配目标（例如数字 1 → label 为 \"1\" 的 AXButton）。\n"
            if has_ax
            else
            "若尚无 AX 信息：先 observe；拿到 element_hints 后再 AX click。"
            "仅当多次观察仍无 pid/element_hints 时，才允许 x/y click。\n"
        )
        digit = extract_single_digit_click_goal(goal)
        digit_rule = ""
        if digit is not None:
            digit_rule = (
                f"完成规则（单数字目标={digit}）：\n"
                f"- 最多点击一次 label 为 \"{digit}\" 的按钮，然后必须输出 done。\n"
                "- 禁止再点其它数字或运算符；禁止为了“确认”重复点击。\n"
                "- 若轨迹里已有对该数字的成功 click，直接 done。\n"
            )
        else:
            digit_rule = (
                "完成规则：目标一旦达成立即 done，不要多余 click。"
                "不要为了“保险”重复同一操作。\n"
            )
        return (
            "你是桌面自动化规划器。目标：\n"
            f"{goal}\n\n"
            "只输出一个 JSON 对象（不要任何解释、不要 markdown 代码块），"
            "描述下一步要执行的单个桌面动作。可选 action：\n"
            f"{allowed}\n\n"
            "点击策略（AX-first）：\n"
            "- 当观察结果提供 pid/window_id/element_index（或 element_token）时，"
            "优先输出 AX click，不要只用 x/y。\n"
            "- 仅当观察中没有 AX 字段时，才使用坐标 click。\n"
            "- 若观察里有 elements / element_hints / action_hints，先据此选择目标。\n"
            f"{ax_rule}"
            f"{digit_rule}\n"
            "动作示例：\n"
            '{"action":"observe"}\n'
            '{"action":"click","pid":123,"window_id":0,"element_index":5}\n'
            '{"action":"click","x":123,"y":456}\n'
            '{"action":"type","text":"要输入的文字"}\n'
            '{"action":"hotkey","keys":["cmd","l"]}\n'
            '{"action":"scroll","dx":0,"dy":-500}\n'
            '{"action":"wait","seconds":1}\n'
            '{"action":"done","summary":"完成说明"}\n'
            '{"action":"stop","reason":"无法继续的原因"}\n\n'
            f"当前观察: {_obs_summary(observation)}\n"
            f"已完成步骤 ({steps_used}/{max_steps}):\n{_trajectory_summary(trajectory)}\n\n"
            "输出下一个动作（若目标已完成则输出 done）："
        )

    async def next_action(
        self,
        *,
        goal: str,
        observation: dict,
        trajectory: list[dict],
        steps_used: int,
        max_steps: int,
    ) -> dict:
        prompt = self._build_prompt(
            goal=goal,
            observation=observation,
            trajectory=trajectory,
            steps_used=steps_used,
            max_steps=max_steps,
        )
        try:
            raw = await self._run_codex(prompt)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("CodexPlanner codex run failed: %s", exc)
            return {"action": "stop", "reason": f"planner_error:{type(exc).__name__}"}
        return self._parse_action(raw)

    async def _run_codex(self, prompt: str) -> str:
        settings = self.settings
        worktree = Path(settings.codex_workspace_root)
        add_dir = Path(settings.codex_task_root)
        with tempfile.NamedTemporaryFile(
            "r+", suffix=".txt", delete=False, encoding="utf-8",
        ) as out_file:
            out_path = out_file.name
        try:
            command = [
                settings.codex_bin,
                "exec",
                "--json",
                "--sandbox", self.sandbox,
                "--cd", str(worktree),
                "--add-dir", str(add_dir),
                "--output-last-message", out_path,
                "-",
            ]
            if settings.codex_model:
                command[2:2] = ["--model", settings.codex_model]
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(os.environ),
            )
            assert proc.stdin is not None
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=settings.codex_timeout_seconds)
            try:
                return Path(out_path).read_text(encoding="utf-8", errors="replace")
            except Exception:
                return ""
        finally:
            try:
                os.unlink(out_path)
            except Exception:
                pass

    @staticmethod
    def _parse_action(raw: str) -> dict:
        text = (raw or "").strip()
        if not text:
            return {"action": "stop", "reason": "empty_planner_output"}
        # Find the first balanced-ish JSON object in the output.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {"action": "stop", "reason": "no_json_in_planner_output"}
        try:
            obj = json.loads(text[start:end + 1])
        except Exception:
            return {"action": "stop", "reason": "invalid_json_in_planner_output"}
        if not isinstance(obj, dict) or "action" not in obj:
            return {"action": "stop", "reason": "planner_action_missing"}
        return obj
