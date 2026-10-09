# Web Primary Interface

Conveyor has two complementary operator surfaces:

- **Telegram / Feishu — quick control.** Start or check work from anywhere, ask short questions, cancel jobs, receive status, and handle lightweight approvals.
- **Web Workbench — serious work.** Stay with a session, watch execution, refine an active worktree, inspect changed files and diffs, and make Apply / Discard decisions with enough context.

The channels are not competing clients. They are two distances from the same control plane.

```text
Phone
Telegram / Feishu
    │
    │ quick control
    ▼
 Conveyor
    ▲
    │ serious work
    │
Web Workbench
```

## Workbench information hierarchy

The desktop interface should answer these questions in order:

1. **What am I working on?** — current session and its conversation.
2. **What is Conveyor doing right now?** — queued/running state and live tool execution.
3. **What changed?** — the active worktree, changed files, and cumulative diff.
4. **What decision is next?** — refine again, Apply, Discard, Cancel, or approve a scoped action.
5. **What machine is doing the work?** — runtime owner, optional host screen, nodes, and system health.

Operational telemetry remains available, but it should not visually compete with the code-change decision.

## Phase 1 — desktop workbench hierarchy

This phase intentionally reuses the existing API and safety model.

- Add a compact product-role bar that identifies the browser as the **Web Workbench** and Telegram / Feishu as quick-control surfaces.
- Keep the familiar three-column model, but reduce the visual weight of session navigation.
- Make the conversation and live execution stream the central working surface.
- Promote the **Changes** section to the first inspector card on desktop.
- Keep active-worktree context visible while the operator continues refinement.
- Increase the usable diff viewport and keep Apply / Discard immediately adjacent to the review surface.
- Treat the composer as the workbench command deck rather than a generic footer input.
- Preserve the existing stacked/mobile layout for small screens.

## Phase 2 — session-native workbench state

The next step is functional rather than cosmetic:

- expose active refinement/worktree state directly on session responses instead of deriving it from the currently selected job event stream;
- add explicit per-session run history and allow switching jobs without losing session context;
- add inspector tabs (Changes / Runs / Host / System) once session-level state is authoritative;
- make changed-file and diff navigation file-aware rather than unified-diff-only;
- add keyboard navigation for serious desktop use;
- allow deep links to a session, job, and changed file.

This avoids turning the Web Workbench into a separate execution model: Telegram, Feishu, and Web continue to share the same queue, session identity, worktrees, approvals, and audit trail.

## Phase 2a — session-native refinement history (this PR)

The queue and worktree lifecycle were already implemented. This phase makes
the existing control-plane state authoritative in the Web Workbench without
introducing another background agent, sandbox, or Apply/Discard path.

- `GET /api/sessions` includes a small `active_refinement` summary per
  visible session. The lookup is batched against the existing SQLite
  `session_worktrees` table and includes **no absolute worktree paths**.
- `GET /api/sessions/<id>` includes the same current summary and recent
  `runs` (up to 200). A run's ID, state, mode, timestamps and redacted prompt
  preview are safe for display; logs and raw prompts are not returned by this
  navigation projection. Session identity includes channel, operator and chat,
  never the chat ID alone. Legacy ambiguous raw IDs are refused.
- The Web Workbench's Runs inspector switches the selected runtime job while
  keeping the session transcript and active refinement context. Its Changes
  inspector resolves cumulative files/diff and Apply/Discard through the
  **latest** queue job for the active chain, even when a historical run is open.
- Once Apply/Discard closes the SQLite chain, the active badge disappears
  regardless of old job metadata or the selected run.
- Session polling invalidates stale fetch responses and explicitly clears
  old session details on selection. API authentication and scoped approval
  checks are unchanged.

### Boundaries

This is session-native visibility and history, **not** autonomous multi-step
planning. The existing queue still schedules each explicit refinement turn.
It does not auto-run failed checks, auto-apply changes or bypass operator
approvals. Long histories are capped at 200 recent runs; full pagination,
per-file diff navigation, deep links, and keyboard shortcuts remain follow-ups.

### Smoke review

Create two Web refinement turns in one session, then create an unrelated
Telegram session with the same source chat ID. Open the Web session, select
turn 1 in Runs and confirm the active chain still shows turn 2 and its
cumulative Changes. Apply or Discard after approval; confirm the badge closes,
history remains, and the other session never appears in the run list.
