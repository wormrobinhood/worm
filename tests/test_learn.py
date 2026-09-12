"""The brain: verdicts linked to outcomes, rules re-weighted against the base rate within bounds, creator trust."""
import json
import time

from wormhole import learn
from wormhole.learn import Brain, creator_trust


def fired(**pts):
    return [{"rule": r, "points": p, "text": r} for r, p in pts.items()]


def result(score, verdict, price, **pts):
    return {"score": score, "verdict": verdict, "metrics": {"price_usd": price}, "fired": fired(**pts)}


def age(db, token, seconds):
    db.x("UPDATE outcomes SET scored_at=? WHERE token=?", (int(time.time()) - seconds, token))


def prices(monkeypatch, **by_token):
    monkeypatch.setattr(learn, "token_prices", lambda toks: {t: {"price_usd": p, "age_s": 0} for t, p in by_token.items()})


def outcome(db, token):
    return db.one("SELECT * FROM outcomes WHERE token=?", (token,))


def checks(db, token):
    return json.loads(outcome(db, token)["checks"] or "{}")


def backdate_rug_flag(db, token, seconds=learn.RUG_CONFIRM_S + 1):
    """Pretend the first rug reading happened `seconds` ago, so the next check can confirm it."""
    c = checks(db, token)
    c["rug_seen"]["ts"] -= seconds
    db.x("UPDATE outcomes SET checks=? WHERE token=?", (json.dumps(c), token))


def test_rules_start_at_weight_one(db):
    w = Brain(db).weights()
    assert set(w) == set(learn.RULES) and all(v == 1.0 for v in w.values())
    assert "creator_rugs" in w


def test_rug_after_avoid_is_called_and_nudges_rules(db, monkeypatch):
    b = Brain(db)
    b.record("0xrug", result(20, "avoid", 1.0, snipe=-20, socials=+5, activity=0))
    age(db, "0xrug", 3700)                                   # past the 1 h checkpoint
    prices(monkeypatch, **{"0xrug": 0.1})
    b.check()
    assert outcome(db, "0xrug")["resolved"] == 0 and "rug_seen" in checks(db, "0xrug")   # one reading is a flag, not a verdict
    backdate_rug_flag(db, "0xrug")
    b.check()                                                # second reading, still -90%, 5 minutes later
    o = outcome(db, "0xrug")
    assert o["resolved"] == 1 and o["outcome"] == "rugged" and abs(o["change_pct"] + 90) < 1e-6
    w = b.weights()
    assert abs(w["snipe"] - 1.02) < 1e-9        # warned before a rug, base rate 0.5: +2 * STEP * 0.5
    assert abs(w["socials"] - 0.98) < 1e-9      # reassured before a rug: loses the same
    assert w["activity"] == 1.0                 # zero points: no lesson
    r = db.one("SELECT hits, misses, fired_bad, fired_good FROM rules WHERE id='snipe'")
    assert (r["hits"], r["misses"], r["fired_bad"], r["fired_good"]) == (1, 0, 1, 0)
    ev = db.one("SELECT text FROM events WHERE kind='lesson' ORDER BY id DESC LIMIT 1")
    assert "called it" in ev["text"] and "snipe +" in ev["text"]


def test_dip_at_one_hour_is_not_a_rug(db, monkeypatch):
    b = Brain(db)
    b.record("0xdip", result(80, "looks healthy", 1.0, buyers=+10))
    age(db, "0xdip", 3700)
    monkeypatch.setattr(learn, "token_prices", lambda toks: {"0xdip": {"price_usd": 0.4}})   # -60%: not -80%
    b.check()
    o = outcome(db, "0xdip")
    assert o["resolved"] == 0 and o["outcome"] == "pending" and "3600" in o["checks"] and "rug_seen" not in o["checks"]


def test_one_rug_reading_followed_by_recovery_is_not_a_rug(db, monkeypatch):
    b = Brain(db)
    b.record("0xv", result(60, "mixed", 1.0, pace=0))
    age(db, "0xv", 3700)
    prices(monkeypatch, **{"0xv": 0.15})                     # -85% at the 1 h check
    b.check()
    assert "rug_seen" in checks(db, "0xv") and outcome(db, "0xv")["resolved"] == 0
    backdate_rug_flag(db, "0xv")
    prices(monkeypatch, **{"0xv": 2.0})                      # +100% five minutes later
    b.check()
    o = outcome(db, "0xv")
    assert o["resolved"] == 0 and "rug_seen" not in checks(db, "0xv") and abs(o["change_pct"] - 100) < 1e-6
    age(db, "0xv", 24 * 3600 + 5)
    b.check()
    assert outcome(db, "0xv")["outcome"] == "grew"


def test_growth_after_healthy_verdict_resolves_at_24h(db, monkeypatch):
    b = Brain(db)
    b.record("0xup", result(80, "looks healthy", 1.0, buyers=+10, snipe=-5))
    age(db, "0xup", 24 * 3600 + 5)
    monkeypatch.setattr(learn, "token_prices", lambda toks: {"0xup": {"price_usd": 2.0}})   # +100%: grew
    b.check()
    o = outcome(db, "0xup")
    assert o["outcome"] == "grew" and o["resolved"] == 1
    w = b.weights()
    assert abs(w["buyers"] - 1.02) < 1e-9 and abs(w["snipe"] - 0.98) < 1e-9


def test_flat_and_dumped_labels(db, monkeypatch):
    b = Brain(db)
    b.record("0xflat", result(50, "mixed", 1.0, pace=+3))
    b.record("0xup90", result(50, "mixed", 1.0, pace=+3))
    b.record("0xdump", result(50, "mixed", 1.0, pace=+3))
    for t in ("0xflat", "0xup90", "0xdump"):
        age(db, t, 24 * 3600 + 5)
    prices(monkeypatch, **{"0xflat": 1.2, "0xup90": 1.9, "0xdump": 0.45})
    b.check()
    assert outcome(db, "0xflat")["outcome"] == "flat"
    assert outcome(db, "0xup90")["outcome"] == "flat"        # +90% is not "grew": that needs +100%
    assert outcome(db, "0xdump")["outcome"] == "dumped"
    assert b.weights()["pace"] == 1.0 - learn.STEP     # reassured (+points) before a dump: loses once, flat teaches nothing


def test_no_price_ever_resolves_unknown_only_after_the_grace_period(db, monkeypatch):
    b = Brain(db)
    b.record("0xghost", {"score": 30, "verdict": "avoid", "metrics": {}, "fired": fired(snipe=-20)})
    age(db, "0xghost", 24 * 3600 + 5)
    monkeypatch.setattr(learn, "token_prices", lambda toks: {})
    b.check()
    assert outcome(db, "0xghost")["outcome"] == "pending"      # still waiting for a price
    age(db, "0xghost", 30 * 3600 + 5)
    b.check()
    o = outcome(db, "0xghost")
    assert o["outcome"] == "unknown" and o["resolved"] == 1
    assert b.weights()["snipe"] == 1.0


def test_missing_price_leaves_the_checkpoint_open_and_never_resolves_unknown(db, monkeypatch):
    b = Brain(db)
    b.record("0xm", result(20, "avoid", 1.0, snipe=-20))
    age(db, "0xm", 3700)
    monkeypatch.setattr(learn, "token_prices", lambda toks: {})
    b.check()
    assert "3600" not in checks(db, "0xm")                    # no price: not stamped, tried again next cycle
    age(db, "0xm", 3700 + 300)
    prices(monkeypatch, **{"0xm": 0.1})
    b.check()
    c = checks(db, "0xm")
    assert c["3600"]["price"] == 0.1 and "rug_seen" in c and outcome(db, "0xm")["resolved"] == 0
    age(db, "0xm", 24 * 3600 + 5)
    monkeypatch.setattr(learn, "token_prices", lambda toks: {})
    b.check()
    assert outcome(db, "0xm")["resolved"] == 0 and "86400" not in checks(db, "0xm")
    age(db, "0xm", 24 * 3600 + 305)
    prices(monkeypatch, **{"0xm": 0.1})
    b.check()
    o = outcome(db, "0xm")
    assert o["outcome"] == "rugged" and o["resolved"] == 1     # the 24 h reading alone settles a rug


def test_first_price_at_31_minutes_still_sets_the_baseline(db, monkeypatch):
    b = Brain(db)
    b.record("0xl", {"score": 30, "verdict": "avoid", "metrics": {}, "fired": fired(snipe=-20)})
    age(db, "0xl", 31 * 60)
    prices(monkeypatch, **{"0xl": 1.0})
    b.check()
    assert outcome(db, "0xl")["price0"] == 1.0 and abs(checks(db, "0xl")["baseline"]["age_s"] - 31 * 60) <= 2
    age(db, "0xl", 3700)
    prices(monkeypatch, **{"0xl": 0.05})
    b.check()
    backdate_rug_flag(db, "0xl")
    b.check()
    assert outcome(db, "0xl")["outcome"] == "rugged"
    # after six hours it is too late for a baseline
    b.record("0xn", {"score": 30, "verdict": "avoid", "metrics": {}, "fired": fired(snipe=-20)})
    age(db, "0xn", 7 * 3600)
    prices(monkeypatch, **{"0xn": 1.0})
    b.check()
    assert outcome(db, "0xn")["price0"] is None


def test_checkpoints_missed_during_downtime_are_skipped_not_backfilled(db, monkeypatch):
    b = Brain(db)
    b.record("0xd", result(50, "mixed", 1.0, pace=0))
    age(db, "0xd", 24 * 3600 + 5)
    prices(monkeypatch, **{"0xd": 1.3})
    b.check()
    c = checks(db, "0xd")
    assert c["3600"]["skipped"] is True and c["21600"]["skipped"] is True and "price" not in c["3600"]
    assert c["86400"]["price"] == 1.3 and outcome(db, "0xd")["outcome"] == "flat"


def test_stale_cached_price_is_not_a_reading(db, monkeypatch):
    b = Brain(db)
    b.record("0xs", result(20, "avoid", 1.0, snipe=-20))
    age(db, "0xs", 3700)
    monkeypatch.setattr(learn, "token_prices", lambda toks: {"0xs": {"price_usd": 0.1, "age_s": 1000}})
    b.check()
    assert "3600" not in checks(db, "0xs") and outcome(db, "0xs")["resolved"] == 0


def test_deadline_resolves_from_the_last_reading_before_unknown(db, monkeypatch):
    b = Brain(db)
    b.record("0xe", result(50, "mixed", 1.0, pace=0))
    age(db, "0xe", 6 * 3600 + 5)
    prices(monkeypatch, **{"0xe": 0.4})                      # -60% at 6 h, then the pool vanishes from Gecko
    b.check()
    monkeypatch.setattr(learn, "token_prices", lambda toks: {})
    age(db, "0xe", 30 * 3600 + 5)
    b.check()
    o = outcome(db, "0xe")
    assert o["outcome"] == "dumped" and abs(o["change_pct"] + 60) < 1e-6


def test_record_never_touches_a_resolved_row_or_resets_a_pending_one(db, monkeypatch):
    b = Brain(db)
    dep = "0x" + "ab" * 20
    db.x("INSERT INTO launches(token, deployer, ts, graduated, block) VALUES(?,?,?,?,?)", ("0xr", dep, 1, 1, 100))
    b.record("0xr", result(20, "avoid", 1.0, snipe=-20))
    age(db, "0xr", 3700)
    prices(monkeypatch, **{"0xr": 0.1})
    b.check()
    backdate_rug_flag(db, "0xr")
    b.check()
    assert outcome(db, "0xr")["outcome"] == "rugged"
    trust = creator_trust(db, dep)[0]
    b.record("0xr", result(80, "looks healthy", 5.0, buyers=+10))       # a rescan lands
    o = outcome(db, "0xr")
    assert o["outcome"] == "rugged" and o["resolved"] == 1 and o["score"] == 20 and o["price0"] == 1.0
    assert creator_trust(db, dep)[0] == trust
    # a pending row keeps its baseline, clock and checks but takes the new verdict
    b.record("0xp", result(50, "mixed", 1.0, pace=+3))
    age(db, "0xp", 3700)
    db.x("UPDATE outcomes SET checks=? WHERE token='0xp'", (json.dumps({"3600": {"price": 1.1, "ts": 1}}),))
    b.record("0xp", result(30, "avoid", 9.0, snipe=-15))
    o = outcome(db, "0xp")
    assert o["score"] == 30 and o["verdict"] == "avoid" and o["price0"] == 1.0 and "3600" in o["checks"]
    assert o["scored_at"] <= int(time.time()) - 3600 and json.loads(o["fired"])[0]["rule"] == "snipe"


def test_weights_stay_within_bounds(db, monkeypatch):
    b = Brain(db)
    db.x("UPDATE rules SET weight=1.49 WHERE id='snipe'")
    db.x("UPDATE rules SET weight=0.51 WHERE id='socials'")
    b.record("0xb", result(10, "avoid", 1.0, snipe=-20, socials=+5))
    age(db, "0xb", 3700)
    prices(monkeypatch, **{"0xb": 0.05})
    b.check()
    backdate_rug_flag(db, "0xb")
    b.check()
    w = b.weights()
    assert w["snipe"] == learn.HI and w["socials"] == learn.LO


def test_oracle_rule_outranks_an_always_on_penalty(db):
    """300 tokens, 3 in 10 go bad. snipe warns on every token; top10 warns exactly on the bad ones and
    reassures on the rest. The old rule (sign agreement) sent both to 1.5x."""
    b = Brain(db)
    toks = []
    for i in range(300):
        bad = i % 10 in (0, 3, 6)
        t = f"0x{i:04x}"
        b.record(t, {"score": 40, "verdict": "mixed", "metrics": {"price_usd": 1.0},
                     "fired": fired(snipe=-15, top10=(-20 if bad else 5))})
        toks.append((t, bad))
    for t, bad in toks:
        b._resolve(outcome(db, t), "rugged" if bad else "grew", -90.0 if bad else 150.0)
    w = b.weights()
    assert abs(w["snipe"] - 1.0) < 0.2                       # rugs at the base rate teach nothing (the old rule: 1.5x)
    assert w["top10"] == learn.HI and w["snipe"] < 1.2 < w["top10"]
    rules = {r["id"]: r for r in b.summary()["rules"]}
    assert rules["snipe"]["fired_bad"] == 90 and rules["snipe"]["fired_good"] == 210 and abs(rules["snipe"]["lift"] - 1.0) < 0.05
    assert rules["top10"]["fired_bad"] == 90 and rules["top10"]["fired_good"] == 0 and rules["top10"]["lift"] > 3.0


def test_scorecard_counts_resolved_only(db, monkeypatch):
    b = Brain(db)
    b.record("0xa", result(20, "avoid", 1.0, snipe=-20))
    b.record("0xp", result(20, "avoid", 1.0, snipe=-20))
    age(db, "0xa", 3700)
    monkeypatch.setattr(learn, "token_prices", lambda toks: {t: {"price_usd": 0.1} for t in toks})
    b.check()
    backdate_rug_flag(db, "0xa")
    b.check()
    s = b.summary()
    assert s["scorecard"] == {"avoid": {"rugged": 1}} and s["tracked"] == 2 and s["resolved"] == 1
    assert s["counts"] == {"rugged": 1, "pending": 1}


def test_summary_reports_returns_base_rate_and_lift(db, monkeypatch):
    b = Brain(db)
    for i in range(3):
        b.record(f"0xa{i}", result(20, "avoid", 1.0, snipe=-20))
    b.record("0xh", result(80, "looks healthy", 1.0, buyers=10))
    for t in ("0xa0", "0xa1", "0xh"):
        age(db, t, 24 * 3600 + 5)
    prices(monkeypatch, **{"0xa0": 0.1, "0xa1": 0.4, "0xh": 3.0})
    b.check()
    s = b.summary()
    assert s["counts"] == {"rugged": 1, "dumped": 1, "grew": 1, "pending": 1} and s["tracked"] == 4 and s["resolved"] == 3
    assert s["scorecard"] == {"avoid": {"rugged": 1, "dumped": 1}, "looks healthy": {"grew": 1}}
    assert s["returns"]["avoid"] == {"n": 2, "mean_pct": -75.0, "median_pct": -75.0}
    assert s["returns"]["looks healthy"] == {"n": 1, "mean_pct": 200.0, "median_pct": 200.0}
    assert s["base_rate_pct"] == 67
    rules = {r["id"]: r for r in s["rules"]}
    assert rules["snipe"]["fired_bad"] == 2 and rules["snipe"]["lift"] is None      # fewer than 5 warnings: no lift yet
    assert len(s["outcomes"]) == 4 and s["outcomes"][0]["symbol"] is None


def test_creator_trust_formula(db):
    dep = "0x" + "aa" * 20
    assert creator_trust(db, dep)[0] == 50                                   # unknown creator: neutral
    for i in range(4):
        db.x("INSERT INTO launches(token, deployer, ts, graduated) VALUES(?,?,?,?)", (f"0xt{i}", dep, 1, 1 if i == 0 else 0))
    t, info = creator_trust(db, dep)
    assert {k: info[k] for k in ("launches", "grads", "rugged", "grew")} == {"launches": 4, "grads": 1, "rugged": 0, "grew": 0}
    assert t == 44                                          # 50 - 10 * log10(4); an unchecked graduation earns nothing
    db.x("INSERT INTO outcomes(token, verdict, outcome, resolved) VALUES('0xt0','mixed','flat',1)")
    assert creator_trust(db, dep)[0] == 52                  # + 8: a graduation that held up
    db.x("UPDATE outcomes SET outcome='rugged' WHERE token='0xt0'")
    assert creator_trust(db, dep)[0] == 24                  # - 20 instead
    assert creator_trust(db, dep, exclude_token="0xt0")[0] == 45      # without that token: 3 launches, no rug
    for i in range(20):
        db.x("INSERT INTO launches(token, deployer, ts) VALUES(?,?,?)", (f"0xs{i}", dep, 1))
    assert creator_trust(db, dep)[0] == 16                  # 24 launches: 50 - 13.8 - 20
    db.many("INSERT INTO launches(token, deployer, ts) VALUES(?,?,?)", [(f"0xz{i}", dep, 1) for i in range(10_000)])
    assert creator_trust(db, dep)[0] == 0                   # the launch penalty caps at 40; clamped at 0


def test_creator_trust_pending_and_unknown_graduations(db):
    dep = "0x" + "bb" * 20
    for i in range(3):
        db.x("INSERT INTO launches(token, deployer, ts, graduated) VALUES(?,?,?,?)", (f"0xg{i}", dep, 1, 1))
        db.x("INSERT INTO outcomes(token, verdict, outcome, resolved) VALUES(?,?,?,?)", (f"0xg{i}", "mixed", "pending", 0))
    t, info = creator_trust(db, dep)
    assert t == 45 and info["pending"] == 3 and info["good"] == 0          # 50 - 10 * log10(3): nothing earned while pending
    db.x("UPDATE outcomes SET outcome='unknown', resolved=1")
    t, info = creator_trust(db, dep)
    assert t == 30 and info["unknown"] == 3 and t <= 50                    # -5 for each outcome that could not be read
    db.x("UPDATE outcomes SET outcome='grew'")
    assert creator_trust(db, dep)[0] == 89                                 # 45.2 + 24 (3 x 8) + 20 (capped)
