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
