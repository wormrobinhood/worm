# Operational recovery and trading evidence

## Local hardening changes, September 23, 2026

Trading remains disabled by default and the live-sell release gate remains false. These changes do not authorize deployment, signing or live trading.

### Health and storage

`/healthz` reports operational readiness, independently of the expensive API snapshot. A backfill is healthy only while its checkpoint progresses. `/livez` reports that the HTTP process is reachable; it must not be used as evidence that collection or trading works.

`runtime_health.monitor` runs in a separate thread. It checks free disk space and commits a small database heartbeat through a separate connection with a one-second lock timeout. It emits structured `worm_health_alert` and `worm_health_recovered` log events without relying on database writes. A stopped monitor is itself detected through its expired probe timestamp.

Storage warning: below the greater of 32 MiB or 10% free. Storage critical: below the greater of 16 MiB or 5% free. Critical storage, a failed write probe, stalled collection or stale workers block new trading entries. Catch-up also blocks new entries. These checks do not disable exits. Logs alone are not email delivery: an operational alert receiver must be configured and its delivery tested separately.

The pipeline and payment-recovery exception handlers tolerate a second failure while recording an error to the database. SQLite transaction cleanup also tolerates automatic rollback during disk exhaustion. Neither failure should silently terminate its retry loop or replace the original storage error with a missing-savepoint error.

The watchdog now detects an indexer stalled during backfill. It avoids repeatedly restarting a process with exhausted storage or a failed write probe. Restarting cannot create disk space. There is no automatic deletion of databases, journals, backups or financial evidence.

Completing the startup backfill does not immediately qualify trading: its target was captured at startup and the chain may have advanced since. Cached chain-head timestamps and the last committed checkpoint estimate the remaining lag without another RPC or database read. A gap of three minutes or more keeps the catch-up entry gate closed.

Optional token metadata uses a shared 15-second network budget across its batches; uncached pair symbols use 10 seconds. Each request gets at most five seconds or the remaining allowance, with no retry amplification in this optional path. Partial answers are preserved; missing metadata stays unknown, and graduated-token metadata can be retried by the existing refetch routine. This is a network budget, not a hard process deadline. Ordinary RPC calls, transaction submission and receipt recovery retain their original behavior. The new bounded batch method only accepts `eth_call`.

Position monitoring has its own thread, separate from discovery and holder-feature queries. Live positions are handled before paper positions in that pass. The marker's fallback exit stage runs before research/advisor work. Worker freshness is checked independently. This is not yet a dedicated-wallet architecture: RPC access, database locks and signing infrastructure remain shared, and long RPC/receipt calls still require further latency testing before unattended live trading.

The UI independently polls operational health. A freshly served but stale snapshot does not override the delayed-data indicator. Paper aggregate unrealized P&L is unavailable if any position lacks a fresh valuation; `priced_unrealized_usd` remains an explicitly partial subtotal in the API.

### Capital and execution

Lifetime budget accounting now removes all `trade_profit` allocations from cumulative realized profit before calculating capital lost. Money already owed to burning cannot fund another trade. A settled burn is not subtracted again. Retained, undistributed profits can cover losses, but room never rises above the configured original budget. This is an accounting repair, not a replacement for a separately funded trading wallet or a full independent capital ledger.

New paper positions use acquisition cost per received token as the exit reference, matching receipt-settled live positions. Gas remains a separate cashflow. The pool mid remains in `last_usd`. Model `quoted-pool-v2` records the new semantics; existing rows are not rewritten or reset. Older quoted positions continue to use quotes for valuation and exits, never synthetic fills.

Prospective live-validation cohorts now require:

- The same entry rule and frozen exit policy.
- The current execution model and recorded execution specification.
- A quote asset supported by the live adapter (currently USDG).
- The same versioned feature, price and policy implementation and execution assumptions.

The implementation fingerprint conservatively hashes the relevant source files. A source edit can therefore void a pass even if the edit is only a comment. This favors evidence safety over reusing historical results. Old cohorts become voided; their records remain. ETH paper positions remain research results and do not qualify the USDG live adapter.

Failed renewal cohorts immediately revoke prior passes. A missing member or a position still open beyond its maximum holding period plus one hour invalidates its evidence; it does not create a sale, assume a fill or rewrite the position's P&L. A position closed after this deadline also cannot qualify the strategy, even when recovery closed it before evaluation ran. If the policy has no maximum age, the evidence deadline is 48 hours plus one hour. A cohort still needs all required members before final evaluation.

## Production storage recovery: operator-approved procedure

The local patch does not resolve an already-full production volume. Recovery must be approved separately from a code push.

1. Confirm trading is off. Inventory the active volume, journal and existing backups privately. Record current deployment and database checkpoints. Do not expose variables, keys or journal payloads in tickets or logs.
2. Establish destination capacity outside the active state directory, preferably a separate encrypted volume or off-host storage. If necessary, approve a capacity increase first. Do not begin `VACUUM` or a new large backup on a full volume.
3. Stop every writer and signer before taking a coordinated snapshot of the operational database and transaction journal. An operator pause marker alone does not stop database writers.
4. Run `scripts/backup-state.py SOURCE DESTINATION --service-stopped`. It refuses a destination inside the active state directory, checks destination capacity, uses SQLite's backup API, checks database integrity, and writes a checksum manifest and a payment-pause marker. It does not export environment files or keys.
5. Transfer the private backup to the approved off-host destination. Run `scripts/verify-backup.py BACKUP` after transfer. Independently verify the manifest and an offline restoration of both databases before deleting any old snapshot. Backup files contain sensitive operational state even though keys are not deliberately exported; never commit them.
6. With approval and verified off-host copies, archive only the specifically identified old backup directories or increase capacity. Do not delete the active database, WAL, SHM or transaction journal. Existing backups without manifests must first be checked privately; the verification script deliberately refuses incomplete backup formats.
7. Restore into an isolated directory with no signer and payments paused. Check both databases' integrity and expected records. Never run two signers against the same wallet. Preserve the original state until restoration is accepted.
8. Recover the service with trading still off. Verify storage headroom, successful write probes, advancing chain checkpoints, new scan records and fresh worker timestamps. Reconcile pending journal entries before allowing payment activity.
9. Reconcile overdue paper positions using available executable quotes. Label the collection gap; do not invent historical stop fills or turn missing prices into zero P&L.
10. Verify health failure and recovery are visible and alert delivery reaches its intended recipient. Set a storage-growth review and off-host backup retention policy. Same-volume snapshots are not a disaster-recovery plan.

## Remaining release gates

- Production headroom and collection recovery verified after the approved intervention.
- Off-host backup restoration and actual alert delivery demonstrated.
- Independent capital custody/ledger and bounded exit latency evaluated before live trading.
- Fresh prospective evidence for the intended live universe, with fair per-strategy opportunities, measured execution costs and uncertainty checks.
- Separately authorized funded buy/sell and recovery rehearsal for the current live adapter.
- Explicit user approval before pushing, deployment and live enablement.

No paper policy is represented as profitable by these engineering changes. Entry thresholds and loss limits were not loosened to unlock trading.
