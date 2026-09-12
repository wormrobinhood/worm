# IRL Worm

*Internet money, real life impact.* A small hooded worm on Robinhood Chain. It digs through every
[pons](https://www.ponsfamily.com/launchpad) graduation, looks for bad actors, tells the community
first, and grows longer with the treasury it holds.

Phase 1: read-only signals. Phase 2 (in progress): its own wallet, its own token $WORM on pons, fee claiming, and a 20% forward to its creator. Nothing is signed unless `WH_LIVE=1`.

## What it does

- **Indexes pons V2** straight from the public RPC: `TokenLaunched` and `PoolGraduated` events on the
  factory, kept for 72 hours so creator history means something.
- **Scores every graduation** with named rules: creator history, creator tax, snipe-window buys, unique
  buyers, the creator buying its own curve, top-10 holder concentration, the creator's current holding,
  trades after graduation, socials, and launch-to-graduation pace. Every point is explained on the card.
- **Learns from outcomes.** Each verdict is checked at 1h, 6h and 24h. A rule that warned before a rug
  gains 2% weight; one that reassured before a rug loses 2%. Weights stay between 0.5x and 1.5x.
- **Trust value per creator**, shown on cards and in the bad-actors table.
- **Paper book**: what it would have bought at $10 a token, marked to market. No real money.
- **Strategy lab**: every candidate scoring 60 or more is traded on paper by 24 arms at once (8 exit
  policies × entry delays of 0, 30 and 60 minutes) on the same sampled price path for 48 hours, with 3%
  round-trip costs. Arms keep a running net return per dollar risked. The best arm with at least 10
  cases becomes the policy the paper book and the trader use; one position in ten explores another
  arm. Until then the default is `costout_1.5x@0m`: at +50% sell two thirds (the cost comes back),
  trail the rest 40% below its peak, cut at -35% before that, never hold past 48 hours.
- **Runway**: measures income from the treasury, estimates the planned bills (compute, gas, bridge) and
  projects 90 days under three scenarios. Rule: keep a 90-day reserve, spend on compute at most half of
  what it earns, invest only from the surplus.
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

Optional environment (see `.env.example`): `WH_WALLET` and `WH_BASE_WALLET` make the worm grow with real
balances; `WH_DEMO_TREASURY` fakes a treasury to look at the stages.

## Phase 2 commands

```bash
python -m wormhole.wallet new        # create the worm's key in .env (never printed), show the address
python -m wormhole.wallet show       # balances on Robinhood Chain and Base
python -m wormhole.logo              # render web/logo.png (the token logo)
python -m wormhole.launch            # dry run of the $WORM launch on pons (simulated, nothing sent)
WH_LIVE=1 python -m wormhole.launch --live   # the real launch: 0.0005 ETH fee + gas
```

Token settings live in `.env`: `WH_TOKEN_NAME`, `WH_TOKEN_SYMBOL`, `WH_TOKEN_X`, `WH_TOKEN_TELEGRAM`, `WH_CREATOR_TAX_BPS`
(default 100 = 1%), `WH_SITE_URL` (logo and website links). `WH_OWNER_WALLET` receives `WH_OWNER_SHARE` (default 0.20)
of every fee claim. The live screen (`WH_SCREEN=1`, default on) needs Chromium: `python -m playwright install chromium`.

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
| `WH_BUY_MIN_SCORE`, `WH_MAX_POSITION_USD`, `WH_MAX_OPEN`, `WH_MAX_DAILY_USD` | trader limits (70, 10, 5, 30) |
| `WH_CAUSES`, `WH_GIVE_SHARE` | `Name\|0xaddr\|weight,...` and the share of the surplus given every 30 days (0.10) |
| `WH_DEMO_TREASURY` | pretend treasury so the growth, runway, trader and giving logic can be watched before funding |

Posting to X is manual on purpose: entries sit on the site with a copy button.

## Roadmap

1. Signals and the live page (this).
2. Its own token on pons, paired with USDG, so creator fees fund it.
3. The voice: journal entries from telemetry, checked like the fly's; compute paid from its own Venice balance. Built, demo mode.
4. Buys from the surplus above the 90-day reserve with hedged exits, quoted and simulated on Uniswap v4. Built, demo mode;
   live USDG-quoted buys and live sells still need the Permit2 approval step.
5. Causes: giving from the surplus, on-chain and public. Built, demo mode.

## The docs page

`/docs` explains the whole thing in plain words: how a dig works, the trust values, how the brain learns,
the strategy lab, the readiness gate, the fee split, the runway policy, the token, security, and the roadmap.
Set `WH_REPO_URL` once the public repository exists and the page links to it.

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
