# Engine evidence and recovery update

## Purpose and scope

Phase A of the engine audit strengthens data integrity without changing the fee split, wallet configuration, launch settings, scoring thresholds, or the live-sell safety gate. Publication and deployment are separate actions; verify the deployed revision and runtime flags.

## Changes

- **Fresh prices:** one quote validator rejects stale, non-finite, non-positive, and explicitly unavailable observations. The production price adapter always supplies quote age. Paper entries, paper/trader marks, lab sampling and baseline refresh use the validator. A successful API response that omits a token invalidates its cached quote. Provider failure may retain an aged display value, but consumers cannot use an excessively old quote for decisions. Lab ticks retain the quote observation time. Missing API data no longer generates synthetic zero-price fills.
- **Immutable assessments:** `assessments` captures each successful scored version, including features, explanations, applied rule weights, observation block, timestamp and engine evidence version. The existing `scores` table remains the latest UI result. Outcomes retain the first prediction and link to its assessment; rescans cannot rewrite its verdict or receive credit for earlier price movement. A retry after an interruption between scoring and outcome recording recovers the earliest saved assessment. This version still tracks one primary outcome per token; independently evaluating every rescan is later work.
- **Outcome validity:** missing final evidence resolves as unknown, not a baseline-derived flat return or a stale earlier checkpoint return. Baseline observation time is stored separately. The brain checks pending outcomes at each marker cycle, allowing adverse-event monitoring between checkpoints. Two early adverse readings must have distinct observation times separated by the confirmation interval; replaying a cached observation cannot confirm a collapse. A valid final checkpoint can resolve directly. A two-reading confirmed adverse event can also resolve outside that final window; it is event evidence, not a fabricated 24-hour reading.
- **Atomic learning:** nested SQLite savepoints make related outcome, weight, event and lab-aggregate writes a single rollback-safe operation. Resolution reads the current unresolved row inside the transaction. Retrying an already resolved outcome or case cannot increment its aggregates again.
- **Durable scanning:** `scan_jobs` replaces the worker's volatile queue and timer retries. Graduation state and its scan job commit together. Jobs have bounded attempts, delayed retries, leases, deduplication and failed state. An old attempt cannot acknowledge a newer lease. Resumed backfill creates jobs for graduations after the durable rollout boundary without automatically scoring the entire historical backfill. Public queued counts now come from durable jobs; failed counts are separately available in API stats.
- **Stricter readiness:** at least ten checked healthy and ten avoid verdicts, positive measured warning skill, a positive strategy bound, sufficient surplus and the existing readiness threshold are required. Runtime readiness uses complete versioned assessments with baseline provenance. Historical scorecards remain visible but cannot be passed off as this new verified cohort. The explicit live-sell gate remains closed.
- **Public diagnostics:** scoring, backfill, skipped-range and narrator failures use safe public messages instead of raw exception details. Detailed exceptions stay in private logs. This patch does not claim a complete review of every event producer.

## Migration and historical data

Schema changes are additive: new assessment/job tables and nullable outcome links/timestamps. Existing scores, outcomes, weights and lab aggregates are preserved. There is no automatic relabeling, reset or reconstruction of missing historical facts. New columns/tables initialize when the updated application opens its database; no production database was migrated during local development.

The AI advisor now reads frozen, complete assessment features instead of joining mutable latest-card features. Legacy outcomes without links are excluded from this feature history. It may wait for new valid observations before proposing rules. Already adopted rules and old weight/strategy aggregates are not retrospectively certified or cleared by this patch.

Readiness may fall because its checked cohort is now stricter. This is an evidence-quality correction, not lost balances or missing scans. Existing historical scorecards and new readiness evidence intentionally describe different cohorts.

## Recovery behavior and limits

The queue is designed for the existing single scoring worker. A lease lasts 30 minutes; interrupted work becomes eligible after expiry. There are three attempts and a ten-minute delay after a failed/partial result. Failed jobs remain visible and an explicit rescan can request another attempt. A worker waiting on treasury work renews its lease. Do not run multiple independent workers against the same volume without adding worker fencing and heartbeats across the whole scoring operation.

The rollout boundary includes graduations first ingested after the previous persisted block cursor. Old missing jobs and previously skipped block ranges are not silently reconstructed. Canonical block/reorganization handling and a durable skipped-range repair scheduler remain follow-up work.

Monitoring still uses the existing sequential marker cadence and external feed. It is not a guaranteed real-time crash detector. Missing prices leave positions unmarked rather than inventing executions. Partial paths, quote-only execution validation, and more conservative market-depth simulation remain strategy-validation work.

## Verification

Latest local result: **445 passed**, including 30 new audit and shadow regression cases; one existing Starlette/AnyIO deprecation warning.

The offline suite includes audit regressions for cached quote freshness and timestamps; missing outcomes; rescan attribution; interrupted and duplicate learning; atomic graduation/job writes; restarted leases and bounded retries; downtime catch-up; immutable advisor history; legacy migration; and readiness that cannot be unlocked with treasury alone. Existing tests whose expectations encoded the old unsafe behavior were updated accordingly.

Run locally:

```sh
PYTHONDONTWRITEBYTECODE=1 WH_ENV_FILE=/dev/null WH_TRADING=0 .venv/bin/pytest -q -p no:cacheprovider
```

Use the existing deployment backup/recovery procedure before any later rollout. These tests use temporary databases and fake services; they do not constitute a live trading rehearsal.

## Next phase

Prospective AI shadow evaluation, matched strategy cohorts, coverage-aware provenance rules, full historical data-quality review, broader worker/provider health reporting and execution-depth tests remain recommended. This patch must not be interpreted as proof of profitability or permission to enable live trading.

## Prospective scoring-rule gate

New AI scoring rules that pass historical screening enter shadow mode. The first 80 eligible later launches form a fixed cohort, with one launch per creator and creators in the discovery history excluded. Selection happens without inspecting outcomes. The evaluator waits for every selected outcome to resolve; unknown results are excluded, not replaced with more favorable later cases. At least 20 matching and 20 comparison cases must remain. A directional median separation and a conservative randomization test are required. Candidate alpha budgets spend a total bounded 0.02 across the lifetime of the table; finite tests use an add-one correction. A rule is evaluated once, not repeatedly until it passes. A passing candidate can become a scoring rule if capacity permits, but can never enable trading.

A fixed cohort may take days or longer to fill. This is forward evaluation infrastructure, not a claim that future results have already been observed or that trading is profitable. The website exposes pending and completed shadow results. Previously adopted rules and exit-arm experiments are not retroactively reclassified by this addition.

Operator policy: production `WH_TRADING=0` remains explicit. Only an authorized operator setting change can enable trading; additional live-execution and readiness gates still apply then.
