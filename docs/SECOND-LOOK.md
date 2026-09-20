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

A read-only study of about 370 graduated pools (public data only: the worm's own published
assessments, one-minute candles, and every swap of the first hours read from the chain),
with each token's own costs and honest monitoring (a position is only ever sold at a
price a poller would really have seen), found:

- Nothing measured at graduation separates winners from losers. Every one of the sixteen
  rules has a lift between 0.95 and 1.05, and several point the wrong way on this chain.
- Buying at the verdict, or anywhere in the first hour, lost 15 to 25 percent a trade under
  every exit rule tried. The first minutes after graduation are the snipers' exit.
- A pool that is still busy hours later is being sold into: entries there lost 15 to 20
  percent, and buying a new high on rising volume was the worst entry of all.
- A round trip costs 2 percent on a token without creator tax and up to 10 percent on a
  token with a 4 percent tax, before any price move.
- Exits matter less than entries: on the same entries every reasonable stop-and-trail
  setting landed within about three points of every other, and all beat holding by more than
  twenty. Polling every 15 to 60 seconds was worth two to three points a trade over polling
  every five minutes.
- No entry rule tested kept an edge as the sample grew. Every region lost money after
  costs; the least bad sat between −5 percent and zero.

So there is no proven strategy here, and this update does not pretend otherwise. It
rebuilds the paper book so that it can find out, prospectively and at no cost, and it
makes the dormant live path safe and coherent for the day the evidence exists.

## The second look

Nothing is bought at the verdict any more. Every complete verdict (any score; the worm's
own token never) goes on a watch list. The pool's mid price is read from the chain once a
minute (one batched storage read of the Uniswap v4 PoolManager for the whole list), and a
pool whose liquidity is gone (97 percent down half an hour after its verdict) is dropped. At fixed
looks (half an hour, two hours and four hours in) the path since the verdict is judged by named entry rules, together with the pool's
own swap flow over the last fifteen minutes (count, buy share, USD volume, read from the
pool's logs). The first rule that passes buys once, on paper, at the pool's own quote.
USDG and ETH pools are both traded on paper; other pair assets are not.

The rules in force are hypotheses, the least bad regions the studies found. All are
expected to fail their cohorts; they are there to be proven wrong or right in the open:

| rule | look | buys when |
|---|---|---|
| `quiet-v1` | 2 hours | creator tax at most 1 percent, creator not a serial launcher, between 1 and 19 swaps in the last 15 minutes (still traded, no longer churned by bots); a fixed third of such tokens, chosen by token address, so the book is not flooded |
| `runner-v1` | 4 hours | creator tax at most 1 percent, not a serial launcher, worth at least 100,000 dollars fully diluted (about twice its value at graduation), still traded |
| `clean-crowd-v1` | 30 minutes | creator tax at most 1 percent, not a serial launcher, at most 5 percent of the curve bought by wallets whose earlier picks all went bad (and at least 150 resolved tokens on record to say so), the pool's price still moving; only the two thirds of tokens the quiet rule never takes. The one rule that looks inside the first hour: what the crowd's record says is worth something early and nothing after the second hour |
| `holders-v1` | 2 and 4 hours | creator tax at most 1 percent, not a serial launcher, the ten biggest holders at the verdict (contracts left out) still hold 80 percent or more of what they held then, read from the chain at the look, the pool's price still moving |

What each look measured is stored on the paper row. Evidence is only ever pooled per rule
name; changing a rule means renaming it.

## The crowd's record (the wallet study)

"Buy what the wallets that were early in winners buy" was tested on 1,451 graduations. A trading bot's
router buys in its own name, so first every one of 93,216 such buys among the early curve buys was traced
to the wallet behind it (40,420 wallets in all). Records were built walk-forward: a wallet's earlier pick
counts only once its 24-hour outcome was known. Result: a good record predicts nothing. Tokens bought
early by wallets with at least three earlier picks, 40 percent or more of them not bad, turned out not bad
13.1 percent of the time against 14.5 percent for all (chance alone does better 93 percent of the time),
and bought with the profit lock they lost 11 to 18 percent a trade at every entry time, like everything
else. The same for wallets with two or more earlier winners. Copying is not a strategy here.

The opposite is a signal, small and real: the share of the curve bought by wallets whose earlier picks
(three or more) all went bad. At 30 percent or more, 6 percent of tokens turned out not bad and none
grew; under 5 percent, 18 percent and 6 percent. As a filter it was worth 7 to 12 points a trade at
entries in the first hour and nothing after the second, and never a profit by itself: the typical trade
still lost, and the averages that look good are one or two enormous runners. It is used twice: the
`losing_crowd` rule on the card (a warning, and a small bonus for a clean crowd), and the
`clean-crowd-v1` entry rule above. Wallet records live in `wormhole/crowd.py`: buyers are the wallets
that ended up with the tokens, a pick is folded in when its outcome is resolved, and tokens scored
before buys were followed are re-read from the chain a few per cycle.

## Linked holders and concentration (the bubble-map study)

Every transfer of 1,453 graduated tokens up to the block of the verdict was read, and holders who had passed
tokens to each other were joined into groups, the way a bubble map draws them. Who counts as a person decides
everything: linked through routers, custodial bots and multisenders everybody is one group and the read says
nothing, so a wallet that passed tokens on in ten or more EARLIER tokens is a service, and once a group holds
10 percent of the circulating supply the chain is asked which of its members are contracts, those are dropped
and the group is measured again (without that step there is no signal at all: the groups that did well were
holders tied to lockers and vaults). Walk-forward, 1,244 judged tokens, base rate 14.2 percent not bad and 3.9
percent grew: a linked group of 10 percent or more was found on 53 tokens (4.3 percent), of which 4 (7.5
percent) turned out not bad and none grew; at 15 percent, 2 of 29 and none. The same in both halves of the
period, and about one chance in ten of being luck. The scorer's `linked_wallets` line shows it without points
(`wormhole/linked.py`); the records decide what it becomes. At the verdict, seconds after graduation, such
groups are rare; what the biggest holders do in the two hours after is being collected for a later look.

The same data turned the concentration rules around. Over the first day top-10 at half or more was not bad 23
percent of the time against 13 percent below 35 percent, more holders was worse, and tokens whose creator's
own group kept 10 percent or more did best of all (32 percent not bad, 9 percent grew, n = 78). Widely spread
supply at graduation is mostly bots that sell at once. `top10` and `deployer_hold` therefore carry no points
any more. They are still said on the card, with the plain warning that big holders can sell into everyone at
any time, because the records cannot see past a day; a creator holding 20 percent still bars a healthy verdict.

## Do the big holders stay? (the two-hour holder study)

For 1,426 graduations, every transfer sent or received by the 120 biggest holders at the verdict was read for the
two hours after graduation. Almost always they are flippers: the ten biggest keep a median 0.6 percent of their
tokens for two hours, and the big holders as a group sell more than they held (they keep buying and selling).
Trades entered at the two-hour mark were then compared by how much the ten biggest had kept. The 24-hour label is
no use for this (a rug in the first hour is in both the feature and the label); what counts is the trade entered
at the look, with the profit lock and all costs.

The more they kept, the better the trade did, step by step: the share of winning trades rose from 16 percent
(kept under 5 percent) through 19, 22, 22 and 28 percent to 46 percent when the ten biggest still held 80 percent
or more, and at the four-hour look the average went the same way (-13, -17, -8, -3, +5, +4 percent). That top
group is rare (about 4 percent of tokens). One trade per token, entered at the first look where the ten biggest
still held 80 percent: +1.8 percent a trade on 42 tokens (43 percent won), and on cheap tokens +11.4 percent on
18 (56 percent won), against -10 to -15 percent and 18 percent winners for everything else. 59 percent of those
tokens ended their first day not bad, against 15 percent of all. It is the best region any of the studies found,
and it is not proven: the first half of the period carried it (+13 and +32 percent) and the second half lost
(-9 and -10 percent), on very few trades. `holders-v1` tests it forward on paper. Wallets passing tokens to
other wallets during those two hours (the bubble-map picture at two hours) said nothing reliable about the trade.

## The profit lock

The default exit is now `lock_20`, the creator's design: nothing is sold into strength.
A stop sells everything at −30 percent. Once the position has been 20 percent up, a
trailing stop follows the peak: 15 percent below it, 20 percent once the peak passed 2x,
25 percent once it passed 4x. A position that has done neither within 12 hours is closed.
Three variants (`lock_20_tight`, `lock_20_wide`, `lock_50`) run beside it in the lab, which
now also takes every second-look entry as a case. Open paper positions are re-priced from
the chain every 15 seconds; an exit that triggers is filled at a fresh pool quote with gas,
never at the trigger level. The five-minute cycle still values every position from a bid,
and it too reads the price from the position's own pool: a price API can lag a thin pool by
one trade, which reads as a fall from the peak and sells a position that never fell.

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
