"""Opt-in real X11 handoff safety test; planner is deliberately scripted.

Requires a private test root/session as documented in host_desktop.md.
Does not claim to test an AI model or modify a production desktop.
"""
import argparse
import asyncio, json, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts import linux_browser_e2e as qa
from desktop_computer_loop import build_backend, run_computer_loop
from desktop_computer_requests import create_computer_task, get_computer_task
from human_takeover import HumanTakeoverStore
import agents

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--manifest", type=Path, required=True)
args = parser.parse_args()
root = qa._check_root(args.root)
display, authority = qa._load_manifest(args.manifest, root)
os.environ["XAUTHORITY"] = authority
settings = qa._settings(root, root / "bin/codex")
agent_id, chat_id = qa._assign_display(settings, display)
scope = agents.takeover_scope(agent_id)
leases = HumanTakeoverStore(settings)
created = create_computer_task(settings, "Inspect the current test desktop after the human handoff",
    direct_mode=True, max_steps=8, max_seconds=30, operator_id="linux-safety-qa",
    chat_id=chat_id, channel="web")
assert created["ok"], created
task_id = created["task_id"]
backend = build_backend(settings, task_id)
real_execute = backend.execute_step
calls = []

async def audited(*args, **kwargs):
    assert leases.current(scope) is None, "backend invoked during human takeover"
    action = args[3] if len(args) > 3 else kwargs["action"]
    calls.append({"action": action["action"], "at": time.monotonic()})
    return await real_execute(*args, **kwargs)
backend.execute_step = audited

class HandoffPlanner:
    def __init__(self):
        self.count = 0
        self.release_task = None
        self.paused_calls = None
        self.lease_id = None
    async def next_action(self, **kwargs):
        self.count += 1
        if self.count == 1:
            lease = leases.start(scope=scope, reason="operator_requested", ttl_seconds=30)
            self.lease_id = lease["id"]
            async def release():
                await asyncio.sleep(0.8)
                self.paused_calls = list(calls)
                assert self.paused_calls == [], self.paused_calls
                leases.complete(lease["id"])
            self.release_task = asyncio.create_task(release())
            return {"action": "click", "x": 1, "y": 1}
        assert kwargs["observation"].get("screenshot_id"), "no fresh post-handoff image"
        return {"action": "done", "summary": "fresh post-handoff desktop observed"}

async def main():
    planner = HandoffPlanner()
    try:
        result = await run_computer_loop(settings, "Inspect the current test desktop after the human handoff",
            planner=planner, backend=backend, task_id=task_id,
            max_steps=8, max_seconds=30, direct_mode=True)
        if planner.release_task:
            await planner.release_task
    finally:
        if planner.lease_id and leases.current(scope) is not None:
            leases.complete(planner.lease_id)
    assert result["status"] == "done", result
    assert [c["action"] for c in calls] == ["observe"], calls
    task = get_computer_task(settings, task_id)
    assert not any(x["action_type"] in {"click", "type", "hotkey"} for x in task["trajectory"])
    evidence = {"pass": True, "backend": "real isolated X11", "planner": "scripted adversarial handoff",
                "calls_during_takeover": len(planner.paused_calls),
                "post_handoff_actions": [c["action"] for c in calls],
                "old_click_executed": False, "fresh_screenshot_id": result["screenshot_id"],
                "task_id": task_id, "display": display}
    (root / "takeover-real.json").write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence))
asyncio.run(main())
