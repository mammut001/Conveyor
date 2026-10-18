# Conversation experience release plan

Latest candidate code commit: `150ccd0` on `codex/experience-improvements`.
This supersedes the earlier single-conversation candidates: the operator chose
independent entry histories with explicit continuation. Final evidence is in
`docs/testing/independent-conversations.md`: 1,308 Linux tests (93 skipped),
75 smoke scripts, 16 Web browser cases, 18 chat interaction groups and Web build
passed for the backend candidate `f991bdd`. The UI follow-up through `150ccd0`
adds compact Agent cards, a collapsible new-topic form and responsive navigation.
Its typecheck/build and all 16 Web browser cases passed. Validate the final
merged revision before release.
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

Additional channel acceptance for the independent-history release:

- Start with the same Agent in Telegram and Feishu. Distinct entry conversations
  must not know one another's newly supplied test phrases by default.
- Explicitly choose a listed conversation from the other entry, then verify
  continuity and source labeling. Return to the local root and confirm its
  earlier context remains separate.
- Create a topic, change Agent and return; the topic selection and its draft
  should be restored. Existing Web conversations and default-entry context must
  remain accessible.
- Do not treat history continuation as permission to adopt another channel's
  identity or control its worktree.

## UI acceptance for the final candidate

The latest mobile preview is a fixture, not evidence of a deployed interface.
At 390px width, check the Agent switcher and conversation selector separately:

- The Agent list is titled Agents; the current Agent has a visible selected state.
- New-topic naming is hidden until “另开话题”; cancel and Escape close the form,
  while Enter submits it. Creating a topic must not erase another topic's draft.
- Global navigation has no clipped title; all sections remain reachable by
  horizontal scrolling and keyboard. The dedicated mobile UI retains its own
  navigation rather than adopting the standard layout's header changes.
- Replying disables conversation changes. Switching to Inbox and back retains
  the pending reply and next draft. Loading failure offers explicit retry.

The next release gate is operator approval for the reviewed publication and
production cutover. Do not send Telegram/Feishu test messages without separate
message-sending authorization.
