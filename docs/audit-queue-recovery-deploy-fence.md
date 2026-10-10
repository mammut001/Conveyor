# Queue ownership and atomic deploy fence

# Queue ownership and deploy drain

- Every dequeued job stores its owning process PID and Linux boot/start-tick identity in the queue database.
- On startup, recover **only** running jobs whose owner is verifiably dead. A live Telegram/Feishu/Web owner is never interrupted merely because another service started.
- Missing legacy process ownership or unreadable /proc is **UNKNOWN** and remains running. The operator must stop services, inspect the job and reconcile legacy rows manually. Automatic interruption of unknown jobs is intentionally forbidden.
- Deploy fence is a compare-and-set token in `queue_metadata`, created with `BEGIN IMMEDIATE` only when queued and running counts are zero. Both enqueue and dequeue transactions check that same key; jobs submitted during deployment receive a retry message rather than being stranded in a queue with no wakeup after thaw.
- `deploy_vps.sh` holds the fence across source cutover, smoke and restarts, then thaws on success or failure. The owner token is saved as a mode-0600 `.deploy-fence-token` recovery file until thaw succeeds; it survives cutover cleanup. Never print the token in logs.
- **First deployment limitation:** The live version must already have the new dequeue fence and `deploy_db freeze` support. When upgrading from an older release, automated deployment intentionally fails before cutover. First migrate during an explicit controlled maintenance window with all workers stopped, and inspect the queue. Do not bypass the check on a running installation.
- On systems without Linux /proc, ownership is unknown: no speculative auto-recovery. macOS remains safe but needs explicit job reconciliation after a crash.
- CI smoke simulates a live owner, verified dead owner and cross-process freeze. Live multi-service VPS rollout remains NOT RUN.

Test: `python -m unittest tests.test_queue_recovery_fence -v`, `make smoke`, GitHub Backend CI.

Operational caveat: this fence coordinates **Conveyor's SQLite scheduler**, not arbitrary commands that bypass the scheduler or processes running an old binary. Keep the initial migration maintenance-only.

## Controlled first migration and crash recovery

- The **live** `deploy_vps.sh` (not the candidate) is what GitHub Actions executes. The workflow now checks the installed helper, queue and deploy script support `deploy_fence` before invoking the deploy; legacy installations fail closed.
- Set GitHub Actions secret `VPS_SSH_KNOWN_HOSTS` to a verified `known_hosts` line obtained **out of band** (host key fingerprint checked via VPS console/provider). The workflow intentionally no longer uses unauthenticated `ssh-keyscan`.
- First migration must run in a maintenance window: disable automatic deployment, stop all execution services, verify no remaining Codex child processes, make a SQLite backup, and install/verify the new fence-aware queue and helper before allowing service restart. Do not mix new and old running queue binaries.
- If a deploy process receives SIGKILL or the host reboots, the DB may remain fenced. Confirm **no deploy process** holds `.deploy.lock` and **no running queue job or child subprocess** remains; inspect `.deploy-fence-token` permissions. Under the actual service user invoke `.venv/bin/python scripts/deploy_db.py thaw "$(cat .deploy-fence-token)"` only after those checks, then remove the file. Never force-delete `queue_metadata.deploy_fence` while workers run. If the file is missing, stop services and use a reviewed SQLite recovery procedure.
- Freeze is not a generic kill switch: manual processes and legacy runtimes bypass it. The automation preflight intentionally blocks an older VPS release rather than providing a false atomicity guarantee.
