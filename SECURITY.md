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

`telegram_codex_runner` runs `codex exec` on a VPS on behalf of a
single whitelisted Telegram chat. The threat model is "Telegram
account compromise" and "untrusted Telegram chat". Both are mitigated
by `TELEGRAM_ALLOWED_USER_ID` and the redaction layer in
[`redaction.py`](redaction.py).

Out of scope: the underlying host, the Codex CLI itself, and the
Telegram Bot API. Report those to their respective maintainers.

## Secret handling

* `.env` is git-ignored. Never commit a real token. If one is leaked,
  rotate it and rewrite history (`git filter-repo`).
* `.env.example` and `.env.test` exist to template the variables and
  must contain placeholders only.
* The bot token, the OpenAI key, and the MiniMax key have full codex
  access. Treat any of them as production-grade credentials.
