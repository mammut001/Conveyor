# Security

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems.

Email the maintainer at the address in their GitHub profile, or open a
private [GitHub Security Advisory](../../security/advisories/new) for
this repository if it is enabled. Either way, include:

* A short description of the issue and the impact you see.
* A proof-of-concept (code snippet, request, or screenshot) that
  reproduces it.
* The commit SHA or release tag you reproduced it against.

You should hear back within a week. We will coordinate a fix and a
release note before any public disclosure.

## Threat model in scope

`Conveyor` runs `codex exec` on a VPS on behalf of a
single whitelisted chat (Telegram user id, Feishu open_id). This is a
**single-operator private control surface**, not multi-tenant SaaS.
The threat model is "Telegram account compromise", "Feishu app-tenant compromise",
and "untrusted chat". Both channels are mitigated by per-channel
`ALLOWED_*` gates and the redaction layer in [`redaction.py`](redaction.py).

Codex jobs intentionally use `danger-full-access` so shell and host reads
work from chat on a personal VPS. Operational boundaries are: channel
allowlist, low-privilege service user, per-day worktree isolation, output
redaction, and explicit `/diff` + `/apply` review before merging into the
main repo. Narrowing the Codex sandbox is future hardening — not current
behavior.

Out of scope: the underlying host, the Codex CLI itself, and the
Telegram Bot API. Report those to their respective maintainers.

## Secret handling

* `.env` is git-ignored. Never commit a real token. If one is leaked,
  rotate it and rewrite history (`git filter-repo`).
* `.env.example` and `.env.test` exist to template the variables and
  must contain placeholders only.
* The bot token, the OpenAI key, and the MiniMax key have full codex
  access. Treat any of them as production-grade credentials.
