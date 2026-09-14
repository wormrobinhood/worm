# WORM handoff: security recovery and scheduled launch

Updated 2026-09-14. Source of truth: the main WORM repository (not a separate design copy).

**Release scope:** publishing safeguards, payment recovery, private browser isolation, and a configurable launch countdown. A code release does not authorize a token launch or live payments. Runtime credentials and launch scheduling remain private operator configuration.

Base commit reviewed: `ede309734385e9bddc29c663199ef2484422076d`.

## Why these changes were made

This work preserves launch, claim, creator payout, burn, gold, and inference payment paths while hardening interrupted or ambiguous outcomes. Existing claims retain their recorded allocations; historical policies must be reviewed before migrating older rows without saved splits.

Paper trading remains available. The website countdown can trigger launch checks at an explicitly configured deadline. No deadline is supplied by this release, and the scheduler cannot arm live execution itself.

## Changes and rationale

- `scripts/leakcheck.py`: Git/read failures and malformed push inputs block publication. Every historical path/blob pair is checked, including files renamed out of private directories. CI scans all reachable history. Symlinks, submodules, rehearsal files, and SQLite sidecars require review or are rejected.
- `scripts/publish-check.py`, `scripts/deploy.sh`: verify the effective WORM author/committer identity, dedicated SSH authentication, complete commit history, and the exact outgoing archive before a manual Railway upload. The upload targets an explicit project, service, and environment. Recursive build/upload exclusions protect private state.
- `.github/workflows/checks.yml`, `.github/dependabot.yml`, `SECURITY.md`: offline regression checks, full-history leak scanning, container builds, dependency updates, and a private reporting channel. Actions use immutable commit references and read-only repository permissions.

- `wormhole/outbox.py`, `tx.py`: persist private signed transaction material before submission; callers save pending bookkeeping before the network hears the transaction. Unknown transactions block new sends and recover using identical signed bytes. A interrupted `preparing` entry requires operator review rather than blind resubmission. Add conservative ETH fee/value caps and a `payments.paused` file check.
- `treasury.py`, `db.py`: fail closed on accounting errors; reserve pending payouts as well as outstanding allocations; keep successful-but-incomplete receipt evidence pending; stop calling node absence a proven failure. Snapshot split percentages on new claims. Legacy claims require explicit migration inputs. Use FULL SQLite synchronous durability and atomic grouped writes.
- `launch.py`: save token identity and pending clearance atomically; missing launch event is unresolved, not a revert.
- `server.py`: every launch-control request now requires `WH_OPS_TOKEN`, including localhost. Public status exposes no control credential or signed transaction material.
- Public compute errors and top-up events use fixed messages, so provider/wallet exception details cannot reach API responses or the live feed. This fixes the CodeQL exception-exposure finding. The page-origin test now compares parsed HTTPS authorities exactly instead of accepting lookalike domain prefixes.
- `compute.py`: reject a provider minimum above the approved top-up, cap dollars and authorization lifetime, pin Venice recipient, save authorization details before sending, and block new compute top-ups while any compute payment remains unknown. Old pending authorizations do not age into permission for another payment. Unknown Venice settlement deliberately requires evidence/operator review.
- `screen.py`, `Dockerfile.screen`, `scripts/browser-service.py`: LIVE mode uses an isolated CDP browser service; no local unsandboxed browser beside the signer. Non-live local rendering uses Chromium's sandbox. Live screen is disabled if the separate service URL is missing. Infrastructure setup is still required—see below.
- `requirements.lock`, `requirements-build.lock`: hash-pinned tested application dependencies and updated build tools. An OSV check of 54 installed packages returned advisories for local pip 24.0 and setuptools 79.0.1; replacement build versions pip 26.2.1 and setuptools 84.0.0 returned no advisories. The existing local venv was not upgraded. The final Linux image/Chromium still needs scanning.
- `scripts/backup-state.py`: stopped-service backup of BOTH databases, SQLite integrity verification, private files, and a pause marker on restore. Includes a disposable-data backup test.
- `wormhole/launch_schedule.py`, `run.py`: durable timestamp and state; a dedicated server worker checks once per second, independently of the five-minute treasury loop. No open website is needed. Existing token/pending state prevents a second scheduled launch. Future schedule survives restarts; if downtime spans the deadline, the worker acts after restart once checks pass. Failed/interrupted launches require review rather than repeated attempts.
- `web/index.html`, `design.css`, `design.js`: compact responsive countdown on Live above the stats, with local-time date and server-aligned seconds. Public states include unannounced, countdown, due, started, pending confirmation, failed/review, and launched with token link. The browser NEVER calls the launch trigger at zero. Stale connectivity is shown rather than declaring success.

## Set the launch time

No time is configured by this patch. Choose an explicit timezone and verify the intended wallet/token parameters first.

Option 1: set `WH_LAUNCH_AT` to a future ISO 8601 timestamp (with `Z` or an explicit offset) before starting WORM. It initializes the schedule only if no schedule exists in the database. Later environment edits do not overwrite a persisted schedule or silently rearm a cancelled/failed launch.

Option 2: authenticated `POST /api/launch/schedule`, header `X-Ops-Token: <private WH_OPS_TOKEN>`, JSON `{"at":"<future ISO 8601 timestamp with timezone>"}`. Send `{"at":null}` to cancel before launch starts. Do not put the credential in browser code or publish it. Public `GET /api/launch/status` reads state. Existing authenticated `POST /api/launch` remains an explicit immediate launch request.

At the deadline WORM starts launch checks within the worker's next iteration when the service is running and free to proceed. It requires LIVE enabled, a signer, no pause marker, verified accounting, no unresolved payments, and no existing token. Confirmation time depends on the chain; zero on the countdown does not promise an already-mined token. The scheduler does not enable LIVE or trading itself.

## Required before deployment

Read `docs/PAYMENT-RECOVERY.md` fully. In particular:

1. Back up and verify restore of BOTH `wormhole.db` and `transactions.sqlite3`; preserve pending operation associations. No real restore drill was performed.
2. Review historical claim allocations before supplying `WH_LEGACY_OWNER_SHARE`, `WH_LEGACY_BURN_SHARE`, `WH_LEGACY_GOLD_SHARE`. No historical data was changed here. Mixed-policy history requires row-level review, not a guessed global percentage.
3. Configure private `WH_OPS_TOKEN`. Configure verified `WH_VENICE_PAY_TO` only if using live Venice payments.
4. Build and test the isolated browser service; use `WH_SCREEN_CDP_URL=http://<private-browser-service>:9223`. The client handles Chromium's localhost discovery and retains the configured private destination. The relay accepts IPv4 and IPv6. Do not publish CDP. Give this service no secrets or shared data mounts and apply the platform's available resource/network controls. The application image contains no Chromium installation; container rendering always requires the separate service.
5. Verify final host flags/wallet/token metadata and persistent volume. Local rehearsal flags and public-host flags differ; code sync is not configuration sync. Operate only one signer instance per wallet.
6. Review the default fee/value and compute caps against the approved budget. Do not silently relax them to make a launch pass.
7. Run an explicitly authorized final-policy rehearsal if desired. Keep real trading disabled: the sell path remains deliberately unfinished; this task did not implement trading.

## Validation

- The offline suite covers the publishing guard regressions and private CDP discovery in addition to the recovery and scheduler cases below. Check the release's CI result for the final test count and Linux image build result.
- Tests include broadcast/lookup interruption, identical-byte recovery, persistence failure before send, atomic launch rollback, pending reservations, immutable split, legacy migration gate, old Venice pending blockade, recipient/amount bounds, localhost authentication, actual runner launch recovery, stopped-service backup, timezone validation, due-time execution without a browser, restart persistence, and no automatic repeat after failure.
- `pip install --dry-run --require-hashes -r requirements.lock` succeeds against the installed tested environment; this is not a clean Linux image build.
- No real payment or launch was used for validation.

Do not discard changed assertions as regressions: several old tests explicitly expected callback-after-broadcast, write-off on node absence, or provider-minimum overspending. Their expectations were updated to the safer semantics and new fault-injection tests added.

## Local engine evidence update — 2026-09-14

Phase A implementation is prepared for the authorized release. See [ENGINE-EVIDENCE.md](docs/ENGINE-EVIDENCE.md) for changes, migration behavior, regression coverage and remaining limitations. Prices must be fresh; assessments are immutable; outcome/lab resolution is atomic; scans use durable jobs; readiness requires a verified cohort and measured skill. Historical data stays intact and is not automatically repaired or certified. No wallet, live flag, token schedule or deployment setting was changed.

Validation: 445 offline tests passed (30 new audit and shadow regression cases), with one pre-existing Starlette/AnyIO deprecation warning. `git diff --check` passed. No production-data repair or live execution was performed.

## Publication follow-up: operator-controlled trading and prospective rules

The user authorized publishing the evidence update and explicitly requires trading to stay off until they enable it. Production must have `WH_TRADING=0`; do not change `WH_LIVE`, keys, launch schedule, or `LIVE_SELL_READY`. Readiness and AI cannot turn trading on.

New AI scoring rules now enter a fixed future-cohort evaluation after the historical screen. They do not affect scores until that evaluation passes. See `wormhole/shadow.py`: 80 later launches, one per creator, discovery creators excluded, outcome-blind cohort selection, minimum samples on both sides, lifetime alpha spending and one evaluation only. Unknown results do not become successes. Existing active rules remain stored/in use; this does not retroactively certify them. Exit arms remain separate strategy-lab experiments, not prospectively certified by this rule workflow.

## Local adaptive claim update (not deployed)

Claims now start at a 5 USDG batch, target six hours of recent income, and cap at 100 USDG. A balance of at least 1 USDG can become due after 24 hours from first observation. The due-time fallback never bypasses gas checks. Recent income uses settled claims plus current escrow balance over up to seven days, with a one-day minimum observation denominator. Targets can decline when income declines. Restart-safe observation timestamps and last decision are persisted.

A fresh ETH price and padded RPC gas estimate must show gas <=2% of the claim and leave at least 0.0001 ETH. The transaction sender repeats cost/reserve checks immediately before signing, so direct claim calls cannot bypass them. Sender transaction/daily caps remain active. Unknown prices/configuration/estimates fail closed. The website Treasury section exposes the last evaluated batch target and reason. This is a claim-specific reserve check, not a reserve guarantee across other outgoing transactions.

Settings are documented in .env.example. WH_CLAIM_MIN_USD takes precedence over the legacy WH_MIN_CLAIM_USD; existing legacy values are honored if valid. No fee splits, wallet settings, live flags, trading flags or running services were changed. The existing local Pons View token link change is preserved.

Automatic curve-to-escrow sweeping is now implemented locally as described below. Bounded operations-funded ETH refills are now implemented; see the later verification entry. They start disabled and still require bootstrap ETH.

## Local sweep and funded daily cadence update (not deployed)

`fee_sweep.py` runs before escrow claims. It validates the configured own token against the factory record, wallet, fee recipient and USDG pair, then verifies the curve's own factory/token/deployer/asset/escrow getters. Graduated curves are skipped; internal-buyback-enabled curves and pending buyback allocations require separate handling. Only `sweepFees(0)` is supported, after a successful contract simulation. No arbitrary tokens, swaps or beneficiaries are accepted. A sweep is not income; only the later confirmed claim receives the 50/10/20/20 allocation.

Combined curve + escrow fees must qualify for claim batching before sweeping. The gas budget uses only the newly sweepable creator fees, includes a conservative subsequent-claim allowance, and preserves the ETH reserve. The sender enforces the operation's ETH fee limit and remaining balance again immediately before signing. Existing transaction/daily caps remain. One sweep attempt per hour bounds repeated confirmed failures.

The additive `fee_sweeps` table stores pending bookkeeping before broadcast; private signed bytes remain in the existing transaction journal. Unknown or missing expected FeesSwept event evidence blocks the treasury cycle, including payouts, until reconciled. A successful factory-verified curve event records the actual credited amount. Confirmed reverts are distinguished from unknown outcomes. Missing/malformed configuration or chain data skips new sweeps; existing escrow claims may still proceed if there is no unresolved sweep. Curve fee collection still depends on ETH funding, provider availability and the deployed contract's permitted role. The subsequent bounded rehearsal exercised this automatic path on SOIL914; see the later verification entry.

The existing 90-day planned-cost projection did not previously control claims. Claim batching now shares its configured compute/gas/bridge cost inputs: free USDG after ledger obligations must fund at least 90 days without future income. Gold, token balances, ETH valuation, and prepaid compute credit do not count toward this conservative daily-mode trigger. Invalid or zero cost estimates do not establish coverage. With coverage, claims occur no more often than 24 hours after the last settled claim's recorded timestamp and require the minimum claim (default 5 USDG) plus gas checks. Without a prior claim, the first funded observation starts the 24-hour timer. Falling below 90 days restores adaptive batching. Restarts retain timers. Normal payouts for already claimed obligations are not delayed by the new claim schedule. This is estimated planned-cost coverage, not a guarantee of three months' future expenses.

Validation for the local claim/sweep update: 477 offline tests passed; inline JavaScript and design.js syntax checks passed; git diff --check passed. One pre-existing Starlette/AnyIO deprecation warning. No push, deployment, runtime flag change, or live transaction in this update.


## Operations completion work: 2026-09-14

`gas_refill.py` adds an opt-in operations-only USDG → WETH → native ETH path. Swap and unwrap share one deadline-bound router transaction, after an exact allowance if needed. Default thresholds: below 0.0003 ETH, target 0.0015 ETH, at most 5 USDG per refill and 10 USDG per rolling day, six-hour cooldown, keep 1 USDG liquid, 1% quote slippage, 5% total gas-cost allowance, independent fresh-price deviation check. Every chain submission preserves 0.00005 bootstrap ETH while refill is enabled. Never count reserved creator/gold/burn funds as refill cash. `gas_refills` stores pending intent before broadcast and reserves its exact input until proven settlement; unknown receipts also block compute payments.

A real small rehearsal uncovered the chain WETH implementation's burn-style Transfer event on unwrap. Settlement now supports that evidence as well as Withdrawal without double counting. Restart reconciliation settled the existing transaction without issuing a second refill. The small rehearsal spent 1.754507 USDG and received 0.000697564559396994 native ETH before gas. Its temporary settings were a 2 USDG cap, a higher trigger and a 10% gas-cost ceiling; production defaults were not changed to these rehearsal values.

The automatic treasury cycle swept 1.071058 USDG from the rehearsal curve, claimed the escrow, pinned the 50/10/20/20 policy and forwarded the creator share. A separate process repeated the cycle without any nonce change. Rehearsal batching was 1 USDG with a 10% cost limit, versus production defaults of 5 USDG and 2%. Empty-escrow claim budgeting uses a matching recent successful claim receipt when available: 2.6× its gas used with a 150k floor, otherwise 260k. Actual sends always estimate and recheck their costs again.

Current burn verification: the live chain accepted a fresh eth_call simulation of the current pool-swap-and-burn payload against Ponsorian, the same liquid asset used in the earlier burn rehearsal. The earlier successful burn receipt is still decoded correctly by current code. No fresh burn transaction was submitted; SOIL914 has not graduated and its allocation remains reserved. Do not describe this as a live burn of the official WORM token.

Current AI Surplus verification: a 1 USDG transfer was confirmed on-chain and matched the provider’s confirmed top-up history; the API balance increased from 7.610878 to 8.610878 USD after a delay. Do not repeat the payment to test a delay. The existing balance was above its automatic top-up threshold, so this was a bounded direct payment test, not a low-credit trigger.

An existing stopped-writer Railway backup of both databases was exported off-host, authenticated-encrypted with AES-GCM, decrypted and restored to an isolated local directory with payments paused. Both databases passed integrity checks; the restored outbox had no unresolved transactions. No signer ran from that restore. This establishes one restoration drill, not scheduled off-host backup delivery or a recent production rollback guarantee. Private encrypted state and its key remain outside the repository. `backup-state.py` now accepts relative source/destination paths safely.

`ops_health.py` checks low native ETH, pending payments, stale indexing/browser frames, pause markers and the operations heartbeat. It writes sanitized structured log transitions and serves authenticated GET `/api/ops/health`. Use a separate `WH_HEALTH_TOKEN` in `X-Health-Token`; the monitor never receives launch credentials. Endpoint detection and delivery are separate: Railway native monitors cover infrastructure metrics, not these business conditions. An email-capable external receiver still needs configuration. Trading and production LIVE remain off.

Validation for this release: 499 offline tests passed, JavaScript syntax and whitespace checks passed, and the working tree plus 61 reachable commits passed the privacy/secret scanner. Infrastructure email delivery and recurring off-host backup scheduling remain open gates.

Follow-up failure-case review: the refill attempt timestamp is now persisted before approval, so a confirmed reverted approval also observes the six-hour cooldown. This closes a retry path that otherwise depended only on the global ETH fee cap. Regression suite: 500 passed. A fresh pre-release production snapshot was taken with LIVE off and both SQLite databases write-locked; it too was authenticated-encrypted, copied off-host, decrypted and restored with both integrity checks passing.
