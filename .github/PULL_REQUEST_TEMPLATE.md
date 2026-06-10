## What

One or two sentences. Reference the issue this closes with
`Closes #N` if there is one.

## Why

The motivation. Not the implementation - the *reason* someone would
want this change merged.

## Smoke baseline

Output of `make smoke` on a clean checkout, in a fenced block:

```text
>>> scripts/auto_maintain_smoke.py
... (pass) ...
make smoke: 6/7 env-free smokes green
(1 red: compress_day_smoke.py - documented pre-existing hour-gate
bug, see project.md "Honest gaps"; not introduced by this PR)
```

If you introduce a new smoke, add it to the `SMOKE_FREE` list in
the `Makefile`.

## Blast radius

- [ ] Bot commands (`/run`, `/fix`, `/memo`, `/memory`, `/apply`)
- [ ] Runner CLI subcommands (`python -m runner ...`)
- [ ] MEMORY.md section layout or auto-timestamp rules
- [ ] Worktree layout (`worktrees/day-YYYY-MM-DD/`)
- [ ] systemd unit paths
- [ ] `.env.example` keys

Check all that apply. If you checked any of these, the PR should
update README.md and/or CHANGELOG.md in the same diff.

## Self-review

- [ ] `git grep -nE "203\\.0\\.113\\.42|203\\.0\\.113\\.43|/Users/example"` returns nothing
- [ ] No `.env`, `.env.test`, or secrets in the diff
- [ ] Commit message explains the *why*, not just the *what*
- [ ] No drive-by refactors unrelated to the PR
