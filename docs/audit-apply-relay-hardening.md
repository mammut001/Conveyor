# Apply failure recovery and relay outage handling

# Apply and relay failure safety

- Apply preflights patch applicability, target collisions, source/target parent containment and untracked file lists before the first mutation.
- Newly copied untracked files are tracked and removed if a copy fails partway through. If later finalization fails, only files still matching the copied content are removed.
- If an untracked copy or finalization fails after the tracked patch was applied, Conveyor attempts a **reverse patch**, not a broad `git reset --hard`, so unrelated user modifications are not overwritten by a global reset.
- If reversal or new-file cleanup cannot be guaranteed, Apply reports **manual review required** instead of success. The refinement chain is not marked applied.
- With Approval Relay enabled, SQLite exceptions, missing records, and expired approvals block dangerous-tool execution. The local action remains pending on temporary unavailability and can be retried; a real conflicting/expired decision is final.
- Relay disabled retains the established local confirmation behavior.

Important boundary: OS-level sandbox isolation is separate work; `danger-full-access` still permits an agent subprocess to reach host paths. This PR does not claim a perfectly atomic multi-file filesystem transaction under external concurrent writes.

Test: `python -m unittest tests.test_atomic_apply_relay -v`, existing Apply/Approval tests, `make smoke` and GitHub Backend CI. Live production fault injection remains NOT RUN.

## Reviewer follow-up hardening

- Untracked file creation uses exclusive mode (O_EXCL semantics), never `copy2` over a race-created target. Partial writes are tracked and removed; pre-existing or competing files are preserved.
- Cancellation during `git apply` terminates/reaps the child process; the caller compensates a possibly-applied patch and re-raises `CancelledError`. If reverse compensation fails, a critical diagnostic marks the worktree for manual inspection.
- Injected tests cover last-moment target creation, post-copy DB close failure, reverse-patch failure and cancellation after tracked modification. All production changes still require the existing Apply approval flow.

Caveat: user-controlled concurrent changes to ancestor directories, process-level termination with SIGKILL, and loss of power cannot be made fully transactional using plain `git apply` plus ordinary filesystem copies. Those require OS-level isolation/journaling and manual recovery guarantees rather than a claim of strict global atomicity.
