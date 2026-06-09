#!/usr/bin/env python3
"""End-to-end smoke for the categorized MEMO fast path.

Exercises the real runner.append_memo / read_memory / read_journal /
classify_memo methods against the VPS workspace, bypassing the Telegram
webhook. Use this when you don't have a real Telegram client handy.

Prints one [ok]/[fail] line per assertion and exits non-zero on any failure.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_settings
from runner import CodexRunner, Job, JobMode
from scripts.harness_common import CheckResult, print_results


def _truncate(text: str, limit: int = 200) -> str:
    text = (text or "").replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


async def main() -> int:
    settings = load_settings(".env")
    runner = CodexRunner(settings)
    results = []

    # 1. Tagged preference append (no timestamp).
    try:
        s1 = await runner.append_memo("preference", "smoke-from-memo-smoke.py", auto_timestamp=False)
        # Idempotency contract: the entry is in MEMORY.md. Either this call
        # wrote it ("记下了: ...") or the runner's cross-section dedup
        # noticed it was already there from a prior run ("已存在: ...").
        ok = s1.startswith(("记下了", "已存在"))
        results.append(CheckResult("preference append", ok, s1))
    except Exception as exc:
        results.append(CheckResult("preference append", False, f"raised {type(exc).__name__}: {exc}"))

    # 2. Fact append with auto timestamp.
    try:
        s2 = await runner.append_memo("fact", "TSLA close $248", auto_timestamp=True)
        # Same idempotency contract as test 1; timestamped fact lines also
        # dedup against their own prior selves.
        body_ok = "TSLA close $248" in s2 and s2.startswith(("记下了", "已存在"))
        results.append(CheckResult("fact append (auto-ts)", body_ok, s2))
    except Exception as exc:
        results.append(CheckResult("fact append (auto-ts)", False, f"raised {type(exc).__name__}: {exc}"))

    # 3. Classify: explicit preference text.
    try:
        c1 = await runner.classify_memo("用 pnpm 而不是 npm")
        results.append(CheckResult("classify preference", c1 == "preference", c1))
    except Exception as exc:
        results.append(CheckResult("classify preference", False, f"raised {type(exc).__name__}: {exc}"))

    # 4. Classify: empty -> unfiled (no API call, fast path).
    try:
        c2 = await runner.classify_memo("")
        results.append(CheckResult("classify empty -> unfiled", c2 == "unfiled", c2))
    except Exception as exc:
        results.append(CheckResult("classify empty -> unfiled", False, f"raised {type(exc).__name__}: {exc}"))

    # 5. read_memory: full dump contains both new entries.
    try:
        full = runner.read_memory()
        ok = "smoke-from-memo-smoke.py" in full and "TSLA close $248" in full
        results.append(CheckResult("read_memory full contains both", ok, _truncate(full)))
    except Exception as exc:
        results.append(CheckResult("read_memory full contains both", False, f"raised {type(exc).__name__}: {exc}"))

    # 6. read_memory(category="preference"): only that section.
    try:
        pref = runner.read_memory("preference")
        ok = "## preference" in pref and "TSLA close $248" not in pref
        results.append(CheckResult("read_memory preference", ok, _truncate(pref)))
    except Exception as exc:
        results.append(CheckResult("read_memory preference", False, f"raised {type(exc).__name__}: {exc}"))

    # 7. Seed an unfiled memo, then read it back. The ## unfiled section
    #    only appears once at least one entry lands there. This also
    #    exercises the LLM-failure -> unfiled fallback path.
    try:
        s7 = await runner.append_memo("unfiled", "悬而未决的备忘条目", auto_timestamp=False)
        # Idempotency: this marker was seeded multiple times before dedup
        # landed (3 copies still on disk as evidence). The contract is
        # "unfiled section contains the entry", not "this call wrote it".
        ok = s7.startswith(("记下了: unfiled", "已存在: unfiled"))
        results.append(CheckResult("unfiled seed", ok, s7))
    except Exception as exc:
        results.append(CheckResult("unfiled seed", False, f"raised {type(exc).__name__}: {exc}"))

    try:
        unfiled = runner.read_memory("unfiled")
        ok = unfiled.startswith("## unfiled") and "悬而未决的备忘条目" in unfiled
        results.append(CheckResult("read_memory unfiled", ok, _truncate(unfiled, 120)))
    except Exception as exc:
        results.append(CheckResult("read_memory unfiled", False, f"raised {type(exc).__name__}: {exc}"))

    # 8. read_journal with non-existent date -> empty.
    try:
        empty = runner.read_journal("1999-01-01")
        results.append(CheckResult("read_journal missing -> empty", empty == "", repr(empty)))
    except Exception as exc:
        results.append(CheckResult("read_journal missing -> empty", False, f"raised {type(exc).__name__}: {exc}"))

    # 9. read_journal("2026-06-03") -> today's archive (or empty if 12pm
    #    hasn't run yet). Either branch is fine, just verify no crash.
    try:
        journal = runner.read_journal("2026-06-03")
        results.append(CheckResult("read_journal 2026-06-03 (may be empty)", isinstance(journal, str), _truncate(journal, 120)))
    except Exception as exc:
        results.append(CheckResult("read_journal 2026-06-03", False, f"raised {type(exc).__name__}: {exc}"))

    # 10. MEMORY.md file actually exists on disk and is non-empty.
    try:
        wt = runner._today_worktree_path()
        mem = wt / "MEMORY.md"
        ok = mem.exists() and mem.stat().st_size > 0
        size = mem.stat().st_size if mem.exists() else 0
        results.append(CheckResult("MEMORY.md on disk", ok, f"{mem} ({size} bytes)"))
    except Exception as exc:
        results.append(CheckResult("MEMORY.md on disk", False, f"raised {type(exc).__name__}: {exc}"))

    # 11. Reject unknown category at the API boundary.
    try:
        await runner.append_memo("bogus-category", "x")
        results.append(CheckResult("unknown category rejected", False, "no exception raised"))
    except ValueError:
        results.append(CheckResult("unknown category rejected", True, "ValueError as expected"))
    except Exception as exc:
        results.append(CheckResult("unknown category rejected", False, f"unexpected {type(exc).__name__}: {exc}"))

    # 12. Reject empty content at the API boundary.
    try:
        await runner.append_memo("fact", "   ")
        results.append(CheckResult("empty content rejected", False, "no exception raised"))
    except ValueError:
        results.append(CheckResult("empty content rejected", True, "ValueError as expected"))
    except Exception as exc:
        results.append(CheckResult("empty content rejected", False, f"unexpected {type(exc).__name__}: {exc}"))

    # 13. reclassify_unfiled round-trip. Synthesize a MEMORY.md blob in
    #     memory (no disk writes), monkey-patch classify_memo to a fixed
    #     stub, and verify that a known-fact line under "## unfiled" lands
    #     under "## fact" in the returned content, that the unfiled line
    #     count drops, and that a still-ambiguous line stays in unfiled.
    try:
        synthesized = (
            "# MEMORY.md\n\n"
            "## preference\n- old pref\n\n"
            "## unfiled\n"
            "- the user's name is Alice\n"
            "- another unclassified item\n"
        )

        original_classify = runner.classify_memo

        async def _stub_classify(text: str) -> str:
            return "fact" if "Alice" in text else "unfiled"

        runner.classify_memo = _stub_classify  # type: ignore[method-assign]
        try:
            new_content, moved = await runner.reclassify_unfiled(synthesized)
        finally:
            runner.classify_memo = original_classify  # type: ignore[method-assign]

        has_fact_section = "## fact" in new_content
        if has_fact_section:
            after_fact = new_content.split("## fact", 1)[1]
            next_h = after_fact.find("\n## ")
            fact_body = after_fact if next_h == -1 else after_fact[:next_h]
            alice_in_fact = "the user's name is Alice" in fact_body
        else:
            alice_in_fact = False
        unfiled_tail = (
            new_content.split("## unfiled", 1)[1] if "## unfiled" in new_content else ""
        )
        alice_still_unfiled = "the user's name is Alice" in unfiled_tail
        other_stays = "another unclassified item" in unfiled_tail
        pref_kept = "## preference" in new_content and "old pref" in new_content
        ok = (
            moved == 1
            and has_fact_section
            and alice_in_fact
            and not alice_still_unfiled
            and other_stays
            and pref_kept
        )
        results.append(CheckResult(
            "reclassify_unfiled round-trip",
            ok,
            f"moved={moved}, has_fact={has_fact_section}, alice_in_fact={alice_in_fact}, "
            f"alice_still_unfiled={alice_still_unfiled}, other_stays={other_stays}, pref_kept={pref_kept}",
        ))
    except Exception as exc:
        results.append(CheckResult("reclassify_unfiled round-trip", False, f"raised {type(exc).__name__}: {exc}"))

    # 14. CLI `memorize` subprocess. Spawns `python -m runner memorize` in a
    #     child process, asserts rc + stdout prefix, and confirms the line
    #     landed in MEMORY.md. Uses --env-file to make env discovery
    #     deterministic independent of the parent shell.
    project_root = Path(__file__).resolve().parents[1]
    py_bin = str(project_root / ".venv" / "bin" / "python")
    try:
        proc = subprocess.run(
            [py_bin, "-m", "runner", "memorize", "--env-file", ".env.test",
             "smoke-from-cli-subprocess"],
            cwd=project_root,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        stdout = proc.stdout or ""
        # Idempotency: the marker is in MEMORY.md from prior smoke runs;
        # the CLI subprocess will dedup-skip on a second append. Accept
        # either "记下了" (fresh) or "已存在" (skip) — rc must still be 0
        # and the entry must be findable via read_memory.
        ok = (proc.returncode == 0
              and stdout.startswith(("记下了: ", "已存在: "))
              and "smoke-from-cli-subprocess" in runner.read_memory())
        detail = (f"rc={proc.returncode} stdout={_truncate(stdout, 80)!r} "
                  f"stderr={_truncate(proc.stderr or '', 80)!r}")
        results.append(CheckResult("CLI memorize subprocess", ok, detail))
    except Exception as exc:
        results.append(CheckResult("CLI memorize subprocess", False, f"raised {type(exc).__name__}: {exc}"))

    # 15. CLI `recall-journal` for a non-existent date returns rc=0 and
    #     empty stdout. Verifies the CLI subcommand handles the empty
    #     archive case without crashing.
    try:
        proc = subprocess.run(
            [py_bin, "-m", "runner", "recall-journal", "--env-file", ".env.test",
             "1999-01-01"],
            cwd=project_root,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        ok = proc.returncode == 0 and (proc.stdout or "") == ""
        detail = (f"rc={proc.returncode} stdout={proc.stdout!r} "
                  f"stderr={_truncate(proc.stderr or '', 80)!r}")
        results.append(CheckResult("CLI recall-journal empty", ok, detail))
    except Exception as exc:
        results.append(CheckResult("CLI recall-journal empty", False, f"raised {type(exc).__name__}: {exc}"))

    # 16. tool-registry payload: chat-first — RUN and FIX share workspace-write.
    try:
        def _registry_ok(text: str) -> bool:
            return (
                "<tool-registry" in text
                and "memorize" in text
                and 'policy="' in text
                and "CODEX_WORKSPACE_ROOT" in text
                and "workspace-write" in text
            )

        fix_text = runner._prefetch_memory(Job(
            id="smoke-test-fix", mode=JobMode.FIX, prompt="x", sandbox="workspace-write",
        ))
        run_text = runner._prefetch_memory(Job(
            id="smoke-test-run", mode=JobMode.RUN, prompt="x", sandbox="workspace-write",
        ))
        ok = _registry_ok(fix_text) and _registry_ok(run_text)
        detail = (
            f"fix_ok={_registry_ok(fix_text)} run_ok={_registry_ok(run_text)} "
            f"run_has_readonly={'read-only' in run_text}"
        )
        results.append(CheckResult("tool-registry payload (FIX + RUN)", ok, detail))
    except Exception as exc:
        results.append(CheckResult("tool-registry payload (FIX + RUN)", False, f"raised {type(exc).__name__}: {exc}"))

    # 16b. Plain chat must mention web tools and must not forbid network.
    try:
        run_web_text = runner._prefetch_memory(Job(
            id="smoke-test-run-web", mode=JobMode.RUN, prompt="x", sandbox="workspace-write",
        ))
        web_mentioned = "web tools" in run_web_text.lower()
        no_network_clause = "no network" in run_web_text.lower()
        ok = web_mentioned and not no_network_clause
        detail = f"web_mentioned={web_mentioned}/no_network_clause={no_network_clause}"
        results.append(CheckResult("RUN mode web tools by default", ok, detail))
    except Exception as exc:
        results.append(CheckResult("RUN mode web tools by default", False, f"raised {type(exc).__name__}: {exc}"))

    # 17. tool-registry must warn that apply_patch is unavailable (RUN + FIX).
    try:
        fix_text = runner._prefetch_memory(Job(
            id="smoke-test-fix-apply-patch", mode=JobMode.FIX, prompt="x", sandbox="workspace-write",
        ))
        run_text = runner._prefetch_memory(Job(
            id="smoke-test-run-apply-patch", mode=JobMode.RUN, prompt="x", sandbox="workspace-write",
        ))

        def _apply_patch_warns(text: str) -> bool:
            return (
                "apply_patch" in text
                and "unsupported call" in text
                and "python -m runner memorize" in text
                and "$CODEX_RUNNER_HOME" in text
                and ".venv/bin/python -m runner memorize" in text
            )

        ok = _apply_patch_warns(fix_text) and _apply_patch_warns(run_text)
        detail = f"fix_warns={_apply_patch_warns(fix_text)} run_warns={_apply_patch_warns(run_text)}"
        results.append(CheckResult("tool-registry warns apply_patch unavailable", ok, detail))
    except Exception as exc:
        results.append(CheckResult("tool-registry warns apply_patch unavailable", False, f"raised {type(exc).__name__}: {exc}"))

    # 18. codex subprocess plumbing: the runner home must be passed into
    #     the sandbox as both --add-dir (so the shell can `cd` there) and
    #     the CODEX_RUNNER_HOME env var (so the tool-registry's
    #     `cd "$CODEX_RUNNER_HOME" && .venv/bin/python -m runner memorize`
    #     template resolves to a real path). Without these, the model's
    #     only "memorize" fallback is to rewrite MEMORY.md by hand, which
    #     was the root cause of the 08:34 /fix bypass.
    try:
        from runner import RUNNER_HOME as runner_home_const
        job = Job(
            id="smoke-test-plumbing",
            mode=JobMode.FIX,
            prompt="x",
            sandbox="workspace-write",
        )
        command = runner._codex_command(job)
        env = runner._child_env()
        ok = (
            "--add-dir" in command
            and str(runner_home_const) in command
            and env.get("CODEX_RUNNER_HOME") == str(runner_home_const)
        )
        detail = (f"--add-dir={'--add-dir' in command}, "
                  f"runner_home_in_cmd={str(runner_home_const) in command} "
                  f"(runner_home={runner_home_const!r}), "
                  f"env_runner_home={env.get('CODEX_RUNNER_HOME')!r}")
        results.append(CheckResult("runner home plumbed into codex sandbox", ok, detail))
    except Exception as exc:
        results.append(CheckResult("runner home plumbed into codex sandbox", False, f"raised {type(exc).__name__}: {exc}"))

    # 19. Cross-section dedup: appending the same content to two different
    #     categories must not double-write. This is the guard against the
    #     "4x TSLA" failure mode from today's MEMORY.md archive. The marker
    #     uses a uuid suffix so the test is re-runnable without self-
    #     colliding on subsequent runs (the entry is left behind, but a
    #     fresh uuid each run keeps count == 1 for THIS test's marker).
    try:
        marker = f"dedup-smoke-{uuid4().hex[:10]}"
        s1 = await runner.append_memo("fact", marker, auto_timestamp=True)
        s2 = await runner.append_memo("tool-quirk", marker, auto_timestamp=False)
        full = runner.read_memory()
        count = sum(1 for ln in full.splitlines() if marker in ln)
        first_added = s1.startswith("记下了")
        second_skipped = s2.startswith("已存在")
        ok = count == 1 and first_added and second_skipped
        detail = f"count={count} (want 1), first={s1!r}, second={s2!r}"
        results.append(CheckResult("memorize dedup cross-section", ok, detail))
    except Exception as exc:
        results.append(CheckResult("memorize dedup cross-section", False, f"raised {type(exc).__name__}: {exc}"))

    ok = print_results(results)
    if ok:
        print("memo smoke ok")
    else:
        print("memo smoke failed")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
