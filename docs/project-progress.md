# Project progress queries

Use `/project_progress mammut001/Conveyor` or ask `Conveyor 最近进展如何` in Web, Telegram or Feishu. `/project_progress` uses your active saved project, then the configured default repository when no saved project exists. A saved project ID or exact name is also accepted by the command. An explicit unknown name never falls back to another repository. If multiple saved names match, use an ID or owner/repo.

Set `GITHUB_DEFAULT_REPO=mammut001/Conveyor` to resolve the short name Conveyor without a saved project profile. Public repositories can be queried anonymously. For private repositories or higher API limits, set `GITHUB_TOKEN` with read access to the intended repository.

The report shows at most five open PRs, merged PRs from the twenty most recently updated closed PRs, five recent CI runs across branches, and up to five issues from ten recently updated issue records. These are bounded snapshots, not totals or completion percentages. CI records include their branch; merged changes do not prove deployment. Every section has source links and the report has a UTC query timestamp. An unavailable section is explicitly marked unknown.

Reads run concurrently outside the chat event loop, with the GitHub client's 15-second per-request timeout. Progress queries use a read-only tool rather than starting Pi. Existing tool permissions and channel authorization still apply.

This feature answers on request. It does not create a recurring monitor or send notifications.
