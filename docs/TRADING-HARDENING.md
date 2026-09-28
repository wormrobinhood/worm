# Trading hardening: September 17, 2026

> Superseded in part on September 19, 2026 by [second look](SECOND-LOOK.md): paper entries no longer
> come from healthy verdicts, the sampled 30-token trial became a 50-position paper cohort, and live
> candidates follow the paper book. The execution checks, exact approvals, receipt accounting and
> journal described here still apply.

This update does not enable trading. `WH_TRADING` is unchanged and the code release gate
`LIVE_SELL_READY` remains false. Publication of these safeguards does not authorize live
trading, a funded rehearsal, or changes to wallet and launch settings.

## Paper entries and valuations

New paper positions require a complete **looks healthy** assessment with score at least
70 (or a higher `WH_BUY_MIN_SCORE`). WORM's own token is excluded. The initial supported
execution model is `quoted-usdg-v1`: a verified Pons-hook pool against USDG, a fresh reference
price, a buy quote and a reverse sell quote. No RPC/quote means no invented paper fill.

The checks reject reference-price deviation above 8%, immediate round-trip loss including
estimated gas above 15%, or entry gas above 10% of the order. These are admission limits,
not promises about the next block. Paper fills use the quote minus a 3% output tolerance.
Estimated gas includes the swap and two bounded approvals; fresh ETH pricing is required.
Exits use fresh sell quotes, including on partial sales. A failed quote does not mark a
profit target as filled. A position's exit policy is stored with it.

Existing paper rows retain their historical cost model. They remain visible, but do not
count as prospective validation. Missing current quotes are labeled unpriced rather than
reported as zero-profit positions. The displayed open P&L covers priced positions only
when others are unpriced. All paper results remain simulations.

## Research versus prospective validation

The existing 24 baseline arms and AI-added arms continue exploratory comparison. Their
historical rankings nominate one candidate; they cannot promote it directly.

A trial freezes the candidate and default policy before admitting a fixed cohort of 30
future eligible tokens. Admission uses original complete assessments, fresh USDG execution
checks, and distinct creators not represented in the research history at trial creation.
Re-scoring old tokens cannot backfill the cohort. The trial stores its own future prices.
It requires a continuous sampled path with no gap over 15 minutes; missing cases fail the
cohort rather than being dropped or replaced with winners.

Both frozen policies run on those paths with token-specific fees, 3% slippage allowance
per side and estimated gas for every entry/exit leg. The candidate needs a positive lower
bound on net return; a non-default candidate also needs a positive paired improvement
bound over the default. No decision is made before all 30 cases finish. A decreasing
significance budget across trials limits repeated attempts to find a lucky passing result.
The normal approximation relies on independence and finite variance; distinct creators
cannot eliminate market-wide correlation, manipulation or model error.

A passing trial expires after seven days and is invalidated if either policy definition
changes. Readiness and live entry checks both require a current pass. Historical readiness
scores alone cannot authorize a trade. Delayed entries and exits in these trials still use
sampled price paths: entry liquidity checks do not prove that every later simulated exit
could have filled. Actual sell-quote paper results and a funded rehearsal remain separate
release evidence.

## Live pilot and transaction recovery

The implemented pilot supports USDG pools only. The old ETH-buy skeleton is removed from
live execution because its exit/proceeds accounting was incomplete. Legacy demo orders
retain their original behavior and are not evidence for the new pilot.

Approvals are exact amounts at both the token and Permit2 layers, with a ten-minute Permit2
expiry and readback verification. Quotes are refreshed after approvals. Signing has a
30-second quote limit; the on-chain swap expires within three minutes. The router enforces
minimum output. Position token quantities are also stored as exact integer units.

Every swap has a durable intent before submission. Its hash is stored by the sender's
pre-broadcast callback. A lost acknowledgement keeps that hash pending; the existing private
outbox can recover the same signed bytes. A submission without a safely recorded hash is
held for operator review. Pending and review buys retain their position slot and budget.

Receipt accounting uses net token and USDG Transfer evidence under the configured chain
confirmation policy. Missing or contradictory evidence is held for review, never replaced
with the quoted amount. Holdings and order status update in one database transaction.
Repeated reconciliation cannot create another position or apply a sale twice. Partial-sale
flags advance only after settlement. Failed sells preserve holdings and profit-target state;
they reserve estimated gas and have a retry cooldown. Token donations are never included
in the amount to sell. Legacy holdings without an exact execution record require review.

This is implementation and offline evidence, **not an on-chain trading rehearsal**. The
existing funded launch, gold and treasury rehearsals do not validate this new trading path.

## Loss circuit breaker

`WH_MAX_DAILY_LOSS_USD` defaults to **10**. It sums gross losses from recently closed
positions and current open liquidation values; profitable positions do not replenish the
allowance. Unpriced live holdings block new entries. Reverted buys and interrupted approval
attempts reserve estimated gas as well. Gas figures are conservative model estimates, not
an audited USD conversion of every receipt's actual fee.

At the limit, new entries pause for at least 24 hours after the last observed breach. Exits
remain independent of `WH_TRADING`, readiness and this entry pause, once the live-exit
release gate is opened. The global signing pause and unresolved private outbox still take
precedence. A loss trigger is not a guaranteed maximum loss: prices can gap, pools can lose
liquidity, RPCs can fail, and checks run on the marker interval (normally five minutes).
The existing position, daily spend, open-slot and 90-day reserve checks remain in force.

## Before separately authorizing a live trial

1. Review the changed code, public copy and exact revision; retain database and private
   outbox backups together. Nothing in this update is published automatically.
2. Collect the future cohort and inspect fresh quoted paper results, failed quotes,
   gas costs and the loss breaker. Historical profitability is insufficient.
3. On a separate funded rehearsal wallet, verify a tiny USDG buy, partial take profit,
   full stop/trailing exit, exact approvals, actual receipt amounts and gas under current
   contracts. Test lost responses and restart reconciliation without repeating payments.
4. Review position sizes and polling latency, and finish production alerting/backup gates
   from LAUNCH-READINESS.md. Confirm how an operator resolves REVIEW rows from chain evidence.
5. Only after separate operator approval, consider opening the release gate and a bounded
   live pilot. AI and readiness cannot change these switches themselves.


## Routing any pair from USDG (September 28, 2026)

This update does not enable trading either: `WH_TRADING`, `LIVE_SELL_READY` and the zero
trading budget are unchanged.

Most graduations pair with ETH or a tokenized stock (48 of the last 60 with ETH, 5 with
stocks, 2 with ORBIO, 5 with USDG), and the wallet holds USDG. Live and paper now buy and
sell any Pons token with USDG through the best checked route (`wormhole/route.py`):

- **Candidates.** KyberSwap and LI.FI (asked in parallel, no API key, a dummy address for
  every read-only quote), plus the token's own pool when it trades against USDG (the
  existing Universal Router path). Relay is asked and recorded for comparison but never
  executed: its calldata is a multicall whose minimum sits inside third-party calls and
  cannot be checked. It was never the best route in our samples (99.7 to 99.9% of the best).
- **Choice.** Most tokens out wins. A route further than 15% from the pool's own mid (the
  existing `MID_API_BAND`) is not believed; an aggregator fee above 1% of the order is not
  used for an entry. The existing impact (8%), round-trip (15%) and gas (10%) limits apply
  to the chosen route, whose round trip counts every hop, the creator tax, impact, the
  aggregator's fee and gas on both legs. No route means no fill.
- **Providers fail safely.** Four-second requests, five seconds for the whole fan-out, at
  most four requests a second per provider. Three failures in a row, or one rate limit,
  stand a provider down for 30 seconds, doubling to at most 15 minutes; the first request
  after that is the probe.
- **Live execution (dormant).** For an aggregator route the transaction is built afresh for
  the wallet and refused unless: it goes to that provider's allowlisted router
  (`route.ALLOWED`); it carries no ETH; its calldata, decoded, pays the wallet, spends
  exactly the quoted USDG (or tokens) and encodes an on-chain minimum at or above ours (the
  quote less the 3% cap; the provider is asked for 2.5%); and an `eth_simulateV1` from the
  wallet, with the exact approval simulated, delivers at least that minimum and spends
  exactly the amount. Only then is an exact ERC-20 allowance granted to the provider's own
  spender (a different standing allowance is zeroed first; never unlimited). The quote is
  refreshed after the approval through the same provider only (a different winner means no
  order), rebuilt, checked and simulated again, then sent through `tx.send_tx` with the
  durable intent, journal matching (now for every allowlisted router) and receipt
  settlement as before. Exits take the same path in reverse, banded by the mid that
  triggered them. The pool's own USDG route keeps its Permit2 path.
- **Checked on chain, read-only.** A KyberSwap and a LI.FI route for $10 USDG into an
  ETH-paired token, built for a public USDG holder (not the worm), decoded and simulated,
  delivered 99.98 to 100% of the quote and reverted without the approval.
- **Found on the way.** `chain.call_fn` returns a single output as the value itself. The
  pilot indexed it for its USDG and token balances and one allowance, so every live order
  would have been deferred at its first read. Fixed (`live_trading.read_uint`).

Residual risks: the aggregators and their routers are third parties; an allowlist change is
a code change. A left-over exact allowance can remain when an order fails after its
approval (bounded by that order's amount). A route can move between simulation and
inclusion; the decoded on-chain minimum is what bounds it. Before a live trial, the funded
rehearsal above must also cover one routed buy and one routed sell (an ETH-paired and a
stock-paired token).
