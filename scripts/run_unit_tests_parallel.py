#!/usr/bin/env python3
"""Run the unit tests in a few worker processes (CI).

Same import mode as ``python -m unittest discover -s tests``: the tests
directory is on ``sys.path`` and each module is imported by its bare name.
Workers take whole modules from a shared queue, largest file first, so one
module's tests always run together and in their usual order.

After each module the worker restores os.environ and the working directory,
so a module that leaks an environment variable cannot change the result of a
module that happens to run after it on the same worker (the log names it).

Each worker gets its own CODEX_WORKSPACE_ROOT / CODEX_TASK_ROOT /
CODEX_MEMORY_ROOT (a ``worker-N`` subdirectory), so modules running at the same
time never share a database or a task directory.

    python scripts/run_unit_tests_parallel.py            # CONVEYOR_TEST_WORKERS or 2
    python scripts/run_unit_tests_parallel.py --workers 3

Exit status is non-zero if any test failed or errored, or a worker died.
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import queue
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
ROOT_ENV = ("CODEX_WORKSPACE_ROOT", "CODEX_TASK_ROOT", "CODEX_MEMORY_ROOT")


def _modules() -> list[str]:
    files = sorted(TESTS.glob("test*.py"), key=lambda p: p.stat().st_size, reverse=True)
    return [p.stem for p in files]


def _restore(module: str, env_before: dict[str, str], cwd_before: str) -> None:
    """Undo a module's leftover os.environ / cwd changes so the next module
    on this worker starts clean, and name them (variable names only)."""
    added = sorted(set(os.environ) - set(env_before))
    removed = sorted(set(env_before) - set(os.environ))
    changed = sorted(k for k in env_before if k in os.environ and os.environ[k] != env_before[k])
    if added or removed or changed:
        print(f"===== {module} left env changes (restored): added={added} removed={removed} changed={changed}")
        os.environ.clear()
        os.environ.update(env_before)
    if os.getcwd() != cwd_before:
        print(f"===== {module} left cwd {os.getcwd()} (restored)")
        os.chdir(cwd_before)


def _worker(index: int, todo: "mp.Queue[str | None]", results: "mp.Queue[dict]", log_dir: str) -> None:
    for name in ROOT_ENV:
        base = os.environ.get(name)
        if base:
            path = Path(base) / f"worker-{index}"
            path.mkdir(parents=True, exist_ok=True)
            os.environ[name] = str(path)
    os.chdir(ROOT)
    sys.path.insert(0, str(TESTS))
    sys.path.insert(1, str(ROOT))

    log_path = Path(log_dir) / f"worker-{index}.log"
    log = open(log_path, "w", buffering=1, encoding="utf-8")
    # Subprocesses and C extensions write to fd 1/2 directly; send them to the log too.
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    sys.stdout = sys.stderr = log

    summary = {"worker": index, "run": 0, "failures": 0, "errors": 0, "skipped": 0, "failed": []}
    loader = unittest.TestLoader()
    while True:
        module = todo.get()
        if module is None:
            break
        started = time.monotonic()
        print(f"\n===== {module} =====", flush=True)
        env_before = dict(os.environ)
        cwd_before = os.getcwd()
        suite = loader.loadTestsFromName(module)
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        _restore(module, env_before, cwd_before)
        summary["run"] += result.testsRun
        summary["failures"] += len(result.failures)
        summary["errors"] += len(result.errors)
        summary["skipped"] += len(result.skipped)
        if result.failures or result.errors:
            summary["failed"].append(module)
        print(f"===== {module} done in {time.monotonic() - started:.1f}s =====", flush=True)
    results.put(summary)
    log.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workers", type=int, default=int(os.environ.get("CONVEYOR_TEST_WORKERS", "2")))
    parser.add_argument("--log-dir", default=os.environ.get("CONVEYOR_TEST_LOG_DIR") or tempfile.mkdtemp(prefix="conveyor-test-logs-"))
    args = parser.parse_args()
    workers = max(1, args.workers)
    Path(args.log_dir).mkdir(parents=True, exist_ok=True)

    # fork is cheap on Linux (CI). On macOS a forked child that touches
    # CoreFoundation (urllib proxy lookup) is killed by the ObjC fork check.
    ctx = mp.get_context("fork" if sys.platform.startswith("linux") else "spawn")
    todo = ctx.Queue()
    results = ctx.Queue()
    modules = _modules()
    for module in modules:
        todo.put(module)
    for _ in range(workers):
        todo.put(None)

    started = time.monotonic()
    procs = [ctx.Process(target=_worker, args=(i, todo, results, args.log_dir)) for i in range(workers)]
    for proc in procs:
        proc.start()
    summaries = []
    # A worker that dies mid-module never reports; stop waiting once all have exited.
    while len(summaries) < workers:
        try:
            summaries.append(results.get(timeout=5))
        except queue.Empty:
            if not any(proc.is_alive() for proc in procs):
                break
    for proc in procs:
        proc.join()

    ok = True
    for i in range(workers):
        log_path = Path(args.log_dir) / f"worker-{i}.log"
        print(f"\n######## worker {i} ########", flush=True)
        sys.stdout.write(log_path.read_text(encoding="utf-8", errors="replace"))
    for proc in procs:
        if proc.exitcode != 0:
            ok = False
            print(f"worker process {proc.pid} exited with {proc.exitcode}")
    total = {k: sum(s[k] for s in summaries) for k in ("run", "failures", "errors", "skipped")}
    failed = sorted(m for s in summaries for m in s["failed"])
    print(
        f"\nRan {total['run']} tests from {len(modules)} modules in {time.monotonic() - started:.1f}s "
        f"with {workers} workers: failures={total['failures']} errors={total['errors']} skipped={total['skipped']}"
    )
    if failed:
        print("Failing modules: " + ", ".join(failed))
    if total["failures"] or total["errors"] or len(summaries) != workers:
        ok = False
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
