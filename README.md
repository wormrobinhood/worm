# WORM

*Knows the dirt on every launch.*

WORM is an on-chain scout for Robinhood Chain. It investigates Pons graduations,
examines holders and creator history, and flags risk signals. Its memory tracks what
happens next, while AI proposes improvements and paper trading tests strategies.

The live website is linked in this repository’s About section.
[Read how the engine works](docs/ENGINE-EVIDENCE.md).

## Launch status

The website and source code are public. The official $WORM token launch is scheduled
for **September 17, 2026 at 17:39:40 UTC**. Follow the website countdown and live
status for execution progress: at zero, WORM begins its launch checks. Transaction
confirmation and the creator allocation follow. Discretionary trading remains off.

The launch plan includes an initial purchase of 2% of supply: 1% retained by WORM and
1% transferred to its creator. Of claimed fees, 50% go to the creator, 10% buy GLD
reserves, 20% buy back and burn $WORM when a supported pool is available, and 20% fund
compute, gas, runway, and treasury. Buying tokens for the initial allocation or fee
buybacks is separate from the disabled discretionary trading system.

Bounded rehearsals used separate test tokens and wallets to verify launch and treasury
paths. The confirmation release passed 622 offline tests and an actual-service
outage/crash/restart drill with disposable local EVM funds. These checks are not a
third-party security certification or a guarantee of uninterrupted operation. See
[launch readiness](docs/LAUNCH-READINESS.md) and [payment recovery](docs/PAYMENT-RECOVERY.md)
for the evidence, remaining operational gaps and deployment requirements.

## What it does

- **Indexes pons V2** straight from the public RPC: `TokenLaunched` and `PoolGraduated` events on the
  factory, kept for 72 hours so creator history means something.
- **Scores every graduation** with named rules: creator history, the creator's rug record, creator tax,
  snipe-window buys, unique buyers (dust buys do not count), the creator buying its own curve, top-10 holder
  concentration, the creator's current holding, trades after graduation, missing socials,
  launch-to-graduation pace, throwaway buyer wallets, buyers funded by one wallet just before launch, and
  bot fleets that buy on every curve and sell at graduation (the patterns behind a fake crowd). A buy made
  through a trading bot's router is followed to the wallet that received the tokens in the same transaction,
  so the people behind a bot are counted as people and the bot is never a whale or a fleet. Every buyer's earlier picks are
  kept on record once their outcomes are known: a curve bought by wallets whose picks all went bad is a warning (a good record
  was tested and predicts nothing). Holders who passed tokens to each other are read as one group, the way a bubble map draws them
  (shown without points until the records say what it is worth); concentration is shown without points too, because over a token's
  first day it pointed the other way, and a creator holding 20% still bars a healthy verdict. Every point is explained on the card. Three hard signals demote a healthy
  verdict to mixed; a verdict on incomplete chain reads can never be healthy and is re-checked.
- **Learns from outcomes.** Each verdict is checked at 1h, 6h and 24h (a rug needs two readings 5 minutes
  apart at -80%, or the 24h check; grew is +100%). Rules are re-weighted against the base rate: a rule that
  warned before an unusually bad outcome gains, one that reassured loses, up to 2% a lesson; weights stay
  between 0.5x and 1.5x, and each rule shows its lift.
- **Trust value per creator**, shown on cards and in the bad-actors table.
- **Paper book**: nothing is bought at the verdict. Every complete verdict is watched on-chain (the pool's
  mid once a minute, its swap flow at each look) and judged again half an hour, two hours and four hours later by named entry
  rules; a pass buys $10 on paper at the pool's own quote (USDG and ETH pools, gas and liquidity checks
  included). Open positions are re-priced from the chain every 15 seconds. The rules are hypotheses under
  test, not a proven strategy: see [second look](docs/SECOND-LOOK.md) for the study behind them.
- **Strategy lab**: 36 baseline arms (12 exits × 0/30/60-minute delays) plus AI proposals compare sampled
  48-hour price paths of verdicts scoring 60 or more and of every second-look entry. The ranking informs;
  it never promotes. The default exit is the profit lock: stop at −30%, a trailing stop that arms at +20%
  (15% below the peak, 20% past 2x, 25% past 4x), closed after 12 hours if it has done neither.
- **Paper cohorts**: the gate before real money. A trial freezes one entry rule and the exit policy; the
  paper positions that rule opens afterwards (the first per creator) are its members, judged once on their
  realised, quoted results when 50 have closed. All rules share one error budget, a pass is renewed by the
  next cohort or expires, and editing the strategy voids it. See [second look](docs/SECOND-LOOK.md) and
  [trading hardening](docs/TRADING-HARDENING.md).
- **Advisor**: every two hours (`WH_ADVISOR_EVERY_MIN`), once a few more verdicts have resolved, the writer
  (`WH_ADVISOR_MODEL`, default the voice's model) reads the worm's records and proposes scoring rules (up to
  three conditions over at-scan metrics plus points) and exit arms in a strict JSON form. Nothing it says runs
  as code: every rule is backtested on the worm's own resolved verdicts and adopted only if the tokens it fires
  on moved clearly differently from the rest (a median difference of 10 points, or a difference of 8 points in how often they went bad, that fewer than 1 in 100 random splits would show, 20 cases each side; the measure that passed is frozen for the 80-token future cohort)
  and it is not a copy of an existing rule; arms join the lab and earn their use there. With the stub writer
  the worm runs its own one-metric threshold search through the same gate. Proposals, backtests and fates are
  kept and shown.
- **Readiness**: one evidence-only number (strategy proven on paper, warnings right against the base rate,
  runway, surplus) that has to reach 80, with a fresh pass of a paper cohort, before the trader may buy; demo
  money counts for nothing.
- **Runway**: measures income from claimed fees in the ledger, estimates the planned bills (compute, gas,
  bridge) and projects 90 days under three scenarios. Rule: keep a 90-day reserve; compute, gas and
  bridging come only from the operations share of the claims; no trading by policy.
- **Live page** in green on black, the worm drawn in 0 and 1. Its screen panel streams the worm's own
  browser: every dig step by step and, when idle, the newest graduation, the launchpad's newest launches and
  the curves with the biggest market caps, which is where the next graduation comes from (the launchpad's own
  Graduated grid sorts by market cap, so it would show the same big five forever).
  A small copy of the worm lives in the panel and crawls to whatever it is reading.

## Run it

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements.lock
python run.py                 # http://127.0.0.1:4670
python run.py --once 5        # backfill, score the 5 newest graduations, print them, exit
```

Optional environment: see `.env.example`.

Deploying: the image sets `WH_DATA_DIR=/data`; on Railway attach a persistent volume at `/data` (Railway rejects
a Docker `VOLUME` line), otherwise every redeploy starts from an empty database. The entrypoint starts as root
only to take ownership of the mounted volume, then runs the worm as the unprivileged user `worm` (uid 1000).
`/healthz` answers 503 once the indexer has not advanced for 3 minutes, and the process exits after 15 minutes
of stall so the host restarts it.

## Operator commands

```bash
python -m wormhole.wallet new        # create the worm's key in .env (never printed), show the address
python -m wormhole.wallet show       # balances on Robinhood Chain and Base
python -m wormhole.logo              # render web/logo.png (the token logo)
python -m wormhole.launch            # dry run of the $WORM launch on pons (simulated, nothing sent)
WH_LIVE=1 python -m wormhole.launch --live   # the real launch: 0.0005 ETH fee + gas
```

The launch checks factory economics and refuses duplicate or unresolved launches. Signed
transactions and pending bookkeeping are saved durably before submission. Recovery checks
the existing hash and may rebroadcast only the original signed bytes; it does not create a
new payment merely because an RPC reply was lost. Successful included receipts require
matching transaction and event evidence; optional finalized settlement remains available.
See [initial allocation](docs/INITIAL-ALLOCATION.md) before using the 2% launch/buy path.

Token settings live in `.env`: `WH_TOKEN_NAME`, `WH_TOKEN_SYMBOL`, `WH_TOKEN_X` (also linked from the pages), `WH_TOKEN_TELEGRAM`, `WH_CREATOR_TAX_BPS`
(default 200 = 2%), `WH_SITE_URL` (logo and website links). `WH_OWNER_WALLET` receives `WH_OWNER_SHARE` (default 0.50)
of every fee claim; `WH_GOLD_SHARE` (default 0.10) buys tokenized gold (GLD) the worm keeps as a reserve that the runway never
counts; `WH_BURN_SHARE` (default 0.20) buys $WORM on its pool and sends it to the burn address; the rest is
the worm's operations money. The local non-live screen needs sandboxed Chromium: `python -m playwright install chromium`. Live execution uses an isolated browser service through `WH_SCREEN_CDP_URL`; see [payment recovery and deployment](docs/PAYMENT-RECOVERY.md).

## Verified addresses (chain 4663)

| | |
|---|---|
| pons V2 factory | `0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e` |
| pons meme hook | `0xe5e702641ea86f4ae6cc3cdaed2b886f976be044` |
| Uniswap v4 PoolManager | `0x8366a39cc670b4001a1121b8f6a443a643e40951` |
| Universal Router | `0x8876789976decbfcbbbe364623c63652db8c0904` |
| USDG | `0x5fc5360d0400a0fd4f2af552add042d716f1d168` |
| pons fee escrow (creator fees, `claimToken(USDG)`) | `0xd3afeb2a57f70ef218aa82451c51b2fb0416ac9e` |

## Reading the results

Scores are screening assessments, not audits or guarantees. Rules are adjusted using
observed outcomes; AI proposals pass validation before adoption. Paper trades are
simulated, and runway figures are projections. Rehearsal transactions used separate
test tokens and wallets and do not mean the official $WORM token is already live.

## Demo mode and going live

With `WH_LIVE=0`, WORM can scan, learn, quote and display its decisions without signing
chain transactions. Live operation additionally requires a funded dedicated wallet,
private credentials, reviewed limits, durable state and an explicitly chosen launch
schedule. Keep `WH_TRADING=0` until the operator separately authorizes trading and its
execution gates are satisfied. Follow [launch readiness](docs/LAUNCH-READINESS.md)
before changing production execution settings.

Selected settings:

| variable | meaning |
|---|---|
| `WH_VOICE_MODEL` | `stub` (free templates), `aisurplus:<model>` (its own AI Surplus balance, paid in USDG on Robinhood Chain), `venice:<model>` (its own Venice balance, USDC on Base), `anthropic:<model>` (ANTHROPIC_API_KEY), `openai:<model>` (WH_LLM_BASE_URL + WH_LLM_API_KEY) |
| `WH_COMPUTE_PROVIDER` | `aisurplus` (default: paid with plain USDG transfers on Robinhood Chain, nothing bridged) or `venice` (USDC on Base through x402) |
| `WH_OPS_TOKEN` | required for every launch trigger (`POST /api/launch`, header X-Ops-Token), including localhost; unset: launch control disabled. Nothing signs unless `WH_LIVE=1` |
| `WH_AISURPLUS_FALLBACK` | the paid model used when the free open lane is out of its shared weekly quota (`gpt-5.6-luna`); empty disables the fallback |
| `WH_AISURPLUS_KEY`, `WH_AISURPLUS_DEPOSIT` | the key minted at aisurplus.io/app/keys (a secret, popped from the environment like the wallet key) and the deposit address aisurplus.io/app/wallet shows for Robinhood Chain |
| `WH_VOICE_EVERY_MIN` | minutes between journal entries (60) |
| `WH_TOPUP_USD`, `WH_TOPUP_BELOW_USD` | compute top-up size and threshold (5, 1) |
| `WH_TOPUP_COOLDOWN_S`, `WH_TOPUP_MAX_PER_DAY`, `WH_TOPUP_ALWAYS` | at most one top-up per 6 h and two a day (21600, 2); top-ups only happen while the journal or the advisor runs at the provider, and never while every AI Surplus model in use is free, unless `WH_TOPUP_ALWAYS=1` |
| `WH_LAB_MIN_N`, `WH_LAB_LCB_Z`, `WH_LAB_EXPLORE` | research cases (30), ranking bound factor (1.5), paper exploration share (0); separate prospective validation is mandatory for promotion |
| `WH_READY_AT` | readiness needed before real trades (80), plus a fresh pass of a paper cohort and execution gates |
| `WH_TRADING_BUDGET_USD` | the lifetime trading budget (0: no live buys): the most the pilot may ever have at risk or lose; losses use it up and profits never refill it |
| `WH_TRADING_BURN_SHARE` | share of realised trading profit above its high-water mark that is owed to the burn (1.0) |
| `WH_PAPER_MAX_OPEN` | open paper positions at once (25) |
| `WH_MAX_DAILY_LOSS_USD` | gross-loss trigger for pausing new entries (10); exits continue; not a guaranteed loss ceiling |
| `WH_ADVISOR_MODEL`, `WH_ADVISOR_EVERY_MIN`, `WH_ADVISOR_MIN_NEW` | the advisor's writer (default: the voice's), minutes between runs (120), new resolved verdicts a run needs (3) |
| `WH_RESCAN_TOKEN` | lets a remote caller use `/api/rescan/<token>` by sending the header `X-Rescan-Token`; unset, only loopback clients may rescan (the queue is capped at 100 and an address is not queued twice within 10 minutes) |
| `WH_MAX_POSITION_USD`, `WH_MAX_OPEN`, `WH_MAX_DAILY_USD` | trader limits (10, 5, 30); `WH_BUY_MIN_SCORE` (70) only applies to the legacy verdict-time paper entry |
| `WH_OWNER_SHARE`, `WH_GOLD_SHARE`, `WH_BURN_SHARE` | of every claim of creator fees: forwarded to the creator (0.50), spent on tokenized gold kept as a reserve (0.10), spent buying $WORM on its pool and sending it to the burn address (0.20); the rest is operations |
| `WH_TRADING` | real-trading switch, off by default (0): the worm learns on paper until a strategy is proven there; on, the readiness gate, the paper cohort, the budget and the sell release gate still apply |

Posting to X is manual on purpose: entries sit on the site with a copy button.

## Roadmap

1. Signals and the live page (this).
2. Its own token on pons, paired with USDG, so creator fees fund it.
3. The voice: journal entries from telemetry, checked like the fly's; compute paid from its own balance at AI Surplus in USDG on Robinhood Chain, with Venice on Base as the fallback. Built, demo mode.
4. Buys from the surplus above the 90-day reserve, within a lifetime budget, following the paper book: only a token it
   has just bought under an entry rule whose paper cohort passed, with realised profit above a high-water mark going to
   the burn. Built and dormant: off by policy (`WH_TRADING`), without a budget, and behind the sell release gate until a
   funded rehearsal; every order is written down before it is sent and reconciled against the chain afterwards.

## The page

`web/index.html` is the live page: one document with four views chosen by the URL hash, Live (`#overview`),
Learning (`#learning`), Token scans (`#scans`) and Treasury (`#treasury`). The page's inline script reads
`/api/state` and the `/ws` socket and renders every panel; `web/design.js` adds the navigation, the search,
filter and sort of the scans, the evidence disclosures, the data-freshness label, the interactive learning map,
the motion control, the five-entry scroll regions and the launch tape; `web/design.css` is the look. The server
serves those two files at `/design.css` and `/design.js` by exact name (nothing else under `web/` is reachable),
with `Cache-Control: no-cache` and a content ETag, so a new build shows with the page. The socket is always
opened on the page's own origin, `wss:` on HTTPS and `ws:` on HTTP; a test refuses any other host or port.

Every number is the server's: the at-scan FDV is the first price the feed returned for that verdict and is never
overwritten (`fdv0_usd`, with `price0_ts` saying when it was read), the FDV now is the latest refresh, and a value
the feed has not returned yet shows as not read, never as zero.

## The docs page

`/docs` explains the whole thing in plain words: how a dig works, the trust values, how the brain learns,
the strategy lab, the readiness gate, the fee split, the runway policy, the token, security, and the roadmap.
Set `WH_REPO_URL` once the public repository exists and the page links to it.

## Tests

Offline tests live in `tests/` and never touch the real wallet, `.env` values, the chain, or `data/`: the
test setup swaps in a throwaway key and a temporary data folder before anything is imported.

```bash
.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q tests
```

## Keeping secrets out of git

The wallet key and every other secret live only in `.env`, which git ignores. Three hooks in
`.githooks/` run `scripts/leakcheck.py` before every commit, on every commit message, and before
every push. They refuse:

- files that must never be tracked (`.env`, databases, logs, key files, anything in `data/`)
- every value of the local `.env`, searched byte for byte in every file, images included
- private-key shapes (64-hex unless allow-listed as a public hash), PEM keys, GitHub / OpenAI /
  Anthropic / Slack / AWS tokens, seed phrases
- personal terms from the git-ignored `.leakcheck.local`, in files, commit messages, author
  names and emails, and the push URL, plus home-directory paths
- a placeholder author email at push time

After cloning, enable the hooks once and run the full scan by hand whenever you like:

```bash
git config core.hooksPath .githooks
python3 scripts/leakcheck.py --all
```

Public hashes that look like keys go in `scripts/leakcheck_allow.txt`, one per line, only after
proving they are public.

## Payment safety changes

Before deploying the recovery changes, follow [PAYMENT-RECOVERY.md](docs/PAYMENT-RECOVERY.md): back up both databases, explicitly migrate any historical claim policy, configure authenticated operations and the isolated browser, and verify the final environment. Unknown payments stay reserved until verified; paper learning can continue.

For the security changes and server-triggered launch countdown, see [Claude handoff](CLAUDE_HANDOFF.md). The authenticated server schedule controls execution; the website reports its current status.
