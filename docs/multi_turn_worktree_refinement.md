# Multi-turn Worktree Refinement

Conveyor keeps conversation continuity and execution continuity separate.
A durable chat/session can contain many independent queue/runtime jobs, while
an **active refinement chain** lets those jobs intentionally share one detached
Git worktree until the operator applies or discards the accumulated changes.

```text
Session
  └─ active refinement chain
       ├─ queue/runtime job A ─┐
       ├─ queue/runtime job B ─┼─ one shared detached worktree
       └─ queue/runtime job C ─┘

Apply / Discard → closes the chain
next execution   → creates a new chain/worktree
```

## Identity and persistence

The chain key is Conveyor's stable session identity:
`channel:operator_id:source_chat_id`. A bare chat ID is never sufficient, so
Telegram, Feishu and Web sessions cannot collide merely because an upstream ID
has the same text.

Control state is stored in the existing control-plane SQLite database
`codex_memory_root/state/job_queue.sqlite3`:

- `queued_jobs.session_id` records the stable session identity.
- `queued_jobs.refinement_intent` records whether a queued job expects to
  continue the session's active/pending chain.
- `session_worktrees` stores the explicit session → active worktree binding,
  root/latest queue/runtime IDs, turn count, timestamps, and the lifecycle
  state (`active`, `applied`, `discarded`, `stale`).

The migration is additive and idempotent. Historical worktrees are not guessed
or auto-attached; only bindings created by this feature are reusable.

## Lifecycle

### First execution

A normal independent queue/runtime job is created. The runner creates the
usual detached worktree. Only after `git worktree add` succeeds is the stable
session bound to that path as an active chain. A creation failure therefore
cannot leave a fake active binding.

### Follow-up execution

Every follow-up remains a new queue/runtime job with its own ID, events, logs,
provider handling, cancellation and final answer. At runtime, Conveyor resolves
the current active chain and reuses its exact worktree as the Codex cwd.

The queue persists refinement intent, not merely a filesystem path. This is
important when job B is submitted while job A is still running: B can wait in
the persistent queue before A has created the worktree, then resolve the active
binding when B actually starts.

If a queued continuation starts after the chain was applied, discarded, lost,
or otherwise closed, it fails closed. Conveyor does **not** silently turn that
explicit continuation into a fresh independent worktree.

### Apply

Apply keeps the existing dirty-main guard, apply lock, path policy,
tracked/untracked validation, binary/symlink/size checks and TOCTOU rechecks.
Because every refinement job points at the same worktree, Apply uses the full
worktree-vs-HEAD accumulated diff, not only the latest turn's edits.

After a successful Apply, the chain becomes `applied`. Apply still means
"write the validated changes into the main workspace"; it does not create a
Git commit.

### Discard

Discard closes the active chain as `discarded` and removes the shared worktree.
A queued continuation that was already marked as refinement intent will then
fail closed when it reaches the runner.

### Cancel and failure

Cancelling or failing one refinement job does not remove the chain by default.
Previous successful edits remain available for diff, another refinement turn,
Apply, or Discard.

## Reuse safety

Worktree reuse is validated in the runner/worktree layer rather than trusting a
Web or handler-provided path. The target must:

1. exist and not be a symlink;
2. resolve below Conveyor's configured `codex_task_root/worktrees` directory;
3. be the root of a Git worktree;
4. be registered by `git worktree list` for the configured repository; and
5. share the configured repository's Git common directory.

Missing, deleted, outside-root, unregistered or cross-repository paths are
rejected. A validation failure marks the affected active chain stale rather
than executing in an unsafe cwd.

## Routing

Explicit `/fix`, `/run`, Web Fix and ordinary action requests keep their current
routing. In addition, when the same stable session has an active or pending
refinement chain, a conservative deterministic helper recognizes short edit
feedback such as:

- `右边还是太挤`
- `颜色再淡一点`
- `继续`
- `still too wide`
- `a little darker`
- `move it down a bit`

Question/explanation-shaped messages such as `为什么你刚才这么改？` remain
eligible for the fast chat path. The helper only changes the route; the
execution prompt is still the operator's original input, never generated model
text.

## Deliberate non-goals

This implementation does not depend on Codex thread resume and does not add a
permanent provider process. It also does not add parallel worktree execution,
automatic commits/pushes, a new sandbox model, RBAC, voice, semantic search,
watchers, or unrelated integrations.
