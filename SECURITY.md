# Security Policy

This document outlines the security architecture, threat model, configuration limits, and reporting guidelines for Conveyor.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security issues or vulnerabilities.

Email the maintainer at the address in their GitHub profile, or open a private [GitHub Security Advisory](../../security/advisories/new) for this repository if enabled. Please include:
* A short description of the issue and its potential impact.
* A proof-of-concept (code snippet, request, or screenshot) to reproduce the issue.
* The commit SHA or release tag you reproduced it against.

You should hear back within a week. We will coordinate a fix and a release note before any public disclosure.

---

## Threat Model & Trust Boundaries

`Conveyor` runs `codex exec` on a VPS on behalf of a single whitelisted chat (identified by Telegram user ID or Feishu open_id). This is a **single-operator private control surface**, not a multi-tenant SaaS application.

### VPS Shell Equivalence
**IMPORTANT:** The Telegram/Feishu operator account represents VPS shell equivalence. An operator who can issue `/run <prompt>` commands essentially holds root-equivalent execution rights on the VPS under the configured service user (e.g., `ubuntu`). Codex execution jobs intentionally run with the `danger-full-access` sandbox mode so that shell execution and direct host reads/writes function properly. Access controls must be documented and treated as VPS root shell credentials.

### Mitigation Layers
1. **Channel Allowlists:** Access is gated by strict allowlists (`TELEGRAM_ALLOWED_USER_ID`, `CONVEYOR_FEISHU_ALLOWED_USERS`).
2. **Output Redaction:** The redaction layer in `redaction.py` strips API tokens, credentials, and sensitive patterns from logs, stdout, stderr, and exception tracebacks before they return to the operator.
3. **Low-Privilege User:** The service runs under a dedicated, low-privilege system user.
4. **Worktree Isolation:** Codex execution occurs inside temporary per-day or per-job Git worktrees rather than modifying the main repository directly.
5. **Apply Gate:** Merging changes back into the main repository requires explicit `/diff` and `/apply` commands.

---

## Child Process Environment Sanitization

To prevent child subprocesses spawned by Codex from accessing sensitive application credentials (such as Lark app secrets, Telegram bot tokens, or email passwords), the execution environment is strictly sanitized:

* **Denylist:** The variables `TELEGRAM_BOT_TOKEN`, `LARK_APP_SECRET`, `GMAIL_APP_PASSWORD`, `GITHUB_TOKEN`, `CONVEYOR_DESKTOP_AGENT_TOKEN`, and `WEB_SEARCH_API_KEY`, as well as any variables starting with `GOOGLE_`, are stripped from the environment.
* **Allowlist:** Only essential system variables (e.g., `HOME`, `PATH`, `USER`) and configured LLM provider variables (e.g., `OPENAI_API_KEY`, `MINIMAX_API_KEY`, `ANTHROPIC_API_KEY`) are allowed by default.
* **Custom Prefixes:** Advanced users can explicitly allow additional environment prefixes using `CONVEYOR_CHILD_ENV_PREFIXES` or `CONVEYOR_CHILD_ENV_EXTRA_PREFIXES`. Any matching key will bypass the denylist.
* **Security Auditing:** By default, `CONVEYOR_CHILD_ENV_AUDIT=true` logs the count and names of stripped sensitive variables (values are never logged).

---

## Job & Worktree Quotas

To prevent disk space exhaustion or queue flooding on the VPS, Conveyor enforces the following resource quotas:

* **Active Worktree Size Limit:** The total size of active worktree files (located under the `worktrees/` directory) is measured before starting any job. If it exceeds `CONVEYOR_MAX_WORKTREES_BYTES` (default: 500MB), the job is refused.
* **Queue Size Limit:** The job queue holds a maximum of `CONVEYOR_MAX_PENDING_JOBS` (default: 20) pending jobs. Any submission beyond this limit is blocked.
* **Rate Limiting:** Operators are restricted to a maximum of `CONVEYOR_MAX_JOBS_PER_HOUR` (default: 60) job submissions.

---

## Apply Safety Policy

When applying worktree changes back to the main repository, Conveyor validates all paths to protect sensitive configuration files and block untracked file abuses:

* **High-Risk File Blocks:** Any modification to high-risk files (such as `requirements.txt`, `setup.py`, `redaction.py`, files inside the `security/` directory, systemd configurations, or deployment scripts) blocks `/apply` unless `CONVEYOR_APPLY_ALLOW_HIGH_RISK=true` is explicitly set in settings.
* **Untracked Bytes Limit:** The total size of all untracked files being applied in a single batch is limited to `CONVEYOR_APPLY_MAX_UNTRACKED_BYTES` (default: 1MB).
* **Always Denied:** Certain files, such as `.env` files, private keys (`*.pem`, `*.key`), and files containing the words `token`, `secret`, or `password`, are always blocked and can never be applied.
