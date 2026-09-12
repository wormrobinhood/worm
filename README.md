# IRL Worm

*Internet money, real life impact.* A small hooded worm on Robinhood Chain. It digs through every
[pons](https://www.ponsfamily.com/launchpad) graduation, looks for bad actors, tells the community
first, and grows longer with the treasury it holds.

Everything from scouting to trading is built and runs in demo mode: its own wallet, its own token $WORM on pons, fee claiming, a 60/20/20 split of every claim (its creator, a buyback that burns $WORM, its own operations), compute paid from its own balance, a strategy lab, a readiness gate and giving. Trading is off by policy (`WH_TRADING`) until the brain is mature. Nothing is signed unless `WH_LIVE=1`. Five independent code audits (chain indexer, scoring and learning, trading lab, money paths, server and web) were run before launch; their fixes shipped with an offline test suite of 300+ tests.

## What it does

- **Indexes pons V2** straight from the public RPC: `TokenLaunched` and `PoolGraduated` events on the
  factory, kept for 72 hours so creator history means something.
- **Scores every graduation** with named rules: creator history, the creator's rug record, creator tax,
  snipe-window buys, unique buyers (dust buys do not count), the creator buying its own curve, top-10 holder
  concentration, the creator's current holding, trades after graduation, missing socials,
  launch-to-graduation pace, throwaway buyer wallets, buyers funded by one wallet just before launch, and
  bot fleets that buy on every curve and sell at graduation (the patterns behind a fake crowd). Every point is explained on the card. Three hard signals demote a healthy
  verdict to mixed; a verdict on incomplete chain reads can never be healthy and is re-checked.
- **Learns from outcomes.** Each verdict is checked at 1h, 6h and 24h (a rug needs two readings 5 minutes
  apart at -80%, or the 24h check; grew is +100%). Rules are re-weighted against the base rate: a rule that
  warned before an unusually bad outcome gains, one that reassured loses, up to 2% a lesson; weights stay
  between 0.5x and 1.5x, and each rule shows its lift.
- **Trust value per creator**, shown on cards and in the bad-actors table.
- **Paper book**: what it would have bought at $10 a token, marked to market. No real money.
- **Strategy lab**: every candidate scoring 60 or more is traded on paper by 24 arms at once (8 exit
  policies × entry delays of 0, 30 and 60 minutes) on the same sampled price path for 48 hours, with each
  token's own costs (creator tax + curve fee + 1% slippage a side). Arms keep a running net return per
  dollar risked. An arm needs 30 cases, a lower confidence bound above zero and a mean above the default's
  to become the policy the paper book and the trader use; exploration is off (`WH_LAB_EXPLORE`). Until
  then the default is `costout_1.5x@0m`: at +50% sell two thirds (the cost comes back), trail the rest 40%
  below its peak, cut at -35% before that, never hold past 48 hours.
- **Advisor**: every two hours (`WH_ADVISOR_EVERY_MIN`), once a few more verdicts have resolved, the writer
  (`WH_ADVISOR_MODEL`, default the voice's model) reads the worm's records and proposes scoring rules (up to
  three conditions over at-scan metrics plus points) and exit arms in a strict JSON form. Nothing it says runs
  as code: every rule is backtested on the worm's own resolved verdicts and adopted only if the tokens it fires
  on moved clearly differently from the rest (a median difference of 10 points that fewer than 2 in 100 random splits would show, 20 cases each side)
  and it is not a copy of an existing rule; arms join the lab and earn their use there. With the stub writer
  the worm runs its own one-metric threshold search through the same gate. Proposals, backtests and fates are
  kept and shown.
- **Readiness**: one evidence-only number (exit rule proven, warnings right against the base rate, runway,
  surplus) that has to reach 80 before the trader may buy; demo money counts for nothing.
- **Runway**: measures income from claimed fees in the ledger, estimates the planned bills (compute, gas,
  bridge) and projects 90 days under three scenarios. Rule: keep a 90-day reserve; compute, gas and
  bridging come only from the operations share of the claims; no trading by policy; give only from the surplus.
- **Live page** in green on black, the worm drawn in 0 and 1. Its screen panel streams the worm's own
  browser: every dig step by step and, when idle, the newest graduation, the launchpad's newest launches and
  the curves with the biggest market caps, which is where the next graduation comes from (the launchpad's own
  Graduated grid sorts by market cap, so it would show the same big five forever).
  A small copy of the worm lives in the panel and crawls to whatever it is reading.

## Run it

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python run.py                 # http://127.0.0.1:4670
python run.py --once 5        # backfill, score the 5 newest graduations, print them, exit
```

Optional environment (see `.env.example`): `WH_DEMO_TREASURY` fakes a treasury to look at the growth stages
(it never counts for runway, readiness, giving or trades).

Deploying: the image sets `WH_DATA_DIR=/data`; on Railway attach a persistent volume at `/data` (Railway rejects
a Docker `VOLUME` line), otherwise every redeploy starts from an empty database. The entrypoint starts as root
only to take ownership of the mounted volume, then runs the worm as the unprivileged user `worm` (uid 1000).
`/healthz` answers 503 once the indexer has not advanced for 3 minutes, and the process exits after 15 minutes
of stall so the host restarts it.

## Phase 2 commands

```bash
python -m wormhole.wallet new        # create the worm's key in .env (never printed), show the address
python -m wormhole.wallet show       # balances on Robinhood Chain and Base
python -m wormhole.logo              # render web/logo.png (the token logo)
python -m wormhole.launch            # dry run of the $WORM launch on pons (simulated, nothing sent)
WH_LIVE=1 python -m wormhole.launch --live   # the real launch: 0.0005 ETH fee + gas
```

The launch refuses to run unpinned (when the factory's economics preview fails) unless `--unpinned` is given,
writes `WH_TOKEN_PENDING_TX` to `.env` at broadcast and refuses a second launch while it is set, then writes
`WH_TOKEN` on success. Every send goes through one locked sender that computes the hash locally, never
re-broadcasts blindly, and records a pending ledger row before waiting for the receipt.

Token settings live in `.env`: `WH_TOKEN_NAME`, `WH_TOKEN_SYMBOL`, `WH_TOKEN_X`, `WH_TOKEN_TELEGRAM`, `WH_CREATOR_TAX_BPS`
(default 200 = 2%), `WH_SITE_URL` (logo and website links). `WH_OWNER_WALLET` receives `WH_OWNER_SHARE` (default 0.60)
of every fee claim; `WH_BURN_SHARE` (default 0.20) buys $WORM on its pool and sends it to the burn address; the rest is
the worm's operations money. The live screen (`WH_SCREEN=1`, default on) needs Chromium: `python -m playwright install chromium`.

## Verified addresses (chain 4663)

| | |
|---|---|
| pons V2 factory | `0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e` |
| pons meme hook | `0xe5e702641ea86f4ae6cc3cdaed2b886f976be044` |
| Uniswap v4 PoolManager | `0x8366a39cc670b4001a1121b8f6a443a643e40951` |
| Universal Router | `0x8876789976decbfcbbbe364623c63652db8c0904` |
| USDG | `0x5fc5360d0400a0fd4f2af552add042d716f1d168` |
| pons fee escrow (creator fees, `claimToken(USDG)`) | `0xd3afeb2a57f70ef218aa82451c51b2fb0416ac9e` |

## What is not real

The verdicts are rules written by a person and re-weighted by outcomes. They are a screening aid, not
an audit and not advice. The paper book is hypothetical. The worm has bought nothing.

## Demo mode and going live

Everything runs with `WH_LIVE=0` by default: the worm scores, writes its journal, quotes and simulates
buys, plans compute top-ups and giving, and writes every decision as "would". Flip `WH_LIVE=1` and
fund the wallet to make the same code sign and send. Phase 3 to 5 settings:

| variable | meaning |
|---|---|
| `WH_VOICE_MODEL` | `stub` (free templates), `venice:<model>` (its own balance), `anthropic:<model>` (ANTHROPIC_API_KEY), `openai:<model>` (WH_LLM_BASE_URL + WH_LLM_API_KEY) |
| `WH_VOICE_EVERY_MIN` | minutes between journal entries (120) |
| `WH_TOPUP_USD`, `WH_TOPUP_BELOW_USD` | compute top-up size and threshold at Venice (5, 1) |
| `WH_TOPUP_COOLDOWN_S`, `WH_TOPUP_MAX_PER_DAY`, `WH_TOPUP_ALWAYS` | at most one top-up per 6 h and two a day (21600, 2); top-ups only happen while the journal runs on Venice unless `WH_TOPUP_ALWAYS=1` |
| `WH_LAB_MIN_N`, `WH_LAB_LCB_Z`, `WH_LAB_EXPLORE` | cases an exit rule needs (30), the lower-bound factor (1.5), exploration share (0) |
| `WH_READY_AT` | readiness needed before real trades (80) |
| `WH_ADVISOR_MODEL`, `WH_ADVISOR_EVERY_MIN`, `WH_ADVISOR_MIN_NEW` | the advisor's writer (default: the voice's), minutes between runs (120), new resolved verdicts a run needs (3) |
| `WH_RESCAN_TOKEN` | lets a remote caller use `/api/rescan/<token>` by sending the header `X-Rescan-Token`; unset, only loopback clients may rescan (the queue is capped at 100 and an address is not queued twice within 10 minutes) |
| `WH_BUY_MIN_SCORE`, `WH_MAX_POSITION_USD`, `WH_MAX_OPEN`, `WH_MAX_DAILY_USD` | trader limits (70, 10, 5, 30) |
| `WH_OWNER_SHARE`, `WH_BURN_SHARE` | of every claim of creator fees: forwarded to the creator (0.60) and spent buying $WORM on its pool and sending it to the burn address (0.20); the rest is operations |
| `WH_TRADING` | real-trading switch, off by default (0): the worm learns on paper until its brain is mature; on, the readiness gate still applies |
| `WH_CAUSES`, `WH_GIVE_SHARE` | `Name\|0xaddr\|weight,...` and the share of the surplus given every 30 days (0.10) |
| `WH_DEMO_TREASURY` | pretend treasury so the growth, runway, trader and giving logic can be watched before funding |

Posting to X is manual on purpose: entries sit on the site with a copy button.

## Roadmap

1. Signals and the live page (this).
2. Its own token on pons, paired with USDG, so creator fees fund it.
3. The voice: journal entries from telemetry, checked like the fly's; compute paid from its own Venice balance. Built, demo mode.
4. Buys from the surplus above the 90-day reserve with lab-chosen exits, quoted and simulated on Uniswap v4. Built, demo mode, off by policy (`WH_TRADING`) until the brain is mature;
   real buys stay off until real sells exist (the Permit2 approval step and the exit path), and every buy is written
   down before it is sent and reconciled against the chain afterwards.
5. Causes: giving from the surplus, on-chain and public. Built, demo mode.

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
