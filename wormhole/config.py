"""Addresses, limits and knobs. Everything here was verified on-chain on 2026-09-11/12."""
import os
import re
from pathlib import Path

from eth_utils import is_address, is_checksum_address

ROOT = Path(__file__).resolve().parent.parent


def valid_address(a):
    """A 20-byte hex address. A mixed-case one must carry a correct EIP-55 checksum: that is where a
    typo shows up (eth_utils.is_address alone lets a wrong checksum through)."""
    if not isinstance(a, str) or not is_address(a):
        return False
    hexpart = a[2:]
    return hexpart == hexpart.lower() or hexpart == hexpart.upper() or is_checksum_address(a)


def parse_dotenv(text):
    """KEY=VALUE lines from a .env file. Blank lines and # comments are skipped, an inline " # comment"
    is dropped, surrounding quotes are removed, and when a key appears twice the last line wins."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.split(" #", 1)[0].strip().strip('"').strip("'")
    return out


def _load_dotenv(path):
    """Values from .env become defaults for the environment (the environment wins). The one exception
    is WH_SECRET: it is returned to the caller and never put in the environment, so child processes
    (the Playwright driver, Chromium) cannot inherit it."""
    try:
        vals = parse_dotenv(path.read_text())
    except FileNotFoundError:
        return ""
    secret = vals.pop("WH_SECRET", "")
    for k, v in vals.items():
        os.environ.setdefault(k, v)
    return secret


ENV_FILE = Path(os.environ.get("WH_ENV_FILE") or ROOT / ".env")     # another env file, e.g. a rehearsal's, read and written instead of .env
_file_secret = _load_dotenv(ENV_FILE)
DATA_DIR = Path(os.environ.get("WH_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "wormhole.db"
if os.environ.get("WH_DATA_DIR") and not os.path.ismount(DATA_DIR):
    import logging
    logging.getLogger("wormhole.config").warning("WH_DATA_DIR=%s is not a mount point: the database will not survive a redeploy", DATA_DIR)

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

BLOCK_TIME = 0.10125                    # seconds per block, mean over 2.69M blocks; the indexer re-measures at start
BLOCKS_PER_HOUR = int(3600 / BLOCK_TIME)
BACKFILL_HOURS = float(os.environ.get("WH_BACKFILL_HOURS", "72"))   # launches kept for creator history
SCORE_HOURS = float(os.environ.get("WH_SCORE_HOURS", "0"))          # graduations scored at startup; 0: only new ones from now on, no catching up
LOG_CHUNK = 150_000          # the public node caps a query at 10k logs; ~950 launches/hour
TRANSFER_LOG_CAP = 60_000    # stop reading a token's transfers past this and mark the score partial
SNIPE_WINDOW_S = 3           # pons snipe tax window (snipeTaxSeconds on the factory)
FOLLOW_EVERY_S = 6
MARK_EVERY_S = 300
MAX_FEED = 60

# Paper portfolio (no real money in phase 1)
PAPER_SIZE_USD = 10.0
PAPER_MIN_SCORE = 60
PAPER_MAX_OPEN = int(os.environ.get("WH_PAPER_MAX_OPEN", "25"))   # the second-look book opens several positions a day and holds each up to 12 h
PAPER_TAKE_PROFIT = 1.0      # +100%
PAPER_STOP_LOSS = -0.5       # -50%
PAPER_MAX_AGE_S = 24 * 3600

# The worm's own key (phase 2). One secp256k1 key, same address on Robinhood Chain and Base.
# The environment wins over .env when the variable is present, even empty. It is popped, not read:
# after this line no process started from here carries the key in its environment.
_env_secret = os.environ.pop("WH_SECRET", None)
SECRET = (_file_secret if _env_secret is None else _env_secret).strip()
del _file_secret, _env_secret


def _addr_from_secret():
    if not SECRET:
        return ""
    from eth_account import Account
    return Account.from_key(SECRET).address.lower()


def check_addresses(wallet_env, signer, owner):
    """The address checks that stop the process at startup instead of failing every cycle later:
    a WH_WALLET that is not the signer's address would read one wallet and send from another; a
    malformed WH_OWNER_WALLET would make every forward fail while claims go on."""
    if wallet_env and not valid_address(wallet_env):
        raise SystemExit("WH_WALLET is not an address")
    if wallet_env and signer and wallet_env.lower() != signer.lower():
        raise SystemExit("WH_WALLET is not the address of WH_SECRET: remove one of them")
    if owner and not valid_address(owner):
        raise SystemExit("WH_OWNER_WALLET is not an address")


_signer = _addr_from_secret()
_wallet_env = os.environ.get("WH_WALLET", "").strip()
_owner_env = os.environ.get("WH_OWNER_WALLET", "").strip()
check_addresses(_wallet_env, _signer, _owner_env)
WALLET = (_wallet_env or _signer).lower()
BASE_WALLET = (os.environ.get("WH_BASE_WALLET") or WALLET).lower()
OWNER_WALLET = _owner_env.lower()                                    # the creator's wallet: receives OWNER_SHARE of every claim
# The fee policy, of every claim of creator fees: the creator's share is forwarded; the gold share buys tokenized
# gold (GLD) the worm keeps as a reserve; the burn share buys $WORM on its own pool and sends it to the burn address;
# the rest (operations) pays compute, gas, bridging and the runway reserve.
OWNER_SHARE = float(os.environ.get("WH_OWNER_SHARE", "0.50"))
BURN_SHARE = float(os.environ.get("WH_BURN_SHARE", "0.20"))
GOLD_SHARE = float(os.environ.get("WH_GOLD_SHARE", "0.10"))
OPS_SHARE = round(1.0 - OWNER_SHARE - BURN_SHARE - GOLD_SHARE, 6)
if not (0.0 <= OWNER_SHARE <= 1.0 and 0.0 <= BURN_SHARE <= 1.0 and 0.0 <= GOLD_SHARE <= 1.0 and OPS_SHARE >= 0.0):
    raise SystemExit("WH_OWNER_SHARE, WH_BURN_SHARE and WH_GOLD_SHARE must each be between 0 and 1 and add up to at most 1")
GLD = "0xc9a981fee1f9dec688bb123ccdecc63d0debfc4e"          # SPDR Gold Trust, Robinhood Token: tokenized gold, 18 decimals
QUOTER_V3 = "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7"    # Uniswap v3 QuoterV2 on Robinhood Chain: the gold pools are v3
SWAP_ROUTER_V3 = "0xcaf681a66d020601342297493863e78c959e5cb2"  # Uniswap v3 SwapRouter02 on Robinhood Chain: the gold buy goes through it
GOLD_POOL_FEE = 500                                          # the GLD/USDG 0.05% pool, the deepest quote
TRADING = os.environ.get("WH_TRADING", "0") == "1"                   # off by policy until the brain is mature; when on, the readiness gate still applies
LIVE = os.environ.get("WH_LIVE", "0") == "1"                         # nothing is ever signed unless 1
SITE_URL = os.environ.get("WH_SITE_URL", "http://127.0.0.1:4670").rstrip("/")
REPO_URL = os.environ.get("WH_REPO_URL", "").strip()               # public source, shown on /docs once it exists
TOKEN_X = os.environ.get("WH_TOKEN_X", "").strip().lstrip("@")       # the token's X handle: written on-chain at launch, linked from the pages
X_URL = f"https://x.com/{TOKEN_X}" if re.fullmatch(r"[A-Za-z0-9_]{1,15}", TOKEN_X) else ""
TOKEN = os.environ.get("WH_TOKEN", "").lower()                        # the worm's own token once launched
BASE_RPC = os.environ.get("WH_BASE_RPC", "https://mainnet.base.org")
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

GECKO = "https://api.geckoterminal.com/api/v2/networks/robinhood"
UA = "wormhole/0.1 (+https://github.com)"


# ---- compute: who sells the worm its inference and how it pays ----
# aisurplus: AI Surplus (aisurplus.io), paid by plain USDG transfers on Robinhood Chain to the account's deposit
# address, so nothing is ever bridged. venice: Venice AI, paid in USDC on Base through x402 (the older rail).
COMPUTE_PROVIDER = os.environ.get("WH_COMPUTE_PROVIDER", "aisurplus").strip().lower()
if COMPUTE_PROVIDER not in ("aisurplus", "venice"):
    raise SystemExit("WH_COMPUTE_PROVIDER must be aisurplus or venice")
AISURPLUS_KEY = (os.environ.pop("WH_AISURPLUS_KEY", None) or "").strip()     # popped like WH_SECRET: no child process sees it
AISURPLUS_DEPOSIT = os.environ.get("WH_AISURPLUS_DEPOSIT", "").strip().lower()   # the deposit address AI Surplus shows for the account
if AISURPLUS_DEPOSIT and not re.fullmatch(r"0x[0-9a-f]{40}", AISURPLUS_DEPOSIT):
    raise SystemExit("WH_AISURPLUS_DEPOSIT is not an address")
