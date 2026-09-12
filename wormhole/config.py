"""Addresses, limits and knobs. Everything here was verified on-chain on 2026-09-11/12."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path):
    """KEY=VALUE lines from .env become defaults for the environment (the environment wins)."""
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv(ROOT / ".env")
DATA_DIR = Path(os.environ.get("WH_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "wormhole.db"

RPC = os.environ.get("WH_RPC", "https://rpc.mainnet.chain.robinhood.com")
CHAIN_ID = 4663
HOST = os.environ.get("WH_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", os.environ.get("WH_PORT", "4670")))

# Pons V2 (verified source on Blockscout, github.com/ponsdotdev/ponsfamily)
FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
PONS_ROUTER = "0xe33e9e479df8802cb0866d5d05258bec4cf62948"
LOCKER = "0x267444d099b10fb5ed7c3cc7b7c767adca574952"
BUYBACK_VAULT = "0x42df2a798f82289e177311362e8f5ccc45c1219c"
FEE_ESCROW = "0xd3afeb2a57f70ef218aa82451c51b2fb0416ac9e"
# Uniswap v4 on chain 4663 (developers.uniswap.org deployments page)
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
UNIVERSAL_ROUTER = "0x8876789976decbfcbbbe364623c63652db8c0904"
POSITION_MANAGER = "0x58daec3116aae6d93017baaea7749052e8a04fa7"
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
# Assets
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
ZERO = "0x0000000000000000000000000000000000000000"
DEAD = "0x000000000000000000000000000000000000dead"

# Contracts that hold tokens without being holders in the human sense.
INFRA = {FACTORY, HOOK, PONS_ROUTER, LOCKER, BUYBACK_VAULT, FEE_ESCROW,
         POOL_MANAGER, UNIVERSAL_ROUTER, POSITION_MANAGER, PERMIT2, ZERO, DEAD}

BLOCK_TIME = 0.1025                      # seconds per block, measured
BLOCKS_PER_HOUR = int(3600 / BLOCK_TIME)
BACKFILL_HOURS = float(os.environ.get("WH_BACKFILL_HOURS", "72"))   # launches kept for creator history
SCORE_HOURS = float(os.environ.get("WH_SCORE_HOURS", "4"))          # graduations scored at startup
LOG_CHUNK = 150_000          # the public node caps a query at 10k logs; ~950 launches/hour
TRANSFER_LOG_CAP = 60_000    # stop reading a token's transfers past this and mark the score partial
SNIPE_WINDOW_S = 3           # pons snipe tax window (snipeTaxSeconds on the factory)
FOLLOW_EVERY_S = 6
MARK_EVERY_S = 300
MAX_FEED = 30

# Paper portfolio (no real money in phase 1)
PAPER_SIZE_USD = 10.0
PAPER_MIN_SCORE = 60
PAPER_MAX_OPEN = 10
PAPER_TAKE_PROFIT = 1.0      # +100%
PAPER_STOP_LOSS = -0.5       # -50%
PAPER_MAX_AGE_S = 24 * 3600

# The worm's own key (phase 2). One secp256k1 key, same address on Robinhood Chain and Base.
SECRET = os.environ.get("WH_SECRET", "").strip()


def _addr_from_secret():
    if not SECRET:
        return ""
    from eth_account import Account
    return Account.from_key(SECRET).address.lower()


WALLET = (os.environ.get("WH_WALLET") or _addr_from_secret()).lower()
BASE_WALLET = (os.environ.get("WH_BASE_WALLET") or WALLET).lower()
OWNER_WALLET = os.environ.get("WH_OWNER_WALLET", "").lower()     # owner wallet: receives 20% of income
OWNER_SHARE = float(os.environ.get("WH_OWNER_SHARE", "0.20"))
LIVE = os.environ.get("WH_LIVE", "0") == "1"                         # nothing is ever signed unless 1
SITE_URL = os.environ.get("WH_SITE_URL", "http://127.0.0.1:4670").rstrip("/")
TOKEN = os.environ.get("WH_TOKEN", "").lower()                        # the worm's own token once launched
BASE_RPC = os.environ.get("WH_BASE_RPC", "https://mainnet.base.org")
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

GECKO = "https://api.geckoterminal.com/api/v2/networks/robinhood"
UA = "wormhole/0.1 (+https://github.com)"
