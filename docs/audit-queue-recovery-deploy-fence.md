# Queue ownership and atomic deploy fence

# Queue ownership and deploy drain

- Every dequeued job stores its owning process PID and Linux boot/start-tick identity in the queue database.
- On startup, recover **only** running jobs whose owner is verifiably dead. A live Telegram/Feishu/Web owner is never interrupted merely because another service started.
- Missing legacy process ownership or unreadable /proc is **UNKNOWN** and remains running. The operator must stop services, inspect the job and reconcile legacy rows manually. Automatic interruption of unknown jobs is intentionally forbidden.
- Deploy fence is a compare-and-set token in `queue_metadata`, created with `BEGIN IMMEDIATE` only when queued and running counts are zero. Every dequeue transaction checks that same key. New tasks can be enqueued while fenced, but will not start until thaw.
- `deploy_vps.sh` holds the fence across source cutover, smoke and restarts, then thaws on success or failure. A wrong token can never thaw someone else's deployment.
- **First deployment limitation:** The live version must already have the new dequeue fence and `deploy_db freeze` support. When upgrading from an older release, automated deployment intentionally fails before cutover. First migrate during an explicit controlled maintenance window with all workers stopped, and inspect the queue. Do not bypass the check on a running installation.
- On systems without Linux /proc, ownership is unknown: no speculative auto-recovery. macOS remains safe but needs explicit job reconciliation after a crash.
- CI smoke simulates a live owner, verified dead owner and cross-process freeze. Live multi-service VPS rollout remains NOT RUN.

Test: `python -m unittest tests.test_queue_recovery_fence -v`, `make smoke`, GitHub Backend CI.

Operational caveat: this fence coordinates **Conveyor's SQLite scheduler**, not arbitrary commands that bypass the scheduler or processes running an old binary. Keep the initial migration maintenance-only.
