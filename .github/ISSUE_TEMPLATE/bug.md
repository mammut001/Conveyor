---
name: Bug report
about: Something in the runner / bot / smokes is broken
title: "bug: "
labels: ["bug"]
---

**What happened**
A clear, one-paragraph description of the bug.

**Repro**
Steps to reproduce, ideally copy-paste runnable. For bot issues this
usually means: which command (`/run`, `/fix`, `/memo`, etc.), what
prompt, what the bot replied with.

**Expected**
What you expected to happen.

**Smoke baseline**
Output of `make smoke` on a clean checkout. If you can't run it
(e.g. you hit the bug on the live VPS), say so and paste the last
few lines of the relevant `journalctl -u conveyor-telegram-bot`
output.

**Environment**
- Commit SHA: `git rev-parse HEAD`
- Branch: `git rev-parse --abbrev-ref HEAD`
- OS / Python: `python --version`
- codex CLI version: `codex --version` (if available)

**Notes**
Anything else that might help - related issues, partial workarounds,
why you think it's happening.
