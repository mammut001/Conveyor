---
name: Feature request
about: Suggest a change to the runner, bot, or ops surface
title: "feat: "
labels: ["enhancement"]
---

**What you want**
One paragraph. Frame it as a user-visible change, not an internal
refactor.

**Why**
What problem does this solve? What can you do today, and what's the
gap?

**Sketch of the change**
Optional. If you have a rough design - which files, which CLI flag,
which command, which smoke to add - drop it here. The maintainer
will iterate with you.

**Scope check**
- Does this change a public command (`/run`, `/fix`, `/memo`,
  `/memory`, `/apply`, `/cancel`, `/status`, `/help`)? If yes, the
  README's command reference needs an update in the same PR.
- Does it add a new top-level script in `scripts/`? If yes, add a
  smoke for it and wire it into the `Makefile` chain.
- Does it touch the runner CLI subcommands (`python -m runner ...`)?
  If yes, check whether the existing `memorize` /
  `classify_memo` / `reclassify_unfiled` contracts still hold and
  update the relevant smoke.

**Out of scope (please don't bundle)**
- V2 items in the README's "Later" section (Hermes-style tool
  calls, API key rotation, inline approval buttons, `/memo_edit`)
  - file separate issues for those.
