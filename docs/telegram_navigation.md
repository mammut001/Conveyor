# Telegram Workers and session keyboard compatibility (PR #104)

Conveyor's **Web Workbench** remains the authoritative multi-session workspace.
Telegram is the **quick-control surface**, not another independent worktree router.

## Symptoms addressed

Old Telegram reply keyboards may show two labels even when the current bot
version has no handlers: **👷 我的 Workers** and **🔄 切换会话**.
Telegram reply-keyboard taps arrive as normal text, so without an exact-match
handler both go to the conversational model. Similarly, `/workers` was absent
from both the Telegram command menu and the dispatcher.

## Current behavior

- `/workers` and **👷 我的 Workers** show real queue counts for the
  authenticated operator's current physical Telegram chat. If named Agents
  are enabled, show existing agent names with safe read-only detail cards.
  Disabled named Agents are shown honestly, rather than fabricated.
- `/sessions`, `/switch`, and **🔄 切换会话** open this Telegram
  conversation's recent queue-job navigation. Selecting a job gives the
  last known state, mode and redacted prompt preview.
- Menu commands are registered through Telegram `set_my_commands` on bot
  startup. Stale reply-keyboard text is intercepted even before onboarding
  nudges; the rest of normal conversation remains untouched.
- `tgn:` callbacks are read-only, narrowly parsed and re-resolve their
  target under physical channel/chat/operator scope. They cannot be reused
  to access another group, operator, Agent instructions or worktree path.
- No callback silently starts a job, changes its worktree, grants approval,
  creates/archives a session, or switches Telegram into a Web Agent lane.

**Important limitation:** "Switch sessions" on the old keyboard is retained
for compatibility but is **view navigation only**. It does not switch the
execution context of future Telegram messages. Actual per-Agent conversation
switching and multi-turn worktree refinement remain in Web. A future feature
would need an explicit routing/approval design before enabling Telegram
cross-Agent execution.

## Checks

```bash
python -m unittest tests.test_telegram_navigation -v
python -m unittest discover -s tests -v
make smoke
```

CI can test local routing/card rendering and callback scoping; it cannot
verify Telegram's persisted live keyboard or bot-menu configuration remotely.

After approval, on a controlled VPS deployment:

1. Verify current deployed commit and take a configuration backup.
2. Deploy without modifying other PRs, then restart **only** the Telegram
   service during an agreed maintenance window.
3. In the allowed operator's private chat, tap both **old** reply-keyboard
   buttons and run `/workers`, `/sessions`, `/switch` and `/help`.
4. Confirm real inline cards appear and can navigate back, no LLM response
   occurs, and unauthorized users/groups cannot inspect private state.
5. Confirm the BotFather/Telegram command menu refresh includes the commands
   after bot startup; Telegram clients may cache command lists.
6. Ensure ordinary chat, `/run`, `/fix`, tool/relay approval callbacks, and
   Web sessions still work; rollback if any regression occurs.
