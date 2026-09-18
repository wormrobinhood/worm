# Second look, profit lock and paper cohorts: September 19, 2026

This update does not enable trading. `WH_TRADING` is unchanged, the code release gate
`LIVE_SELL_READY` remains false, and the new lifetime trading budget defaults to zero.
It replaces the parts of [trading hardening](TRADING-HARDENING.md) that describe paper
entries, the prospective cohort and live candidates; the execution, approval and recovery
rules there still apply unless this page says otherwise.

## What the records said

Two days after the last update the paper book had opened nothing and the validation
cohort stood at 0 of 30. Entries needed a **looks healthy** verdict of 70 or more on a
USDG pool. About 1 verdict in 120 is healthy, fewer than 1 graduation in 5 pairs with
USDG, and 10 of the 12 healthy verdicts on record had rugged.

A read-only study of about 300 graduated pools (public data only: the worm's own published
assessments, one-minute candles, and every swap of the first hours read from the chain),
with each token's own costs and honest monitoring (a position is only ever sold at a
price a poller would really have seen), found:

- Nothing measured at graduation separates winners from losers. Every one of the sixteen
  rules has a lift between 0.95 and 1.05, and several point the wrong way on this chain.
- Buying at the verdict, or anywhere in the first hour, lost 15 to 25 percent a trade under
  every exit rule tried. The first minutes after graduation are the snipers' exit.
- Buying a new high on rising volume was the worst entry of all (about −20 percent).
- A round trip costs 2 percent on a token without creator tax and up to 10 percent on a
  token with a 4 percent tax, before any price move.
- Exits matter less than entries: on the same entries every reasonable stop-and-trail
  setting landed within about three points of every other, and all beat holding by more than
  twenty. Polling every 15 to 60 seconds was worth two to three points a trade over polling
  every five minutes.
- No entry rule tested showed an edge that survived a second sample. The least bad
  families sat between −6 percent and zero.

So there is no proven strategy here, and this update does not pretend otherwise. It
rebuilds the paper book so that it can find out, prospectively and at no cost, and it
makes the dormant live path safe and coherent for the day the evidence exists.

## The second look

Nothing is bought at the verdict any more. Every complete verdict (any score; the worm's
own token never) goes on a watch list. The pool's mid price is read from the chain once a
minute (one batched storage read of the Uniswap v4 PoolManager for the whole list), and a
pool whose liquidity is gone (97 percent down half an hour after its verdict) is dropped. At fixed
looks the path since the verdict is judged by named entry rules, together with the pool's
own swap flow over the last fifteen minutes (count, buy share, USD volume, read from the
pool's logs). The first rule that passes buys once, on paper, at the pool's own quote.
USDG and ETH pools are both traded on paper; other pair assets are not.

The rules in force are hypotheses, the least bad of what the study tried:

| rule | looks | buys when |
|---|---|---|
| `survivor-v1` | 2, 3 and 4 hours | creator tax at most 1 percent, creator not a serial launcher, at least 20 swaps in the last 15 minutes, not lower than an hour ago, not up more than 10 percent in the last 15 minutes |
| `flush-v1` | 1 and 2 hours | no creator tax, not a serial launcher, fully diluted value at most 15,000 dollars, at least 10 swaps in the last 15 minutes |
| `wide-net-v1` | 2 hours | creator tax at most 2 percent, not a serial launcher, at least 20 swaps in the last 15 minutes |

`wide-net-v1` is the control group and the unbiased sample: what each look measured is
stored on the paper row, so the next round of research reads what production really saw.
Evidence is only ever pooled per rule name; changing a rule means renaming it.

## The profit lock

The default exit is now `lock_20`, the creator's design: nothing is sold into strength.
A stop sells everything at −30 percent. Once the position has been 20 percent up, a
trailing stop follows the peak: 15 percent below it, 20 percent once the peak passed 2x,
25 percent once it passed 4x. A position that has done neither within 12 hours is closed.
Three variants (`lock_20_tight`, `lock_20_wide`, `lock_50`) run beside it in the lab, which
now also takes every second-look entry as a case. Open paper positions are re-priced from
the chain every 15 seconds; an exit that triggers is filled at a fresh pool quote with gas,
never at the trigger level. The five-minute cycle still values every position from a bid.

Paper fills are booked at the pool's quote less 1 percent a side, plus estimated gas for the
swap and its approvals. The earlier model booked every fill at the quote less 3 percent,
which is a live order's revert bound, not what a fill a second after the quote plausibly
loses: it handicapped every round trip by six points before fees. The 3 percent bound (10
after a reverted sell) still protects live orders. What real fills lose against their quotes
is one of the things the funded rehearsal has to measure.

## Paper cohorts replace the sampled trial

A trial freezes one entry rule and the exit policy. Its members are the paper positions
that rule opens afterwards, the first per creator, in opening order, and a member's result
is what the book realised on quoted fills with gas. When the first 50 members have all
closed the cohort is judged once: it passes if the lower confidence bound of its mean
net return is above zero. All rules share one error budget of 10 percent, one-sided: the
k-th attempt (a rule's first cohort, or a retry after a failure or an edit) is judged at
10/(k(k+1)) percent, so the first gets 5, the second 1.7, the third 0.8, and trying more
rules or retrying until luck wins gets harder each time. The renewal of a standing pass is
judged at the bar it passed at. A member without a usable result fails
the cohort rather than being replaced. The next cohort starts at once, so a pass is renewed
by fresh evidence or expires after 21 days; editing the rule or the exit policy voids it.
These are paper fills against live quotes, not proof of executable profit at size.

Readiness follows: **strategy proven on paper** carries half the number and is the gate;
the verdict grade ("warnings right") still moves the number but no longer blocks or unlocks
anything. Runway and surplus are unchanged.

## The advisor's second measure

Of 243 proposals none had passed the historical screen, because it compared medians and
nearly every token falls about 85 percent, whatever a rule says. A rule is now screened on
either of two measures, each at half the old chance level: the median difference (10
points) or the difference in how often its tokens went bad (8 points; bad means −50
percent or worse). The measure that passed is frozen with the rule: the 80-token future
cohort and later re-validation use the same one.

## The dormant live pilot

Live follows paper. The only live candidate is a token the paper book bought in the last
two minutes under an entry rule whose paper cohort holds a fresh pass, and only from its
verified USDG pool. The watcher hands the trader that entry and fresh pool prices for its
open positions; every gate remains the trader's own, and all are closed by default.

- **Lifetime budget.** `WH_TRADING_BUDGET_USD` (default 0: no live buys) is the most the
  pilot may ever have at risk or lose. Open positions and lifetime net losses use it up;
  profits never refill it. It sits beside the per-position, per-day and daily-loss limits.
- **Profit to the burn.** Realised trading profit above a high-water mark becomes owed to
  the burn (`WH_TRADING_BURN_SHARE`, default all of it) and is spent by the usual
  buy-and-burn. After a drawdown nothing is swept until the old high is passed again.
- **No lock-ups.** `send_tx` now marks any failure before the key was used. Such an order
  is released, not held. An order whose hash was not stored is matched to the private
  journal by its calldata after a minute: found, it settles from its receipt; absent,
  nothing was ever signed and it is released. Only a confirmed receipt whose transfers do
  not match the intent is still held for the operator.
- **Exits that can act.** A settled buy stands its exact sell approval at once, so a stop is
  one transaction; trading receipts are polled every second; a sell that reverted is
  retried after 20 seconds with a 10 percent tolerance instead of waiting ten minutes at 3.

## What remains before any real trade

A passing paper cohort; an explicit budget; the funded exit rehearsal that
`LIVE_SELL_READY` stands for; and the operator's decision. A separate trading wallet would
isolate the pilot from the treasury's transaction stream and is recommended before real
money; it is not part of this update.
