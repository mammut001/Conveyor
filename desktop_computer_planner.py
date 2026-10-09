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
  Later steps of the same task resume that Codex thread.
- ``ScriptedPlanner``: deterministic, network-free. Used by the smoke
  suite and as a fallback when Codex is unavailable.

Simple single-digit Calculator goals (e.g. “点击数字 1”) are handled by
``maybe_simple_digit_action`` *before* Codex, so the product path does
not thrash multiple AX buttons (which produced displays like ``113``).
A short named follow-up such as “再点等号” is handled by
``maybe_followup_label_action``: it presses that button and does not
clear the calculator. A label that is not on screen still goes to Codex.
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
from desktop_computer_requests import _HARD_BLOCKED_KEYWORDS, _LOGIN_PASSWORD_KEYWORDS
from desktop_screenshot import resolve_screenshot_dir

logger = logging.getLogger(__name__)


_BROWSER_NAV_RULE = (
    "浏览器导航：地址栏输入网址或搜索词后必须按 Enter 提交，再根据新页面截图核实结果。"
    "地址栏显示新网址不表示已打开该页面；禁止把旧正文作为新结果。"
    "状态 address_focused 表示地址栏已经聚焦，下一步应 type，勿重复 Ctrl+L；"
    "awaiting_submit 表示网址已输入但未提交，下一步应 hotkey [\"enter\"]，勿 done。"
    "若用户明确要求只输入不提交，则遵守该要求。\n"
)


_ALLOWED = ("observe", "click", "type", "hotkey", "scroll", "wait", "done", "stop")

# Preferred Clear button labels on macOS Calculator (short, safe).
_CLEAR_LABELS = ("all clear", "clear", "ac", "c")


def is_observe_only_goal(goal: str) -> bool:
    """True only for goals that explicitly require read-only observation."""
    text = (goal or "").strip().lower()
    if not text:
        return False
    asks_observe = any(token in text for token in ("observe", "观察", "看一下", "查看"))
    forbids_mutation = any(
        token in text
        for token in (
            "不要点击", "严禁点击", "禁止点击", "不要输入", "严禁输入",
            "只观察", "仅观察", "observe only", "only observe",
            "do not click", "don't click", "without clicking",
            "do not type", "without typing",
        )
    )
    if not (asks_observe and forbids_mutation):
        return False
    # Remove explicit prohibition clauses, then reject any remaining positive
    # action clause (for example: "不要输入，然后点击 1").
    remaining = re.sub(
        r"(?:不要|严禁|禁止|不)(?:(?:点击|输入|滚动|按下|快捷键|、|，|或|和|\s))+",
        "",
        text,
    )
    remaining = re.sub(
        r"(?:do\s+not|don't|without)\s+"
        r"(?:click(?:ing)?|typ(?:e|ing)|scroll(?:ing)?|press(?:ing)?|hotkey)"
        r"(?:\s*(?:,|or|and)\s*"
        r"(?:click(?:ing)?|typ(?:e|ing)|scroll(?:ing)?|press(?:ing)?|hotkey))*",
        "",
        remaining,
    )
    return re.search(
        r"(?:点击|输入|滚动|按下)|\b(?:click|type|scroll|hotkey|press)\b",
        remaining,
    ) is None


def maybe_observe_only_action(
    *,
    goal: str,
    trajectory: list[dict],
) -> dict | None:
    """Deterministic observe -> done policy for explicit read-only goals."""
    if not is_observe_only_goal(goal):
        return None
    observed = any(
        isinstance(entry, dict)
        and entry.get("action_type") == "observe"
        and entry.get("result_ok", True)
        for entry in (trajectory or [])
    )
    if observed:
        return {"action": "done", "summary": "read-only observation completed"}
    return {"action": "observe"}


def infer_target_app(goal: str) -> str | None:
    """Infer only an explicitly named common desktop app from a goal."""
    raw = goal or ""
    if "计算器" in raw:
        return "Calculator"
    text = raw.lower()
    apps = (
        "calculator", "safari", "chrome", "google chrome", "firefox",
        "finder", "notes", "textedit", "calendar", "mail", "slack",
        "chatgpt", "preview", "music", "spotify",
    )
    for app in sorted(apps, key=len, reverse=True):
        if app in text:
            return app.title() if app != "textedit" else "TextEdit"
    return None


def goal_needs_loaded_page(goal: str) -> bool:
    """True when success requires leaving the browser's initial blank page.

    The loop only uses this with a window title the desktop already reported.
    It does not read pixels or decide whether a forecast is correct.
    """
    text = (goal or "").lower()
    return any(word in text for word in (
        "webpage", "website", "weather", "http://", "https://",
        "网页", "网站", "天气",
    ))


def goal_needs_browser(goal: str) -> bool:
    """Recognize desktop goals whose first useful surface is a browser.

    Only used for a fixed browser bootstrap, not for URL inference, shell
    commands, or changing the user's selected desktop.
    """
    text = (goal or "").lower()
    return any(word in text for word in (
        "browser", "firefox", "chromium", "chrome", "safari", "webpage", "website",
        "weather", "浏览器", "网页", "网站", "天气",
    ))


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
        # An observe already saved a screenshot and still has no button
        # labels. Hand the image to the model instead of observing forever.
        if _observation_has_screenshot(obs):
            return None
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
        if _observation_has_screenshot(obs):
            return None
        return {
            "action": "stop",
            "reason": f"digit_{digit}_not_in_element_hints",
        }
    click = _ax_click_from_hint(obs, digit_hint)
    if click is None:
        return {"action": "stop", "reason": f"digit_{digit}_ax_incomplete"}
    return click


# "点击" must be tried before "点", or the label keeps the extra character.
_FOLLOWUP_CLICK_LABEL = re.compile(
    r"^(?:再|然后|接着|继续)?再?(?:点击|点|按)\s*(.+)$"
)

# Spoken calculator buttons. The on-screen label is the symbol, not the word.
_SPOKEN_BUTTONS = {
    "等号": ("=", "equals"),
    "等于": ("=", "equals"),
    "加号": ("+", "＋", "add", "plus"),
    "减号": ("-", "−", "minus", "subtract"),
    "乘号": ("×", "*", "multiply"),
    "除号": ("÷", "/", "divide"),
    "小数点": (".", "decimal", "point"),
    "清除": ("Clear", "All Clear", "AC"),
    "全清": ("All Clear", "AC", "Clear"),
    "归零": ("All Clear", "AC"),
}


def _followup_spoken(goal: str) -> str | None:
    """The button words in a short click follow-up, or None."""
    body = (goal or "").strip()
    if not body or len(body) > 32:
        return None
    match = _FOLLOWUP_CLICK_LABEL.match(body)
    if match is None:
        return None
    label = match.group(1).strip().strip("。.!！?？，,")
    if not label or len(label) > 16:
        return None
    # A single digit stays on the digit shortcut, which clears first.
    if len(label) == 1 and label.isdigit():
        return None
    lowered = label.lower()
    for keyword in (*_HARD_BLOCKED_KEYWORDS, *_LOGIN_PASSWORD_KEYWORDS):
        if keyword and keyword in lowered:
            return None
    return label


def followup_click_labels(goal: str) -> tuple[str, ...] | None:
    """Labels to press for a named follow-up click, or None to use Codex."""
    spoken = _followup_spoken(goal)
    if spoken is None:
        return None
    aliases = _SPOKEN_BUTTONS.get(spoken)
    if aliases:
        return (spoken, *aliases)
    return (spoken,)


def _calculator_window(observation: dict) -> dict | None:
    """A calculator window from the observe list, if one is open."""
    windows = observation.get("windows") if isinstance(observation, dict) else None
    if not isinstance(windows, list):
        return None
    for row in windows:
        if not isinstance(row, dict):
            continue
        app = str(row.get("app") or "")
        title = str(row.get("title") or "")
        blob = f"{app} {title}".lower()
        if "calc" not in blob and "计算器" not in app and "计算器" not in title:
            continue
        try:
            int(row.get("pid"))
            int(row.get("window_id"))
        except (TypeError, ValueError):
            continue
        return row
    return None


def maybe_followup_label_action(
    *,
    goal: str,
    observation: dict,
    trajectory: list[dict],
) -> dict | None:
    """Press a named button without Codex. Never clears unless asked.

    ``再点等号`` looks for ``=``. The chat window is often in front after
    发送, so a calculator word whose label is missing looks at the
    calculator window next. Any other miss returns None and the model
    sees the screenshot. This path does not press Clear on the way.
    """
    spoken = _followup_spoken(goal)
    wanted = followup_click_labels(goal)
    if spoken is None or wanted is None:
        return None

    done = {lab.lower() for lab in _trajectory_labels(trajectory)}
    if any(label.lower() in done for label in wanted):
        return {"action": "done", "summary": f"clicked {wanted[0]}"}

    obs = observation if isinstance(observation, dict) else {}
    hints = obs.get("element_hints")
    pid = obs.get("pid")
    window_id = obs.get("window_id")
    if not isinstance(hints, list) or not hints or pid is None or window_id is None:
        if _observation_has_screenshot(obs):
            return None
        return {"action": "observe"}

    hint = _find_hint(obs, labels=wanted)
    if hint is not None:
        return _ax_click_from_hint(obs, hint)

    if spoken in _SPOKEN_BUTTONS:
        window = _calculator_window(obs)
        if window is not None:
            target_pid = int(window["pid"])
            target_wid = int(window["window_id"])
            if target_pid != int(pid):
                # Do not set target_app. On Linux the app name is not
                # "Calculator", and a name miss fails the step.
                return {
                    "action": "observe",
                    "pid": target_pid,
                    "window_id": target_wid,
                }
    return None


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


_SCREENSHOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")


def _observation_has_screenshot(observation: dict) -> bool:
    sid = observation.get("screenshot_id") if isinstance(observation, dict) else None
    return isinstance(sid, str) and bool(sid.strip())


def planner_screenshot_path(settings: Settings, observation: dict) -> Path | None:
    """Local PNG for this observation, or None when the step has no image.

    The clicker saves ``{screenshot_id}.png`` under the desktop screenshot
    directory. Only a regular file inside that directory is attached, so a
    path in the observation cannot point the model at an arbitrary file.
    """
    if not isinstance(observation, dict):
        return None
    root = resolve_screenshot_dir(settings)
    sid = observation.get("screenshot_id")
    if isinstance(sid, str) and _SCREENSHOT_ID.match(sid):
        candidate = (root / f"{sid}.png").resolve()
        if _file_inside(candidate, root):
            return candidate
    raw = observation.get("path")
    if isinstance(raw, str) and raw.strip():
        candidate = Path(raw).expanduser().resolve()
        if candidate.suffix.lower() == ".png" and _file_inside(candidate, root):
            return candidate
    return None


def _file_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    return path.is_file()


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
    nav = observation.get("browser_navigation_status")
    if nav in ("address_focused", "awaiting_submit", "unknown"):
        parts.append(f"browser_navigation_status={nav}")
    # Surface AX / element / action hints so the planner can prefer them.
    for key in (
        "pid", "window_id", "element_index", "element_token",
        "elements", "element_hints", "windows", "action_hints", "ax_hints",
        "click_method", "effect",
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
            limit = 900 if key == "element_hints" else 2000 if key == "windows" else 240
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
    for entry in trajectory[-10:]:
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
        effect = entry.get("effect")
        if effect == "no_visible_change":
            extra += " visual_state=unchanged"
        err = entry.get("error")
        if isinstance(err, str) and err.strip():
            extra += f" error={err.strip()[:64]}"
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


_CLICK_RULE = (
    "点击规则：\n"
    "- element_hints 里有目标的 label 时，用 element_index 点那个按钮。\n"
    "- 没有对应 label 时，用截图的窗口内像素作为 x、y，并带上 pid 和 window_id。"
    "落在按钮框里的点会按下那个按钮。\n"
    "- 点窗口的屏幕中心只会把窗口放到最前，不会按下按钮。"
    "只有该窗口还不是最前的非面板窗口时，才点它的屏幕中心。\n"
)


_SCREEN_CLICK_RULE = (
    "点击规则（这台桌面只有一张全屏截图）：\n"
    "- x、y 是截图里的屏幕像素，直接点你看到的位置；不需要 pid、window_id、element_index。\n"
    "- type、hotkey、scroll 作用在当前有焦点的地方：先 click 输入框，再 type。\n"
    "- 打开网址：hotkey [\"ctrl\",\"l\"] 聚焦地址栏，type 网址，再 hotkey [\"enter\"]。\n"
    "- 桌面上只有浏览器，不要尝试打开别的应用。\n"
)
_SCREEN_EXAMPLES = (
    '{"action":"observe"}\n'
    '{"action":"click","x":640,"y":360}\n'
    '{"action":"type","text":"要输入的文字"}\n'
    '{"action":"hotkey","keys":["ctrl","l"]}\n'
    '{"action":"scroll","dx":0,"dy":500}\n'
    '{"action":"wait","seconds":1}\n'
    '{"action":"done","summary":"完成说明"}\n'
    '{"action":"stop","reason":"无法继续的原因"}\n\n'
)


_THREAD_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _thread_id_from_jsonl(raw: bytes) -> str | None:
    """Session id from a ``thread.started`` event. Anything else is ignored."""
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "thread.started":
            continue
        thread_id = event.get("thread_id")
        if isinstance(thread_id, str) and _THREAD_ID.match(thread_id):
            return thread_id
    return None


async def _read_all(stream) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    while True:
        block = await stream.read(65536)
        if not block:
            break
        chunks.append(block)
    return b"".join(chunks)


class CodexPlanner(Planner):
    """Real planner: asks Codex for the next single action.

    Mirrors the project's ``codex exec --json`` invocation (see
    runner/operators/run.py) but feeds a strict one-action prompt and
    reads the final message file for the JSON reply. The model is told
    to output ONLY the JSON object — no prose.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        sandbox: str = "danger-full-access",
        resume_thread_id: str | None = None,
        screen_coordinates: bool = False,
    ) -> None:
        self.settings = settings
        self.sandbox = sandbox
        # True on an agent's own desktop: one full-screen screenshot per
        # observation and clicks in its pixels (see desktop_x11.py).
        self.screen_coordinates = screen_coordinates
        self._thread_id: str | None = None
        if isinstance(resume_thread_id, str) and _THREAD_ID.fullmatch(resume_thread_id):
            self._thread_id = resume_thread_id

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

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
                "不要为了“保险”重复同一操作。"
                "上一步失败时不要 done。\n"
            )
        if self.screen_coordinates:
            return (
                "你是桌面自动化规划器。目标：\n"
                f"{goal}\n\n"
                "只输出一个 JSON 对象（不要任何解释、不要 markdown 代码块），"
                "描述下一步要执行的单个桌面动作。可选 action：\n"
                f"{allowed}\n\n"
                f"{_SCREEN_CLICK_RULE}"
                f"{_BROWSER_NAV_RULE}"
                "- 上一次动作失败时不要输出 done。\n"
                f"{digit_rule}\n"
                "动作示例：\n"
                f"{_SCREEN_EXAMPLES}"
                f"当前观察: {_obs_summary(observation)}\n"
                f"已完成步骤 ({steps_used}/{max_steps}):\n{_trajectory_summary(trajectory)}\n\n"
                "输出下一个动作（若目标已完成则输出 done）："
            )
        return (
            "你是桌面自动化规划器。目标：\n"
            f"{goal}\n\n"
            "只输出一个 JSON 对象（不要任何解释、不要 markdown 代码块），"
            "描述下一步要执行的单个桌面动作。可选 action：\n"
            f"{allowed}\n\n"
            "点击策略：\n"
            f"{_CLICK_RULE}"
            f"{_BROWSER_NAV_RULE}"
            "- 若观察里有 elements / element_hints / action_hints，先据此选择目标。\n"
            "- 如果目标明确提到某个 App，observe 时加入 target_app（使用 App 的正式名称）；"
            "不要凭空猜测未提到的 App。\n"
            "- windows 列出当前窗口（app、title、z、pid、window_id、x、y、w、h）。"
            "z 越大越靠前。名字里带 panel 的条和 desktop 壁纸不是目标。\n"
            "- 上一次 click 失败，或目标窗口还不是最前的非面板窗口时，禁止输出 done。\n"
            "- type、hotkey、scroll 必须带上目标窗口的 pid 和 window_id。"
            "不要往终端里打命令来打开应用。\n"
            f"{digit_rule}\n"
            "动作示例：\n"
            '{"action":"observe"}\n'
            '{"action":"click","pid":123,"window_id":0,"element_index":5}\n'
            '{"action":"click","x":123,"y":456}\n'
            '{"action":"type","text":"要输入的文字","pid":123,"window_id":0}\n'
            '{"action":"hotkey","keys":["cmd","l"]}\n'
            '{"action":"scroll","dx":0,"dy":-500}\n'
            '{"action":"wait","seconds":1}\n'
            '{"action":"done","summary":"完成说明"}\n'
            '{"action":"stop","reason":"无法继续的原因"}\n\n'
            f"当前观察: {_obs_summary(observation)}\n"
            f"已完成步骤 ({steps_used}/{max_steps}):\n{_trajectory_summary(trajectory)}\n\n"
            "输出下一个动作（若目标已完成则输出 done）："
        )

    def _build_followup(
        self,
        *,
        goal: str,
        observation: dict,
        trajectory: list[dict],
        steps_used: int,
        max_steps: int,
    ) -> str:
        """Later step in the same Codex thread. The rules are already there."""
        return (
            "按当前要求继续操作这台桌面。只输出一个 JSON 对象，不要解释。\n"
            f"{_SCREEN_CLICK_RULE if self.screen_coordinates else _CLICK_RULE}"
            f"{_BROWSER_NAV_RULE}"
            f"目标：{goal}\n"
            f"当前观察: {_obs_summary(observation)}\n"
            f"已完成步骤 ({steps_used}/{max_steps}):\n{_trajectory_summary(trajectory)}\n"
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
        image = planner_screenshot_path(self.settings, observation)
        image_note = (
            "\n一张当前窗口截图已附在这次调用上。用图里的窗口内像素。\n"
            if image is not None
            else ""
        )
        full = prompt + image_note
        followup = self._build_followup(
            goal=goal,
            observation=observation,
            trajectory=trajectory,
            steps_used=steps_used,
            max_steps=max_steps,
        ) + image_note
        try:
            if self.screen_coordinates:
                # Keep one current frame per visual decision. Resumed threads
                # accumulate old screenshots and stale address-bar states.
                # The full goal and redacted action history still travel with
                # every call; the legacy AX/macOS resume path is unchanged.
                raw = await self._run_codex(full, image=image, resume=False)
            elif self._thread_id:
                raw = await self._run_codex(followup, image=image)
                if raw is None:
                    raw = await self._run_codex(full, image=image, resume=False)
            else:
                raw = await self._run_codex(full, image=image)
            action = self._parse_action(raw or "")
            if action.get("action") == "stop" and action.get("reason") in {
                "invalid_json_in_planner_output", "no_json_in_planner_output",
            }:
                # No desktop action has run. Retry the same observation once,
                # without quoting or recording the malformed model response.
                retry = full + "\n上一输出无法解析。只输出一个合法 JSON 动作对象，不要附加其他内容。"
                raw = await self._run_codex(retry, image=image, resume=False)
                action = self._parse_action(raw or "")
            return action
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("CodexPlanner codex run failed: %s", exc)
            return {"action": "stop", "reason": f"planner_error:{type(exc).__name__}"}

    async def _run_codex(
        self,
        prompt: str,
        *,
        image: Path | None = None,
        resume: bool = True,
    ) -> str | None:
        settings = self.settings
        worktree = Path(settings.codex_workspace_root)
        add_dir = Path(settings.codex_task_root)
        with tempfile.NamedTemporaryFile(
            "r+", suffix=".txt", delete=False, encoding="utf-8",
        ) as out_file:
            out_path = out_file.name
        proc = None
        readers: list[asyncio.Task] = []
        use_resume = bool(resume and self._thread_id)
        try:
            if use_resume:
                # Resume has no --sandbox/--cd; the first exec already set those.
                # --image sits before the next flag so it cannot swallow "-".
                command = [settings.codex_bin, "exec", "resume"]
                if image is not None:
                    command += ["--image", str(image)]
                command += ["--json", "--output-last-message", out_path]
                if settings.codex_model:
                    command += ["--model", settings.codex_model]
                command += [self._thread_id, "-"]
                logger.info("desktop planner resume")
            else:
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
                if image is not None:
                    # Same placement as the chat runner: one path, then a flag,
                    # so --image cannot swallow the trailing "-" stdin prompt.
                    command[2:2] = ["--image", str(image)]
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
            if getattr(proc, "stdout", None) is not None:
                readers.append(asyncio.create_task(_read_all(proc.stdout)))
            if getattr(proc, "stderr", None) is not None:
                readers.append(asyncio.create_task(_read_all(proc.stderr)))
            await asyncio.wait_for(proc.wait(), timeout=settings.codex_timeout_seconds)
            try:
                text = Path(out_path).read_text(encoding="utf-8", errors="replace")
            except Exception:
                text = ""
            raw_out = b""
            if readers:
                done = await asyncio.gather(*readers, return_exceptions=True)
                readers.clear()
                if done and isinstance(done[0], bytes):
                    raw_out = done[0]
            if use_resume and (proc.returncode not in (0, None) or not text.strip()):
                logger.info("desktop planner resume failed, starting a new session")
                self._thread_id = None
                return None
            found = _thread_id_from_jsonl(raw_out)
            if found and proc.returncode in (0, None):
                self._thread_id = found
            return text
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            # A task stop or planner timeout cancels this coroutine. The
            # cancellation must also reap the Codex child, otherwise a
            # detached model process can continue consuming time/API quota.
            if proc is not None and proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=3)
                    except Exception:
                        logger.warning("unable to reap cancelled Codex planner process")
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
