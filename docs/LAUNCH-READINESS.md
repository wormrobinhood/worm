# Release and launch readiness

This document describes verification gates, not a declaration that production is ready or risk-free.

## Evidence already obtained

The regression suite is rerun for each release; see CLAUDE_HANDOFF.md for the final release count. Separate, limited on-chain rehearsals verified a token launch, local countdown transition, on-chain metadata and creator tax, a USDG-to-ETH conversion, an escrow claim, the new creator allocation payout, and a small GLD purchase. A later bounded rehearsal also exercised the new automatic curve sweep and claim cycle, operations-funded atomic ETH refill, and restart duplicate prevention. These used explicit small-test thresholds. Funded daily claiming remains covered offline; prolonged unattended deployment is not yet proven.

Older burn and compute payment evidence belongs to earlier rehearsals. It does not certify every current contract state, provider payment or unattended recovery path. Shadow AI evaluation needs future completed cohorts; it is not a proven performance result.

## Before arming unattended fee operations

- Review and release the exact local claim/sweep changes. Preserve both databases and verify the deployed revision, health and runtime settings afterward.
- Exercise the new automatic sweep-to-claim path with a bounded rehearsal, including restart recovery; verify it does not need an operator-triggered sweep for supported curves.
- The bounded ETH refill is implemented and rehearsed. Review its production limits before enabling WH_GAS_REFILL; preserve bootstrap ETH, reservations, and one active signer.
- Verify the graduated-token burn path under the current policy and provider compute-credit settlement separately. Keep burn allocations reserved until the token has the supported pool. Do not spend enough to force graduation merely to create a test case.
- Verify restored database/outbox consistency on an isolated stopped signer, scheduled encrypted off-host backups, and actionable delivered alerts for low ETH, stale indexing, unresolved payments and unavailable browser service.
- Recheck GitHub collaborators, write-capable deploy/SSH keys, tokens, installed apps, branch/ruleset protections and bypasses, account 2FA/passkeys; separately check Railway members, tokens, source integration and deployment triggers. Never equate public read access with write access or claim immunity to account/host compromise.
- Verify the intended production wallet, creator recipient, metadata, token identity and schedule. Code publication is not permission to launch a token or enable payments.

## Trading remains a separate gate

WH_TRADING must stay off until explicitly enabled by the operator. The live-sell gate remains closed; do not enable trading just because fee handling is ready. Live exits, execution liquidity/depth and adversarial/slippage behavior require their own implementation and validation.

## Where users can read about the new policies

The website Docs page explains sweep → escrow → adaptive/daily claims → reserved fee allocation, gas checks, planned-cost runway and the remaining ETH-refill limitation. CLAUDE_HANDOFF.md records implementation details. docs/PAYMENT-RECOVERY.md describes the recovery model; docs/ENGINE-EVIDENCE.md describes evidence and learning limits. Check the actual deployed revision rather than assuming that local documentation is already public.

## Open operational gates

- Latest small AI Surplus transfer: confirmed in provider top-up history and a matching 1 USD balance increase. Continue to distinguish chain submission from provider credit.
- Connect and test an email alert receiver for the private operational health endpoint. A Railway metrics dashboard alone does not provide low-wallet or stuck-payment email alerts.
- Configure recurring off-host backup delivery and key custody. One restored historical backup is verified; recurring delivery is not configured by that drill.
- The official token is not launched or graduated. A fresh burn simulation and earlier liquid-token burn evidence do not prove a future official-token pool.
- Return rehearsal funds only after testing is closed. Base USDC also needs Base gas or an explicitly approved supported relay to leave the rehearsal wallet.
