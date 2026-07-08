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
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from config import Settings

logger = logging.getLogger(__name__)


_ALLOWED = ("observe", "click", "type", "hotkey", "scroll", "wait", "done", "stop")


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
    sid = observation.get("screenshot_id") or observation.get("sha256")
    if sid:
        return f"screenshot {sid} ({observation.get('width')}x{observation.get('height')})"
    return "no screenshot yet"


def _trajectory_summary(trajectory: list[dict]) -> str:
    if not trajectory:
        return "(none)"
    lines = []
    for entry in trajectory[-8:]:
        if not isinstance(entry, dict):
            continue
        act = entry.get("action_type") or entry.get("action") or "?"
        lines.append(f"- {act}")
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
        return (
            "你是桌面自动化规划器。目标：\n"
            f"{goal}\n\n"
            "只输出一个 JSON 对象（不要任何解释、不要 markdown 代码块），"
            "描述下一步要执行的单个桌面动作。可选 action：\n"
            f"{allowed}\n\n"
            "动作示例：\n"
            '{"action":"observe"}\n'
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
