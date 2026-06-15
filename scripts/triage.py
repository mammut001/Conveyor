from __future__ import annotations

from collections.abc import Iterable

from scripts.harness_common import CheckResult


def advice_for_result(result: CheckResult) -> str | None:
    if result.ok:
        return None

    name = result.name.lower()
    detail = result.detail.lower()

    if name == "systemd":
        return "Check `sudo systemctl status conveyor-telegram-bot` and `sudo journalctl -u conveyor-telegram-bot -n 80 --no-pager`."
    if name == "workspace":
        return "Verify `CODEX_WORKSPACE_ROOT` points at the git repo root and the service user can read it."
    if name == "minimax":
        if "429" in detail or "rate limit" in detail or "too many requests" in detail:
            return "Provider is rate limiting; wait for cooldown, then run `scripts/smoke.py` once."
        if "401" in detail or "api key" in detail:
            return "Check `MINIMAX_API_KEY`, MiniMax base URL, and `~/.codex/config.toml` for the service user."
        return "Run `scripts/doctor.py --json` on the VPS and inspect the MiniMax `/models` detail."
    if name in {"task root", "logs", "worktrees", "disk"}:
        return "Check filesystem permissions, disk space, and ownership for `CODEX_TASK_ROOT`."
    if name == "latest completed":
        return "Run `scripts/log_summary.py latest` and inspect the latest attempt for `turn.failed` or missing final output."
    if name == "latest final":
        return "Run `scripts/metadata_report.py latest` and verify the final message path exists."
    if name == "replay":
        return "Output filtering regressed; inspect `scripts/replay.py`, `runner._event_summary`, and Telegram reply truncation/redaction."
    if name == "command_harness":
        if "no module named 'telegram'" in detail:
            return "Install service dependencies with `.venv/bin/pip install -r requirements.txt` or run this harness inside the VPS venv."
        return "Telegram command wiring regressed; inspect `bot.py` handlers, argument parsing, and patched backend calls."
    if name == "fault_harness":
        return "Runner state transitions regressed; inspect retry, cancellation, metadata writes, and rate-limit detection in `runner.py`."
    if name == "stale running jobs":
        return "Use `/status`, then `/cancel` if needed; inspect `scripts/job_audit.py` and the stale job log."
    if name == "orphan worktrees":
        return "Run `/clean` or `scripts/auto_maintain.py` after confirming no active job owns those worktrees."
    if name.startswith("conveyor-") or name in {"env permissions", "repo secret scan", "journal token scan", "task root permissions"}:
        return "Run `scripts/security_audit.py --since '1 hour ago'` and fix the named hardening or secret-hygiene item."
    return "Run `scripts/diagnostics.py --since '1 hour ago'` and inspect the failing section detail."


def triage_lines(results: Iterable[CheckResult]) -> list[str]:
    lines: list[str] = []
    for result in results:
        advice = advice_for_result(result)
        if advice:
            lines.append(f"- {result.name}: {advice}")
    return lines


def triage_text(results: Iterable[CheckResult]) -> str:
    lines = triage_lines(results)
    return "\n".join(lines) if lines else "No failing checks."
