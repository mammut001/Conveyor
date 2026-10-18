# Conversation experience release plan

Latest candidate code commit: `2de042b` on `codex/experience-improvements`.
The earlier candidate `2c76393861743a69f1f722aa8a65ed5837ae00dd` is superseded
by the requested Agent identity/history-origin improvements. That earlier code
passed the full Linux gates; the latest slice passed 77 backend regressions,
five Agent browser cases, typecheck and build. Validate the final merged
revision before release; do not attribute the earlier full gates to untested
later changes.
Production baseline checked on 2026-10-18:
`8617183efd6f510fb68411e2faff564bae2acf35`.

The operator has been asked to approve production deployment. No approval has
been received. Do not interpret automatic goal continuation as that approval.
No source has been pushed, merged, or deployed.

## Release procedure after approval

1. Publish the candidate through the normal reviewed main-branch path. The VPS
   deploy script accepts only a requested SHA reachable from `origin/main`.
   Record the final merged SHA and validate that revision; a rewritten or
   conflicting merge needs fresh checks for the changed code.
2. Use `scripts/deploy_vps.sh`, the transactional Git deployment path. Do not use
   legacy `scripts/deploy.sh`: it syncs live files before acquiring its lock and
   has only partial file rollback.
3. Recheck production's current SHA, clean tracked checkout and idle queue at
   execution time. The last read-only check found zero queued/running jobs and
   active Telegram, Feishu and Web services; this is not a future idle guarantee.
4. Let the existing deploy script validate a detached candidate, take the
   protected backup, acquire the atomic queue fence, sync dependencies, run the
   production smoke with placeholder configuration and restart its set of
   previously active Conveyor services. The script can restart additional
   Conveyor services/timers besides the three chat services. Do not widen that
   list or enable currently disabled services.
5. Verify the requested SHA, service health and fence release. Any failed
   dependency/smoke/health check must use the script's whole-revision rollback
   to the recorded previous SHA; confirm health again after rollback.

The GitHub deployment workflow currently allows manual dispatch only. Leave
that maintenance-mode setting in place.

## Channel acceptance after cutover

Use the authenticated operator's own test conversation only with their explicit
message-sending authorization. Otherwise have the operator perform these checks:

- “看看任务进度” returns the existing scoped status without a model call.
- “查看帮助” returns the short guide; `/commands` retains the full inventory.
- A multi-tool request shows read/answer progress and does not reset its model
  waiting allowance every round.
- Follow up within the same Agent, then exercise `/deep` and “清空当前对话”.
  Confirm another Agent's history remains separate.
- Record time to initial acknowledgement, first useful text and final result
  separately. Compare a real problematic request, rather than treating the
  1.29-second synthetic provider probe as channel performance evidence.

The goal remains unproven until real channel behavior has been checked and the
operator can assess the experience. Local/Linux tests do not establish this.
