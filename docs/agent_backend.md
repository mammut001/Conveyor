# Agent backend boundary

`runner/backend.py` introduces a small provider-neutral protocol around the existing execution surface. `CodexBackend` is an adapter over the current `CodexRunner`; it does not change Codex authentication, worktree behavior, streaming, apply/discard policy, or queue semantics.

The purpose is to make future providers explicit rather than letting Web/channel code grow provider-specific branches. The default and only production backend at this stage remains Codex.
