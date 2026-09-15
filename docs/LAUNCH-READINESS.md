# Release and launch readiness

This document describes verification gates, not a declaration that production is ready or risk-free.

## What is ready, and what remains

- **Public website and source:** the confirmation/UI release and public overview are deployed (PRs #9 and #10). Verify the exact revision again after each release.
- **Token launch:** the operator selected September 17, 2026 at 17:39:40 UTC. Metadata, production funding, the initial purchase quote and protected reserves were verified before scheduling. At the scheduled time, the server checks the current funds and economics again before execution. Follow the public countdown/status for changes and progress.
- **Unattended operations:** recurring off-host backups and application-specific delivered alerts remain deferred. Manual restore drills and Railway service notifications do not replace them. Future official-token pool/burn verification remains separate.
- **Trading:** disabled until the operator explicitly requests it, with live exit and execution validation still required.

Historical handoff entries describe the state at the time they were written. Later release entries supersede earlier local-only statements; a passing test suite does not close every operational gate.

## Evidence already obtained

The regression suite is rerun for each release; see CLAUDE_HANDOFF.md for the final release count. Separate, limited on-chain rehearsals verified a token launch, local countdown transition, on-chain metadata and creator tax, a USDG-to-ETH conversion, an escrow claim, the new creator allocation payout, and a small GLD purchase. A later bounded rehearsal also exercised the new automatic curve sweep and claim cycle, operations-funded atomic ETH refill, and restart duplicate prevention. These used explicit small-test thresholds. Funded daily claiming remains covered offline; prolonged unattended deployment is not yet proven.

The latest 1 USDG AI Surplus payment was matched to confirmed provider history and a 1 USD credit increase. Burn verification combines a fresh current-code simulation with an earlier confirmed liquid-token burn; it is not a new official WORM burn. Shadow AI evaluation needs future completed cohorts; it is not a proven performance result.

## Before arming unattended fee operations

- The claim/sweep/refill release was deployed and verified with LIVE and trading off. Review any newer local audit changes before release, preserve both databases and verify the exact deployed revision, health and runtime settings afterward.
- The automatic sweep-to-claim path passed a bounded rehearsal and a duplicate-prevention restart check for the supported rehearsal curve. Different curve settings or future graduated pools still need their own verification.
- The bounded ETH refill is implemented and rehearsed. Review its production limits before enabling WH_GAS_REFILL; preserve bootstrap ETH, reservations, and one active signer.
- Verify the graduated-token burn path under the current policy and provider compute-credit settlement separately. Keep burn allocations reserved until the token has the supported pool. Do not spend enough to force graduation merely to create a test case.
- Verify restored database/outbox consistency on an isolated stopped signer, scheduled encrypted off-host backups, and actionable delivered alerts for low ETH, stale indexing, unresolved payments and unavailable browser service.
- Recheck GitHub collaborators, write-capable deploy/SSH keys, tokens, installed apps, branch/ruleset protections and bypasses, account 2FA/passkeys; separately check Railway members, tokens, source integration and deployment triggers. Never equate public read access with write access or claim immunity to account/host compromise.
- Verify the intended production wallet, creator recipient, metadata, token identity and schedule. Code publication is not permission to launch a token or enable payments.

## Trading remains a separate gate

WH_TRADING must stay off until explicitly enabled by the operator. The live-sell gate remains closed; do not enable trading just because fee handling is ready. Live exits, execution liquidity/depth and adversarial/slippage behavior require their own implementation and validation.

## Where users can read about the new policies

The website Docs page explains sweep → escrow → adaptive/daily claims → reserved fee allocation, gas checks, planned-cost runway and bounded automatic ETH refill with bootstrap funding. CLAUDE_HANDOFF.md records implementation details. docs/PAYMENT-RECOVERY.md describes the recovery model; docs/ENGINE-EVIDENCE.md describes evidence and learning limits. Check the actual deployed revision rather than assuming that local documentation is already public.

## Open operational gates

- Treasury hardening released in PR #8 uses exact per-purchase burn/gold approvals, a ten-minute Permit2 expiry, quotes refreshed after approvals, a 30-second pre-sign quote limit, and three-minute on-chain swap deadlines. Legacy excess approvals are reduced when the next eligible purchase runs; no existing on-chain allowance has been revoked merely by editing code. Earlier swaps do not establish bounded-rehearsal evidence for every changed purchase path; retain that verification gate separately from publication.
- Latest small AI Surplus transfer: confirmed in provider top-up history and a matching 1 USD balance increase. Continue to distinguish chain submission from provider credit.
- Connect and test an email alert receiver for the private operational health endpoint. A Railway metrics dashboard alone does not provide low-wallet or stuck-payment email alerts.
- Configure recurring off-host backup delivery and key custody. Two off-host encrypted restoration drills passed, including a fresh paired production snapshot; recurring delivery is not configured by those drills.
- The official token is not launched or graduated. A fresh burn simulation and earlier liquid-token burn evidence do not prove a future official-token pool.
- The funded rehearsal is closed and its Robinhood Chain refund is complete. The operator explicitly left the small Base USDC balance untouched; the closed rehearsal signer must stay paused.

## Current launch preparation

The confirmation release (PR #9) is deployed and verified on both production services. It defaults to verified successful block inclusion, as requested by the operator, with finalized settlement remaining optional. Runtime code and website assets matched the release, operational health passed, and no pending payments were present at verification. Review confirmation modes, reorganization monitoring and migration behavior in PAYMENT-RECOVERY.md.

The production signer and separate operations credential have been verified privately. The name, logo, social links, tax and fee split have operator approval; the description explains screening, memory, AI proposals and paper trading. Production funding and the operator's launch time are now confirmed. Live execution must be armed for the scheduled launch; trading stays off. Do not set a trial timestamp in production: a persisted due request can execute after LIVE is enabled. A countdown is permission to start checks at that time, not a promise of instant launch or chain finality.

The opt-in initial 2% supply purchase is implemented and has passed a bounded public-chain rehearsal: launch and buy are one Pons router transaction; the creator’s 1% transfer is a separate, journaled transaction after verified successful inclusion. The current-contract local fork simulation verified the timer, purchase, metadata, creator tax, transfer, zero remaining router allowance and duplicate prevention. The later public-chain rehearsal separately verified the purchase and exact creator allocation; it does not certify a future official-token pool. Review the explicit spending cap, protected reserves, and fresh quote before configuring it. See INITIAL-ALLOCATION.md. Recurring backups and application-specific email monitoring were deferred by the operator; native Railway service notifications and manual backup drills do not close those gaps.

## Service recovery verification

The actual application subprocess passed an isolated local EVM drill with disposable funds and external service sockets blocked. The drill injected RPC outages, a lost broadcast acknowledgement, process crashes, and an unmined transaction across a restart. The service recovered automatically, reconciled exactly two creator payments, left no pending ledger/outbox rows, and did not duplicate either payment after an additional restart. Scheduling sleeps were accelerated; signing, transaction journaling, receipt validation and treasury reconciliation used the application code.

This verifies service/payment recovery under those faults. The local token was a test ERC-20, so the drill does not retest Pons contract behavior, external provider uptime, every indexing fault, or prolonged unattended operation. Those limits remain distinct from the earlier funded Pons rehearsal.
