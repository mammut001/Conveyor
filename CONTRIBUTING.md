# Contributing

Thanks for your interest in `telegram_codex_runner`.

This project is intentionally small and single-user in origin: the same
person who runs the bot also reviews the diffs. That means a few things
for new contributors.

## What this project is

A small Python service that bridges a whitelisted Telegram user to the
[`codex`](https://github.com/openai/codex) CLI on a VPS. It is a tool for
running `codex exec` from a phone, not a multi-tenant SaaS. Read
[`README.md`](README.md) for the command surface and
[`project.md`](project.md) for the deep design notes.

## Ground rules

* **Keep the ALLOWED_USER_ID gate honest.** The whitelist is the only
  thing standing between this bot and the public internet. Do not add
  "support for multiple users" without an explicit threat model.
* **Never commit secrets.** `.env` is git-ignored. `.env.example` and
  `.env.test` are the only env-shaped files in the repo, and both use
  placeholders. If you find a real token in a commit, rotate it.
* **One job at a time.** The runner serializes Codex invocations by
  design. Do not add concurrent-job paths without explaining why the
  serialization guarantee can be relaxed.

## Development loop

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
make smoke        # env-free AST/behavior smokes, runs in <30s, used as CI gate
make smoke-all    # also runs memo_smoke, which needs a real .env
```

`make smoke` is the pre-deploy gate. PRs that break it will not be merged.

## Commit and PR style

* Imperative-mood subject line, ~50 chars (`runner: gate jobs by chat id`).
* One bounded change per commit. The CHANGELOG and `project.md` are kept
  in sync with the code on `main`.
* Reference the `scripts/<name>.py` path or `bot.py:<line>` in the body
  when the change is non-obvious.
* No `Co-Authored-By` trailers for AI assistants on a contributor's
  behalf. Attribute your own work.

## Filing issues

Bug reports and feature requests are welcome. For security issues, see
[`SECURITY.md`](SECURITY.md) instead of opening a public issue.

## License

By contributing, you agree that your contributions are licensed under
the project's [MIT License](LICENSE).
