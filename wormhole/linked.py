"""Linked wallets: holders who passed tokens to each other before the verdict (what a bubble map draws).

One owner wearing many wallets shows up as a group of holders connected by transfers. Who counts as a person
decides everything. Measured on 1,453 graduations (docs/SECOND-LOOK.md): linked through routers, custodial bots
and multisenders everybody is one group and the read says nothing; a wallet that passed tokens on in ten or
more EARLIER tokens is therefore a service, not a person, and once a group holds 10% or more of the
circulating supply the chain is asked which of its members are contracts (lockers, vaults, smart wallets),
those are dropped and the group is measured again. What is left is rare (about 4% of tokens) and, so far, bad:
7.5% of those tokens turned out not bad against 14%, and none grew. On 53 tokens that is one chance in ten of
being luck, so the line carries no points yet: the records decide.

Read-only on the chain (eth_getCode, cached); writes only its own two tables."""
import collections
import logging
import time

from . import config as C

log = logging.getLogger("wormhole.linked")

SERVICE_MIN_TOKENS = 10      # passed tokens on in this many other tokens: a service, not a person
MIN_HISTORY = 150            # tokens whose senders are on record before the read says anything
GROUP_MIN_PCT = 10.0         # a linked group is worth a line from this share of the circulating supply
SENDERS_KEPT = 400           # biggest senders remembered per token
CODE_ASKED_MAX = 80          # members of big groups the chain is asked about, per token
KEEP_DAYS = 21


def ensure_tables(db):
    db.x("CREATE TABLE IF NOT EXISTS token_senders(wallet TEXT, token TEXT, ts INTEGER, PRIMARY KEY(wallet, token))")
    db.x("CREATE INDEX IF NOT EXISTS token_senders_token ON token_senders(token)")
    db.x("CREATE TABLE IF NOT EXISTS code_cache(address TEXT PRIMARY KEY, is_contract INTEGER, ts INTEGER)")


def history(db):
    return db.one("SELECT COUNT(DISTINCT token) n FROM token_senders")["n"]


def remember_senders(db, token, moves, curve, ts=None):
    """Who passed this token on (to anyone: a wallet, the pool, a router). Once per token."""
    ensure_tables(db)
    if db.one("SELECT 1 FROM token_senders WHERE token=? LIMIT 1", (token,)):
        return 0
    sent = collections.Counter()
    for frm, to, value in moves:
        if value > 0 and frm != curve and frm not in C.INFRA and frm != token:
            sent[frm] += value
    rows = [(w, token, int(ts or time.time())) for w, _ in sent.most_common(SENDERS_KEPT)]
    if rows:
        db.many("INSERT OR IGNORE INTO token_senders(wallet,token,ts) VALUES(?,?,?)", rows)
    return len(rows)


def services(db, wallets, token):
    """The wallets among `wallets` that passed tokens on in SERVICE_MIN_TOKENS other tokens."""
    out, wallets = set(), list(wallets)
    for i in range(0, len(wallets), 400):
        chunk = wallets[i:i + 400]
        for r in db.q(f"SELECT wallet FROM token_senders WHERE wallet IN ({','.join('?' * len(chunk))}) AND token!=?"
                      f" GROUP BY wallet HAVING COUNT(*)>=?", (*chunk, token, SERVICE_MIN_TOKENS)):
            out.add(r["wallet"])
    return out


def _find(parent, x):
    while parent.setdefault(x, x) != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def groups(moves, held, skip):
    """[(tokens held by the group, its holders, every wallet on its paths)] for groups of two or more holders,
    biggest first. A transfer links two wallets unless either of them is in `skip`."""
    parent = {}
    for frm, to, value in moves:
        if value > 0 and frm != to and frm not in skip and to not in skip:
            parent[_find(parent, frm)] = _find(parent, to)
    members, paths = collections.defaultdict(list), collections.defaultdict(set)
    for w in parent:
        paths[_find(parent, w)].add(w)
    for w in held:
        if w in parent and w not in skip:
            members[_find(parent, w)].append(w)
    out = [(sum(held[w] for w in g), g, paths[root]) for root, g in members.items() if len(g) >= 2]
    return sorted(out, key=lambda x: -x[0])


def contracts_among(rpc, db, wallets):
    """Which of these wallets have code. A wallet that delegates (EIP-7702) is still a person. None when the
    chain could not be asked: the caller then says nothing rather than guess."""
    ensure_tables(db)
    wallets = list(wallets)
    known = {}
    for i in range(0, len(wallets), 400):
        chunk = wallets[i:i + 400]
        for r in db.q(f"SELECT address, is_contract FROM code_cache WHERE address IN ({','.join('?' * len(chunk))})", chunk):
            known[r["address"]] = bool(r["is_contract"])
    need = [w for w in wallets if w not in known]
    if need:
        try:
            codes = rpc.batch([("eth_getCode", [w, "latest"]) for w in need], chunk=10)      # small batches: the public node refuses big ones when busy
        except Exception as e:
            log.info("code read failed: %s", e)
            return None
        if any(c is None for c in codes):
            return None
        now = int(time.time())
        for w, code in zip(need, codes):
            known[w] = len(code) > 2 and not code.lower().startswith("0xef0100")
        db.many("INSERT OR REPLACE INTO code_cache(address,is_contract,ts) VALUES(?,?,?)", [(w, int(known[w]), now) for w in need])
    return {w for w, is_c in known.items() if is_c}


def read(rpc, db, token, curve, moves, held, circ):
    """{"linked_group_pct", "linked_group_wallets", "senders_on_record"}; the first two None when nothing can be said."""
    ensure_tables(db)
    out = {"linked_group_pct": None, "linked_group_wallets": None, "senders_on_record": history(db)}
    if circ <= 0 or len(held) < 5:
        return out
    skip = set(C.INFRA) | {curve, token}
    wallets = {w for frm, to, _ in moves for w in (frm, to)} - skip
    skip |= services(db, wallets, token)
    found = groups(moves, held, skip)
    big = [g for g in found if 100.0 * g[0] / circ >= GROUP_MIN_PCT]
    if big:
        partners = collections.defaultdict(set)          # the hub of a group is asked about first: a new router links everyone
        for frm, to, value in moves:
            if value > 0 and frm not in skip and to not in skip:
                partners[frm].add(to)
                partners[to].add(frm)
        ask = []
        for _, holders, path in big:
            ask += sorted(path, key=lambda w: (-len(partners[w]), -held.get(w, 0)))
        code = contracts_among(rpc, db, ask[:CODE_ASKED_MAX])
        if code is None:
            return out
        found = groups(moves, held, skip | code)
    top = found[0] if found else None
    out["linked_group_pct"] = round(100.0 * top[0] / circ, 1) if top else 0.0
    out["linked_group_wallets"] = len(top[1]) if top else 0
    return out


def prune(db):
    ensure_tables(db)
    db.x("DELETE FROM token_senders WHERE ts<?", (int(time.time()) - KEEP_DAYS * 86400,))
