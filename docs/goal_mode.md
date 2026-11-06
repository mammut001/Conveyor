# Goal mode (`/goal`) — unattended iteration for the Coder agent

A normal Coder job answers once and stops. `/goal` wraps the job queue in a
durable harness so you can set an objective, go to sleep, and find a reviewed
branch in the morning.

```text
/goal 把手机端 Coder 聊天做到稳定可用，找并修复你能发现的问题 --hours 8 --max 60 --check "cd web && npm run build" --check "python -m unittest tests.test_web_chat"
/goal status
/goal stop
```

Works in the web console chat (any agent conversation, including Coder) and in
Telegram (`/goal`, also inside a forum topic).

## What the harness does

Each iteration is an ordinary `FIX` job in the conversation's own worktree
chain, so every iteration continues in the same isolated worktree:

1. build a prompt: objective, the checks, the last verification result, the
   agent's own "next step", and the working rules;
2. run the job through the normal queue (limits, lanes, rate limit still apply);
3. **run the `--check` commands itself** inside that worktree. The agent's claim
   of success is never trusted;
4. fingerprint the worktree (HEAD, dirty state, diff, check results) to
   measure progress;
5. decide: continue, finish, or stop.

## Stop conditions

| Reason | Trigger |
| --- | --- |
| done | all `--check` commands pass **and** the agent reports `GOAL_STATUS: done`. With no checks, `done` must be reported on two consecutive iterations |
| stalled | 3 iterations in a row with no change in the worktree or check results |
| failed | 3 consecutive failed iterations (provider crash, timeout) |
| blocked | the agent reports `GOAL_STATUS: blocked` (needs a key, a decision, ...) |
| timeout | `--hours` elapsed (default 8, max 24) |
| max_iterations | `--max` reached (default 60, max 200) |
| stopped | `/goal stop` (the running job is cancelled) |

A rate limit or a full queue pauses the loop for two minutes and does not
count as an iteration. An iteration that runs longer than two hours is
cancelled and counted as a failure.

## Safety

* Iterations are normal jobs: **nothing is merged, pushed or deployed**. The
  agent is told to work on branch `goal/<id>` and commit each iteration. In the
  morning review with 改动 / `/diff`, then `/apply` or `/discard`.
* `--check` commands come from your own message (owner-only channels), run with
  the utility environment (no provider keys), a 15 minute limit each, and only
  inside a directory under `<task root>/worktrees`.
* One active goal per conversation.

## Durability

State lives in `<memory root>/goals/<id>.json` and is saved after every step.
When the web service or the Telegram bot restarts (for example a deploy) the
loops that were running are resumed. A job that was in flight during the
restart is simply re-issued as the next iteration.
