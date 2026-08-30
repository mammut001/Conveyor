# Runtime ownership

Conveyor may expose Telegram, Feishu and Web Console from separate Python processes while sharing one SQLite queue. A live `CodexRunner` is process-local and is never assumed to be reachable from another process.

`runtime_control.py` records the execution owner for a running queue job and provides a small SQLite command mailbox. The Web Console can therefore request cancellation of a job owned by another Conveyor process without pretending that its own `CodexRunner` owns that process.

The mailbox currently accepts only `cancel`. Commands are scoped to both `job_id` and an opaque process owner id, duplicate pending cancels are collapsed, results are redacted and bounded, and polling exists only while a job is actively running. No Redis, broker or permanent worker is introduced.
