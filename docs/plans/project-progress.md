# Project progress iteration

Provide a read-only project progress query backed by GitHub facts, available through the existing personal tool registry and chat commands. Accept an explicit owner/repo, a saved project ID/name scoped to the operator, or the active project. Never silently substitute the default repository for an invalid explicit selection.

The report should include recent merged PRs, open PRs (draft/review state), recent CI outcomes and open issues, with source links and a checked-at timestamp. Do not invent completion percentages or infer successful deployment from a merge. Partial provider failures must remain visible. Run blocking API reads outside the async chat event loop, with bounded concurrency and request timeouts.

Integrate the query into Pi Web routing as well as ordinary tool/command routing, so a progress question does not need a coding-agent process. Preserve existing desktop and destructive-action routing.

Validation: repository selection and operator isolation; complete and partial API responses; CI failure reporting; command/intent routing; event-loop responsiveness; targeted regressions; live read-only report for a selected project. Deliver through a reviewed branch/PR, pass CI, merge and synchronize the server following the established workflow.

Recurring subscriptions are a separate capability: do not claim monitoring or create notifications for a one-time progress query.
