# Paper execution and entry-filter research

The paper book remains a control experiment. A losing trade stays in its history. No AI model or shadow filter in this change can enable live trading or send a transaction.

## Exit monitoring

Entries serialize separately from exits. Each position has its own lock, and optional valuation network calls run outside those locks. All available exits are checked before valuation begins. A late valuation cannot reopen a position or overwrite the value of a different remaining quantity; an older mid observation cannot overwrite a newer mark.

Paper exit quotes use a shared five-second read-only RPC budget with no retries, plus a fresh cached ETH price for gas accounting. Missing prices or failed quotes leave the position open and measurable; the next monitoring cycle retries. These are request time budgets, not hard process deadlines. They do not cap market losses. A pool can gap through a stop, lose liquidity, or become unquotable. Live transaction broadcasting and recovery retain their existing behavior.

The fast position-price batch also uses a five-second budget. Incomplete prices or paper exits do not refresh the position worker heartbeat. Existing health gates can therefore stop new entries when that worker remains unhealthy. The monitoring target is 15 seconds plus processing time, not guaranteed execution every 15 seconds.

New columns record the last valid monitor check, maximum observed check gap, first exit trigger, failed exit-quote count, and last failure time. Old positions do not gain invented historical measurements. Valuation freshness remains separate. The execution fingerprint changes (see "Honest evidence" below), so old evidence is not treated as proof of the revised implementation.

## Entry risk filter

`entry-risk-shadow-v1` is a fixed exploratory filter, not a proven profitable strategy. It requires:

- No recorded creator rugs, at most four earlier creator launches, and at most 100 basis points creator tax.
- At most 50% sniping, 20% fleet share, and 50% top-ten holder concentration; at least 50 holders.
- A 15-minute return of at least −20% and a drawdown from the observed peak no worse than −40%.

Missing required measurements cause abstention. Thresholds were chosen after examining losses; retrospective results are exploratory and must not be marketed as an independently validated return. Graduation measurements can also be old by the entry time.

Each future second-look paper entry stores its original features and the versioned filter decision in `entry_shadow`. This does **not** reject the control trade. That allows comparison with the same realized, cost-adjusted outcome later. Freeze the filter during collection; changing it requires a new version and a new evaluation period.

## Offline evaluation

`scripts/evaluate-paper.py` accepts a private JSON export with a `rows` array. Rows contain `id`, `opened_ts`, `closed_ts`, `status`, `size_usd`, `pnl_usd`, `features`, `creator_group`, `quote_asset`, `execution_model`, `execution_spec`, `policy_spec`, and optionally `entry_shadow`. Use numeric features captured at entry, not a current token card. `creator_group` should consistently identify the same creator without exporting personal account information.

Providers are `rules` (exploratory replay), `recorded` (decisions actually stored at entry), and `laya` (local inference). Keep exports and results outside the repository. Run research in a separate environment without wallet or service credentials. Laya is intentionally absent from deployment dependencies. It requires a reviewed local model directory, runs offline on CPU, and abstains if the input cannot fit the model context.

The inference input is an explicit numeric allowlist. It contains no outcomes, profit figures, token marketing text or account secrets. Model probabilities are uncalibrated for this domain. An argmax choice is not a probability of profit.

Reports separate execution fingerprints, exit policies and quote assets. Open positions are excluded from closed P&L. The chronological diagnostic purges repeated creators and unsettled training labels; it is still not an untouched test set if its outcomes have already been inspected. Excluding a trade retrospectively does not simulate alternative use of its capital.

This filter is now under prospective test as its own rule, `shadow-keep-v1` (see below).

Before adopting any filter, collect new decisions before outcomes, compare against both the existing rules and an all-skip baseline, and require enough independent, settled, current-version USDG trades to pass the existing validation gates. Also measure coverage, missed winners, large losses, drawdown, missing-data frequency and execution failures. Rug classification requires its own explicit outcome labels; losing money is not automatically a rug.

## Honest evidence (September 26, 2026)

Nothing here enables trading or loosens the gate: 50 positions per cohort, the shared error budget and the lower-bound rule are unchanged, and `WH_TRADING`, `LIVE_SELL_READY` and the zero trading budget are untouched.

**What live could have traded.** Paper trades USDG and ETH pools; live buys from USDG pools only. Each paper row now reports its `pair` (read from the pool key it already stores) and whether it is `live_comparable`: a USDG pool, bought by a second-look rule at quoted fills, not opened during a loss pause. The paper summary publishes `live_comparable` and `all_pools` side by side (realized, closed count, wins, win rate, open count, per rule). The page shows the first as "could be traded for real" and the second as learning data. The validation gate already counted USDG rows only; that is unchanged.

**The fingerprint.** A cohort used to freeze a hash of twelve whole source files, so any edit voided every cohort in progress and each void took a new attempt. It now freezes values and code. The values are the rule, the exit policy, `trade_checks.evidence_spec()` (execution model, fill, slippage, both sell tolerances, the quote assets, gas, breaker and watch constants) and `strategy_validation.gate_spec()` (cohort size, total alpha, settlement grace, pass validity). The code is covered by digests of its syntax, computed at run time from the package's own source files: `trade_checks.EVIDENCE_CODE` (the code that picks, prices and closes a position, the breaker, the shadow filter) and `strategy_validation.GATE_CODE` (admission, settlement, evaluation, attempts). Comments, blank lines and docstrings do not move a digest; any edit to what the code does voids the cohorts it affects. Nothing is pinned by hand, so nothing can be re-pinned dishonestly. A test double or runtime patch cannot move it either, since the source is read from disk. `SEMANTICS` is a hand-bumped version for changes of meaning outside that code. A new Python minor version changes `ast.dump` and so voids every cohort once; that is accepted.

**Attempts.** Every counted attempt holds its own k in the budget `TOTAL_ALPHA/(k(k+1))`: an evaluated cohort, one still running, and one voided after anything of it could be seen. "Seen" means it had a member, its rule opened any paper position after its cutoff (USDG or not, because every open position's P&L is public), or the breaker turned away one of its rule's picks after it began (the skip and the lab case following it are public too). A cohort voided before that gives its k back. A new cohort takes the smallest k that no counted attempt holds. Why this cannot be gamed:

- Peeking and restarting still costs. Once a position exists its result is visible, so voiding afterwards keeps the attempt spent.
- The only free restart is one where nothing was visible. That carries no information, so it is the same as never having started.
- Counted attempts keep distinct k values, so the total false-pass probability across all rules and edits stays under 10%.
- A cohort is still judged once, at 50 closed members, with no replacement.
- Old trials migrate conservatively. Evaluated and running trials keep their k. A voided one keeps it when members, or positions of its rule, existed before it was voided. Nothing is passed or un-failed.

**The breaker.** Second-look entries now ask the loss breaker first, as live would. A USDG candidate answers to a breaker over the USDG-pool positions (including any flagged as opened in a pause: out of the evidence, not out of the losses). Any other candidate answers to the whole book's breaker. ETH losses therefore do not pause the evidence live is judged on. A turned-away candidate is recorded in `paper_skips` and followed by the lab. It is not a cohort member, which cannot select winners: the pause comes from trades that closed before the candidate existed, never from its own path, and live would have skipped it too. Positions opened during a past pause are flagged `opened_in_pause`, rebuilt once from closed losses plus the stored pause. That is a lower bound, because open marks were not kept. It uses the whole-book breaker then in force, which is conservative for USDG rows. Flagged rows stay in the book and its totals, and never count as live-comparable.

**The lab (`SIM_VERSION` 2).** The lab is a simulation that errs low:

- A take-profit fills at `min(level, sample)`; stops and trails fill at the sample.
- Every leg pays the paper book's own gas: the median of its recent positions, or the entry's own.
- A gain made while holding across a silence longer than 15 minutes is capped at +100%. Losses are never capped and no case is dropped, so data quality cannot select cases.
- A paper entry (or a breaker-skipped pick) gets a case of its own (key `token:entry`), priced every minute from its pool on-chain. The token's verdict case, priced by the price API, is left untouched and stays ranked: removing the cases a rule picked would remove them by outcome. No tick is deleted.
- A version 1 verdict case that got one pool reading at a paper entry keeps it, labelled `src='pool'` and left out of its path, and stays ranked. Only when that reading cannot be told apart (two readings within two minutes before the entry, which depends on timing, not outcome) is the case marked mixed and not ranked. A token can therefore appear twice in a ranking, as its verdict case and as its entry case.
- Version 1 sums are kept in `lab_arms_v1`. Recent cases are re-simulated from their stored ticks.

A USDG-only ranking sits next to the all-pools one. No ranking can choose the exit that new positions get. `current_policy` returns the code default until a paper cohort passes, and a test asserts it.

**`shadow-keep-v1`.** This is a filtered rule (`watch.FILTERED`) and buys nothing. Its members are positions the four entry rules open whose `entry-risk-shadow-v1` decision, recorded at entry, was keep. It runs its own cohort under the unchanged gate, on USDG pools only. Its frozen spec contains the four rules and the filter thresholds. The existing rules keep every entry and every cohort member they had.


## Routing: every pair can be evidence (September 28, 2026)

Paper fills used to be the token's own pool quote, and only USDG pools counted as what live
could have done (3 of the last 30 second-look trades). Now every paper entry and exit spends
or receives USDG through the best checked route (see TRADING-HARDENING.md, "Routing any pair
from USDG"): KyberSwap, LI.FI or the token's own USDG pool, with Relay compared only. The
fill is the route's quote less 1% a side, plus gas, and the round trip counts every hop,
creator tax, impact and aggregator fee. Paper and live choose from the same providers.

- **Eligibility.** A token is live-comparable when a route within the caps exists (15% band
  around the pool's mid, 1% aggregator fee, the 8% impact and 15% round-trip limits), not
  when it is paired with USDG. The watcher now follows every Pons pool, stock-paired ones
  included; their mids are converted through ETH's or the stock's own price (read-only;
  decimals read once from the token).
- **Fallback.** When every aggregator is down, a USDG pool is still its own route. An ETH
  pool is quoted directly as before, charged a further 1% for the missing USDG leg, and
  marked `live_fill=0`: learning data, never live-comparable, never a cohort member. A stock
  pool is not traded then.
- **Recorded on every position:** `pair` (USDG, ETH or the stock's symbol), `route_provider`,
  `live_fill`, `route` (the chosen quote and what the others offered), `exit_provider` and
  `fallback_fills` (exit fills live could not have made; the row is kept either way, since
  an outage says nothing about the token's outcome). The summary adds `by_pair` to both
  books and `routes` (each provider's breaker).
- **Breaker.** The live-comparable breaker (`scope='live'`) counts every position whose fill
  live could have made, any pair, and honours a pause the old USDG-only breaker still holds.
- **Gate.** Unchanged: 50 positions, the shared error budget, the lower-bound rule. It now
  admits routed fills in any pair (`trade_checks.live_fill`) instead of USDG pools only.
- **Fingerprint.** `SEMANTICS='paper-evidence-4'`, `MODEL='routed-quote-v1'`, the route rules
  in the evidence spec and the routing code in `EVIDENCE_CODE`. Deploying voids every running
  cohort once (an attempt is kept by any cohort whose evidence was already visible). This
  is intended: the fills changed meaning.
- **Expected effect.** On the last 30 second-look trades, 27 were ETH pools: roughly ten
  times as many live-comparable positions, plus the stock-paired graduations (about one in
  eight) now watched at all, so a cohort can fill in weeks instead of months. Lab rankings
  are unchanged (the lab's USDG table stays USDG-only).

After the independent review (same day): a position live could hold now exits exactly as
live could (the same routes and the direct exit, nothing else). When none answers the step
waits, as live's would; the ETH-pool fallback with its haircut is kept only for rows that
are learning data anyway, and a member with any fill live could not have made is settled
invalid (never counted, never replaced). Exit and valuation quotes read the gas price and
the pool first and wait at most 3 s for the aggregators; valuations ask KyberSwap and the
pool only. The paper breaker counts a position whose quote is stale at its fresh pool mid
less 15% instead of pausing every entry. Every entry needs a direct exit, so no position can
overrun its maximum age for want of an aggregator. These change the fingerprint again; the
cohorts void once on deploy either way.
