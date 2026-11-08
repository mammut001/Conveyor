# Pi backend (`CONVEYOR_AGENT_BACKEND=pi`)

Conveyor can run agent jobs with the [`pi` coding agent](https://www.npmjs.com/package/@mariozechner/pi-coding-agent)
instead of the Codex CLI. Jobs keep the same queue, worktree, progress and `/diff` / `/apply` lifecycle.

```text
CONVEYOR_AGENT_BACKEND=pi
CONVEYOR_PI_PROVIDER=stepfun          # provider name as pi knows it
CONVEYOR_PI_MODEL=step-5-preview      # omit to let pi pick the provider default
STEPFUN_API_KEY=...                   # the key pi's models.json names for that provider
# CONVEYOR_PI_API_KEY_ENV=MY_KEY      # only if the key lives in a differently named variable
# CONVEYOR_PI_BIN=/usr/bin/pi
```

## Provider and key

pi reads providers from `<CODEX_TASK_ROOT>/pi-agent/models.json` (Conveyor sets `PI_CODING_AGENT_DIR`). For an
OpenAI-compatible provider such as StepFun:

```json
{"providers": {"stepfun": {"baseUrl": "https://api.stepfun.com/step_plan/v1", "api": "openai-completions",
  "apiKey": "STEPFUN_API_KEY", "models": [{"id": "step-5-preview", "reasoning": true, "contextWindow": 1024000}]}}}
```

Conveyor hands pi **only the selected provider's key** (`deepseek` → `DEEPSEEK_API_KEY`, `stepfun` →
`STEPFUN_API_KEY`, `openai` → `OPENAI_API_KEY`, …; unknown provider `x-y` → `X_Y_API_KEY`), from the
environment or the Web Console secret store. This works with credential isolation on. `scripts/doctor.py`
checks the pi binary, `models.json` (provider and model declared) and that the key is set.

Node ignores `SSL_CERT_FILE`; behind a TLS-intercepting proxy set `NODE_EXTRA_CA_CERTS`, which is passed
through to pi.

## Safety

Pi has **no sandbox**: RUN jobs work in the real workspace with full bash/write access. Requests that look
destructive (deletions, force pushes, hard resets, stopping services, `chmod -R`, `mkfs`, …) go through the
approval step on every path — plain messages, `/run`, `/fix`, `/deep` and image captions. Approval is a flag on
the job set by the approval step; text in a message that claims approval is ignored. The detector is a
word/command list, not a guarantee: keep the owner allowlist tight.

## What the chat shows

Reasoning models stream hidden reasoning for a while before any text: after a second of that the placeholder
shows `💭 thinking...`, and a long silent tool call is pulsed as `🔧 <tool> (Ns)...`. Token usage is
recorded in `job.json`. Provider failures (quota, auth, unknown model, rate limit) are explained in Chinese
with the provider's raw message kept.

## Chat tier with reasoning models

The chat tier (`CONVEYOR_CHAT_*`) bills hidden reasoning against `CONVEYOR_CHAT_MAX_TOKENS`. With StepFun
`step-3.7-flash` set `CONVEYOR_CHAT_REASONING_EFFORT=low`; without it an empty, budget-exhausted reply is
retried once with a wider budget.
