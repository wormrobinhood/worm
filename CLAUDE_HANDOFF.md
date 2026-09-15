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

## Follow-up local audit and cleanup — 2026-09-14

These changes are local and have not been published or deployed. The running release remains the previously verified revision.

- Public launch, treasury, trading-simulation and balance errors now use fixed text. Upstream exception details remain in private logs where available, rather than entering the public event feed, trade notes or accounting status. This reduces the risk that an RPC URL or upstream response exposes credentials. It does not retroactively scrub historical database entries or establish that any secret was previously disclosed.
- Launch settlement preserves pending state for missing or invalid receipt status. The CLI refuses to proceed when its stored launch history cannot be read. Removed the unused age-limit constant and outdated suggestions that unknown launches could simply be cleared and retried.
- The transaction sender rechecks the operator pause immediately before signing, after lock waits and RPC preflight.
- Operational health now detects a loop that never produced its first heartbeat after the startup grace period.
- Tests clear inherited WORM configuration and use a disposable absent env file before importing the application. They no longer load the operator's project .env. Replaced a vacuous env-file assertion with a real isolation check. Removed regenerable bytecode left from the directory rename; retained source, tests, databases, keys, backups and rehearsal evidence.

Validation: 520 tests passed, including 20 new failure/privacy regression cases; working-tree secret/privacy scan passed. The existing published revision and its 65 reachable commits passed the publication/history/archive preflight. That archive check describes the existing commit, not these uncommitted changes. One existing Starlette/AnyIO deprecation warning remains.

This audit is not permission to enable LIVE, trading or a launch schedule. Email alert delivery, recurring encrypted off-host backups, account protection and final production configuration remain operational gates. Further treasury hardening to review includes limiting legacy burn allowances, a deadline for the gold swap, and an explicit receipt-finality policy. Do not represent earlier burn simulations or tests as verification of a future official-token pool.

## Local treasury swap safety: 2026-09-14

Authorized local changes, not released and no transactions sent. Burn approvals now authorize exactly the purchase input at both USDG-to-Permit2 and Permit2-to-Universal-Router layers, with a ten-minute Permit2 expiry. Legacy excess amounts and excessively long expiries are replaced when the next eligible burn runs. Both allowance layers are read back before continuing; a successful receipt with no effective approval blocks the swap. Gold approvals also reduce excess amounts and verify changed allowances. ERC-20 allowance itself has no expiry; unused bounded approval can remain if a later quote fails.

Burn and gold refresh quotes after approval receipts. A 30-second quote-validity bound is checked at sender entry and immediately before signing, including lock/RPC delays. Both swap payloads use three-minute chain deadlines; burn also caps its deadline at the verified Permit2 expiry. Gold wraps the exactInputSingle call in SwapRouter02 multicall(uint256,bytes[]), the same deadline wrapper already used by gas refill. Recovery retains the original signed bytes/deadline and releases the obligation only after a confirmed revert; no unknown transaction is cleared to retry.

Validation: 542 offline tests passed, including 22 new bounded-approval, quote-refresh/failure/expiry and recovery cases. Existing Starlette/AnyIO deprecation warning remains. Public RPC eth_call checks at block 62878102 accepted the gold router's empty multicall with a fresh deadline and rejected its expired counterpart. This proves that deadline wrapper is present, not a full gold swap or fresh signed rehearsal. No trading/live flags, deployment or actual allowances changed. The previous real swap rehearsals predate these payload changes; run a bounded rehearsal before release/launch readiness claims.

User separately requested an initial 2% supply purchase split into 1% WORM reserve and 1% creator. That is under feasibility/funding review and is not implemented or authorized for live execution by this entry.

## Local launch preparation and finality: 2026-09-14

This work is local, uncommitted and unreleased. No live transaction, production variable edit, deployment, or launch schedule was submitted.

Financial receipts on Robinhood Chain now require canonical inclusion at or below the RPC finalized head. The sender, private outbox recovery, treasury/AI Surplus ledger reconciliation, launch recovery, sweep/refill reconciliation and future live-buy reconciliation use the same policy. Mined receipts waiting for finality are not rebroadcast and do not release reservations. Missing or inconsistent evidence does not downgrade to a shorter confirmation policy. Public launch/payment messages identify an ordinary pending-finality wait rather than claiming immediate failure. Polls occur every five seconds for up to two minutes initially; later runner cycles finish settlement.

The only shorter policy is USDG/Permit2 approval calls, detected by destination and selector: 20 L2 confirmations with durable block/receipt monitoring until finalized. This is required to use the bounded ten-minute Permit2 expiry despite observed longer finality lag. Financial transfers cannot opt into that policy through a caller flag. Changed accepted approval evidence or a finalized checkpoint creates a persistent incident and payment pause. File and thread locks serialize finality updates; the journal migration serializes schema changes. The RPC remains a trusted source, not an independent finality verifier. Historical already-settled rows are not retroactively proven by adding this policy.

Private operational health allows one hour for normal pending settlement; interrupted preparation and detected finality incidents still alert immediately. This does not configure external email delivery. Native Railway service notifications are retained; the operator deferred application-specific email monitoring and automatic off-host backups. Preserve both state databases, including finality anchors/checkpoints, in backups. The existing stopped-state restore drills do not substitute for recurring backup delivery.

Validation: 566 offline tests passed, including 24 finality, canonical-history, receipt-identity, approval-depth, restart, migration, reconciliation and alert-boundary cases. The working-tree privacy scan passed (111 files); whitespace checks passed. The existing Starlette/AnyIO deprecation warning remains. No signed rehearsal of these finality and revised swap paths was performed in this step, and no release artifacts have been published.

Read-only production inspection confirmed no production signer or operations token, trading explicitly off, and public launch status unscheduled with live execution disabled. A separate operations credential was generated outside the repository with owner-only permissions, but has not been installed in Railway. Final metadata approval, private signing-key entry, address/funding checks and the exact date/time with timezone are outstanding. The proposed 2% initial allocation is explicitly deferred until launch planning and is not implemented. Never arm LIVE or set a test production timestamp as part of a code release.

## Local launch UI cleanup: 2026-09-14

Reworked the Live identity line into distinct execution, growth-stage and treasury-value groups. Payments-off and pre-launch states remain truthful; the UI does not enable a signer or schedule a launch. The overview metric now describes wallet assets instead of calling an unreserved wallet valuation spendable.

Treasury uses a full-width overview, followed by aligned runway and trading panels. The fee split remains visible before wallet setup and is derived from the server's current policy. USDG, native gas, claimable fees and provider prepaid credit are separate; a missing wallet balance is not rendered as a measured zero. Reservations, burn/gold totals, claim/sweep/refill reasons, payment history with its original currency, cost assumptions, projections, trading policy and positions remain accessible through native disclosures. Disclosure state survives data refresh. Runway estimates and unmeasured income are labeled; accounting errors replace reserve-coverage claims with a review state. Trading remains off even when readiness is high.

Removed the superseded dense Treasury HTML generator instead of retaining two competing renderers. Mobile order matches the desktop reading order. Checked the pre-launch, wallet-present, accounting-error and trading-off/high-readiness data states; HTML escaping protects displayed ledger text. Desktop 1440px and mobile 390px checks passed without page overflow, including the expanded projection. Panel expansion and retained disclosure state were checked. All 566 offline regression tests passed, script syntax and whitespace checks passed, and the working-tree privacy scan passed. Existing Starlette/AnyIO warning remains.

The older local preview was found to be displaying rehearsal configuration and history. UI review used a separate read-only preview of public production data, with current local assets. It contains no signer and forwards no mutation requests. Neither the old rehearsal runtime nor Railway settings were changed. These changes remain local and unreleased; wallet setup, credentials, rehearsal, release and launch confirmation gates still apply.


## UI-only publication: 2026-09-14

The launch identity and Treasury UI changes were isolated into PR #7 and merged as fdaf9fd06d7272349ef637a715feae2d60659568 using the dedicated WORM GitHub identity. Only web/index.html, web/design.js and web/design.css changed in that release. The existing backend baseline plus the UI passed 500 offline tests, JavaScript syntax checks, full outgoing history/archive privacy checks and required GitHub CI/CodeQL. The 566-test count elsewhere includes additional local engine hardening, which remains uncommitted and unreleased pending a bounded rehearsal. Main checkout was fast-forwarded and all pre-existing working files were preserved byte-for-byte.

Production signing-key setup is complete: key-derived address was privately verified against the operator-provided dedicated wallet. Operations authentication is configured. Keep WH_LIVE=0, WH_TRADING=0 and WH_GAS_REFILL=0; no launch timestamp has been selected. Trading must remain off until the operator explicitly requests enabling it. Credential setup and UI publication do not imply authorization to launch or transact.

## Initial 2% allocation: local implementation, September 14

The user requested a 2% initial purchase, retaining 1% in WORM and sending 1% to the creator. Implemented `wormhole/launch_allocation.py` with exact integer quoting, an explicit USDG spending cap, protected USDG/ETH reserves and pinned economics. The verified Pons router supports atomic launch + buy; the creator transfer is a separate finalized transaction. No custom launch contract is needed. USDG pairing and the existing fee split remain in place; trading remains off.

The saved plan fixes metadata, salt, wallets, economics and budgets before approval. Exact allowance and actual funded eth_call precede signing. Factory/router/mint/transfer evidence validates settlement. A launch operation lock, durable pending hashes and schedule integration resume the creator transfer even after own_token exists. Confirmed reverts and mismatched evidence stop for review. Unknown outcomes are retained. Public countdown status now distinguishes preparation and allocation completion; private health adds an allocation alert. `.env.example` and `docs/INITIAL-ALLOCATION.md` explain opt-in settings and recovery.

Validation: 594 offline tests passed, including 28 new allocation tests covering restart, receipt mismatch, missing outcomes, reserves, price changes, pinned creator/cap and trading-off behavior. A current-contract local fork exercised the timer, launch/buy, metadata, 2% creator tax, creator's exact 1%, retained allocation, empty router allowance and repeat nonce protection. No public token or real-money purchase was made. Fork execution uses a test-only gas cap because Anvil's gas pricing differs; production limits were not raised.

Current observed quote: 68.083317 USDG for approximately 20,000,000.4967 of the one-billion supply. Creator gets exactly 10 million; WORM keeps the remainder. This must be refreshed before funding/launch. All initial-buy settings default disabled/unset; no production schedule, trading flag or funding was changed. These backend changes and the allocation status UI are local, not included in the earlier UI-only release. The latest UI-only public main remains fdaf9fd06d7272349ef637a715feae2d60659568.

Outstanding decisions/evidence: approve a bounded real rehearsal or accept fork-only evidence for this new path; review and release the complete pending backend hardening; fund/verify the production wallet and confirm metadata/date/timezone. Application-specific email alerts and recurring off-host backups remain explicitly deferred. Native Railway notifications/manual restore drills do not close those gaps. Do not call the system fully unattended-ready or enable trading.

## Release verified: September 14, after PR #8

This update supersedes the preceding local-only status. The user authorized completing the remaining launch preparation while leaving the launch time unset. PR #8 was merged as e8db722f24065e8fbfb30bc762b18ad7ad68fbe7 through the dedicated WORM identity. All five GitHub checks passed, including both test jobs and CodeQL with no new alerts. The outgoing history/archive privacy checks passed. The main checkout was fast-forwarded to the released main.

Both Railway services successfully deployed that revision. Runtime verification confirmed the intended signing wallet, mounted persistent data, no unresolved transactions, and private operational health 200 with no alerts. Public health was 200 and design.js matched local bytes. Production preparation settings are WH_LAUNCH_BUY=1, maximum initial purchase 70 USDG, protected USDG 76.50 and protected ETH 0.0001. WH_LIVE=0, WH_TRADING=0 and WH_GAS_REFILL=0 remain in force. No WH_LAUNCH_AT or stored schedule was set, and no production token was launched.

A fresh paired production snapshot was captured with both databases write-locked, encrypted before download, restored off-host with payments paused, and passed both integrity checks. No restored signer was started. This remains a manual backup drill; recurring delivery and application-specific alert delivery are still deferred.

User help is still required to fund the real rehearsal and production wallets. Funding requests were sent; do not interpret silence as confirmation or spend real funds merely because these preparation settings exist. No new real-money rehearsal ran in this release step. The requested rehearsal cap is 70 USDG for the initial purchase and 0.001 ETH total gas, with the 0.0005 ETH launch fee separate. Production funding planning is 160 USDG plus 0.003 ETH, subject to fresh chain quotes and balance verification. Leave the production launch time for the user and keep trading off until their independent explicit instruction.
# Funded allocation verified; refund in progress: September 14, 2026

The funded SOIL2 rehearsal completed with successful finalized receipts for the exact USDG approval, atomic launch/purchase, and separate creator transfer. The purchase spent 68.083317 USDG and delivered 20,000,000.496662546101815026 tokens; exactly 10,000,000 were transferred to the creator. On-chain metadata, 200 bps creator tax, fee recipient, allowance cleanup, early timer behavior and duplicate prevention were verified. Actual gas for those three transactions was 0.000307979406768 ETH, separate from the 0.0005 ETH launch fee. The creator subsequently sold their allocation, so delivery evidence is the finalized Transfer event rather than their current balance.

The user ended additional spending tests and requested liquidation, remaining fee collection, and a refund. The private wind-down runner is handling only the rehearsal wallet: retained SOIL2, GLD, curve/escrow fees, then USDG and native ETH refunds. It preserves signed-transaction recovery and finality checks, and removes an old USDG Permit2 allowance. Refund completion is not yet verified. Do not start another signer or clear launch/outbox state; preserve both rehearsal databases together. The user explicitly chose to leave the small Base USDC balance untouched. Unspent historical allocations returned during closure must not be recorded as completed burns or compute purchases.

Production remains unscheduled, unfunded, LIVE=0 and TRADING=0; the user explicitly deferred production funding. This rehearsal does not certify a future official-token pool burn, live trading, recurring off-host backups or application-specific alert delivery.

Live Pons UI verification exposed a launch metadata bug: a bare X handle is rendered as a relative URL. `launch.params` now converts valid X/Telegram handles and profile URLs into absolute HTTPS URLs and rejects malformed profile values before constructing new launch intent. Saved launch plans remain unchanged. `.env.example` corrected. 605 offline tests pass (one existing AnyIO deprecation warning). These metadata changes are local and have not yet been published. The disposable SOIL2 token retains the old bare handle on-chain; do not launch an extra test token to conceal that issue. Website is present in the on-chain fourth socials field, though the current Pons page did not display a website link.

## Operator-selected inclusion confirmation: September 15, 2026 (local only)

The operator explicitly requested proceeding when a transaction succeeds in a block, rather than waiting for the finalized head. `WH_TX_CONFIRMATION=included` is now the default in local code; `finalized` remains an optional stronger policy. Successful receipt status alone does not release obligations: existing transaction identity, canonical block, repeat-read, exact transfer/launch-event, recipient and amount checks remain. Reverts are failures. Missing evidence stays pending. Already-pending operations use the configured policy; changing policy does not itself broadcast or duplicate a transaction. New outbox mode labels record the chosen policy.

Included receipts remain monitored until finalization; changed accepted evidence still latches a payments pause. This detects a later reorganization but cannot undo dependent transfers that already happened. RPC outages do not silently downgrade the policy. Gas budgets, spending reserves, live/trading flags, exact allowances, provider-credit verification and recipient constraints are unchanged. Pending UI/event copy now says chain confirmation rather than claiming a mandatory finality wait. Recovery and allocation docs explain the choice.

Validation: 622 offline tests passed, including new inclusion/revert/reorganization/receipt-identity/exact-recipient/duplicate-prevention cases and retained coverage for optional finalized mode; one existing AnyIO deprecation warning. The independent local X/Telegram metadata fix remains included in this uncommitted work. Nothing in this update has been pushed or deployed. Production trading remains off and launch remains unscheduled. The operator-authorized private rehearsal refund uses included confirmation; final refund evidence is recorded separately after verification.

The authorized rehearsal close-out is now complete under the operator-selected inclusion policy. Both refund receipts were independently verified for canonical successful inclusion, exact recipient and amount. The rehearsal USDG, GLD and test-token balances are zero, only gas dust remains on Robinhood Chain, and the user-requested small Base balance was left untouched. The rehearsal is paused and has no pending signed intents or ledger entries. Detailed wallet/transaction evidence remains private. Existing unspent allocation obligations were included in the requested final refund, not misreported as completed burns or compute payments; do not reuse the closed rehearsal ledger for a new funded service. This does not enable or launch production.

## Launch preparation: confirmation and recovery release

- Default transaction settlement now uses verified canonical successful inclusion (`WH_TX_CONFIRMATION=included`), with optional finalized mode retained. Accepted receipt monitoring, exact recipient/event checks, persistent outbox recovery, and incident pauses remain active. This is faster settlement with reorganization risk, not a finality guarantee.
- The actual service passed a disposable local EVM outage/crash/restart drill: HTTP stayed available, an accepted payment with a lost acknowledgement recovered, an unmined payment recovered after restart, and two payments settled exactly once with no unresolved rows. The fixture did not exercise Pons contracts; earlier funded rehearsal evidence remains separate.
- Metadata uses canonical X/Telegram profile URLs. The approved description now explains the screening engine, memory, AI proposals and paper trading while retaining the 50/10/20/20 fee split.
- Launch UI adds server-driven progress for schedule, on-chain creation and initial allocation. Long confirmation labels wrap on phones; delayed status does not show active/completed progress. No client-side launch trigger was added.
- Rehearsal refund is complete and the rehearsal signer remains paused. Production funding and the final launch timestamp are deferred to the operator. Keep LIVE, TRADING and GAS_REFILL off for this release. Recurring backups and application-specific alerts remain explicitly deferred; manual restore drills and Railway notifications do not replace them.
- Verification before release: 622 offline tests passed, including 17 confirmation-policy cases. A fresh paired encrypted production backup restored both databases with integrity checks passing and no unresolved transactions. Responsive and launch-state UI checks are recorded with the release evidence.
