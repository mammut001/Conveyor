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
