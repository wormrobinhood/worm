# Paper execution and entry-filter research

The paper book remains a control experiment. A losing trade stays in its history. No AI model or shadow filter in this change can enable live trading or send a transaction.

## Exit monitoring

Entries serialize separately from exits. Each position has its own lock, and optional valuation network calls run outside those locks. All available exits are checked before valuation begins. A late valuation cannot reopen a position or overwrite the value of a different remaining quantity; an older mid observation cannot overwrite a newer mark.

Paper exit quotes use a shared five-second read-only RPC budget with no retries, plus a fresh cached ETH price for gas accounting. Missing prices or failed quotes leave the position open and measurable; the next monitoring cycle retries. These are request time budgets, not hard process deadlines. They do not cap market losses. A pool can gap through a stop, lose liquidity, or become unquotable. Live transaction broadcasting and recovery retain their existing behavior.

The fast position-price batch also uses a five-second budget. Incomplete prices or paper exits do not refresh the position worker heartbeat. Existing health gates can therefore stop new entries when that worker remains unhealthy. The monitoring target is 15 seconds plus processing time, not guaranteed execution every 15 seconds.

New columns record the last valid monitor check, maximum observed check gap, first exit trigger, failed exit-quote count, and last failure time. Old positions do not gain invented historical measurements. Valuation freshness remains separate. The implementation fingerprint changes, so old evidence is not treated as proof of the revised implementation.

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

Before adopting any filter, collect new decisions before outcomes, compare against both the existing rules and an all-skip baseline, and require enough independent, settled, current-version USDG trades to pass the existing validation gates. Also measure coverage, missed winners, large losses, drawdown, missing-data frequency and execution failures. Rug classification requires its own explicit outcome labels; losing money is not automatically a rug.
