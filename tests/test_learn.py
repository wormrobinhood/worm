"""The brain: verdicts linked to outcomes, rules re-weighted within bounds, creator trust."""
import time

from wormhole import learn
from wormhole.learn import Brain, creator_trust


def fired(**pts):
    return [{"rule": r, "points": p, "text": r} for r, p in pts.items()]


def result(score, verdict, price, **pts):
    return {"score": score, "verdict": verdict, "metrics": {"price_usd": price}, "fired": fired(**pts)}


def age(db, token, seconds):
    db.x("UPDATE outcomes SET scored_at=? WHERE token=?", (int(time.time()) - seconds, token))


def test_rules_start_at_weight_one(db):
    w = Brain(db).weights()
    assert set(w) == set(learn.RULES) and all(v == 1.0 for v in w.values())


def test_rug_after_avoid_is_called_and_nudges_rules(db, monkeypatch):
    b = Brain(db)
    b.record("0xrug", result(20, "avoid", 1.0, snipe=-20, socials=+5, activity=0))
    age(db, "0xrug", 3700)                                   # past the 1 h checkpoint
    monkeypatch.setattr(learn, "token_prices", lambda toks: {"0xrug": {"price_usd": 0.1}})
    b.check()
    o = db.one("SELECT * FROM outcomes WHERE token='0xrug'")
    assert o["resolved"] == 1 and o["outcome"] == "rugged" and abs(o["change_pct"] + 90) < 1e-6
    w = b.weights()
    assert abs(w["snipe"] - 1.02) < 1e-9        # warned before a rug: gains
    assert abs(w["socials"] - 0.98) < 1e-9      # reassured before a rug: loses
    assert w["activity"] == 1.0                 # zero points: no lesson
    r = db.one("SELECT hits, misses FROM rules WHERE id='snipe'")
    assert (r["hits"], r["misses"]) == (1, 0)
    ev = db.one("SELECT text FROM events WHERE kind='lesson' ORDER BY id DESC LIMIT 1")
    assert "called it" in ev["text"] and "snipe +" in ev["text"]


def test_dip_at_one_hour_is_not_a_rug(db, monkeypatch):
    b = Brain(db)
    b.record("0xdip", result(80, "looks healthy", 1.0, buyers=+10))
    age(db, "0xdip", 3700)
    monkeypatch.setattr(learn, "token_prices", lambda toks: {"0xdip": {"price_usd": 0.4}})   # -60%: not -80%
    b.check()
    o = db.one("SELECT * FROM outcomes WHERE token='0xdip'")
    assert o["resolved"] == 0 and o["outcome"] == "pending" and "3600" in o["checks"]


def test_growth_after_healthy_verdict_resolves_at_24h(db, monkeypatch):
    b = Brain(db)
    b.record("0xup", result(80, "looks healthy", 1.0, buyers=+10, snipe=-5))
    age(db, "0xup", 24 * 3600 + 5)
    monkeypatch.setattr(learn, "token_prices", lambda toks: {"0xup": {"price_usd": 2.0}})
    b.check()
    o = db.one("SELECT * FROM outcomes WHERE token='0xup'")
    assert o["outcome"] == "grew" and o["resolved"] == 1
    w = b.weights()
    assert abs(w["buyers"] - 1.02) < 1e-9 and abs(w["snipe"] - 0.98) < 1e-9


def test_flat_and_dumped_labels(db, monkeypatch):
    b = Brain(db)
    b.record("0xflat", result(50, "mixed", 1.0, pace=+3))
    b.record("0xdump", result(50, "mixed", 1.0, pace=+3))
    age(db, "0xflat", 24 * 3600 + 5)
    age(db, "0xdump", 24 * 3600 + 5)
    monkeypatch.setattr(learn, "token_prices", lambda toks: {"0xflat": {"price_usd": 1.2}, "0xdump": {"price_usd": 0.45}})
    b.check()
    assert db.one("SELECT outcome FROM outcomes WHERE token='0xflat'")["outcome"] == "flat"
    assert db.one("SELECT outcome FROM outcomes WHERE token='0xdump'")["outcome"] == "dumped"
    assert b.weights()["pace"] == 1.0 - learn.STEP     # reassured (+points) before a dump: loses once, flat teaches nothing


def test_no_price_ever_resolves_unknown_without_lessons(db, monkeypatch):
    b = Brain(db)
    b.record("0xghost", {"score": 30, "verdict": "avoid", "metrics": {}, "fired": fired(snipe=-20)})
    age(db, "0xghost", 24 * 3600 + 5)
    monkeypatch.setattr(learn, "token_prices", lambda toks: {})
    b.check()
    o = db.one("SELECT * FROM outcomes WHERE token='0xghost'")
    assert o["outcome"] == "unknown" and o["resolved"] == 1
    assert b.weights()["snipe"] == 1.0


def test_weights_stay_within_bounds(db, monkeypatch):
    b = Brain(db)
    db.x("UPDATE rules SET weight=1.49 WHERE id='snipe'")
    db.x("UPDATE rules SET weight=0.51 WHERE id='socials'")
    b.record("0xb", result(10, "avoid", 1.0, snipe=-20, socials=+5))
    age(db, "0xb", 3700)
    monkeypatch.setattr(learn, "token_prices", lambda toks: {"0xb": {"price_usd": 0.05}})
    b.check()
    w = b.weights()
    assert w["snipe"] == learn.HI and w["socials"] == learn.LO


def test_scorecard_counts_resolved_only(db, monkeypatch):
    b = Brain(db)
    b.record("0xa", result(20, "avoid", 1.0, snipe=-20))
    b.record("0xp", result(20, "avoid", 1.0, snipe=-20))
    age(db, "0xa", 3700)
    monkeypatch.setattr(learn, "token_prices", lambda toks: {t: {"price_usd": 0.1} for t in toks})
    b.check()
    s = b.summary()
    assert s["scorecard"] == {"avoid": {"rugged": 1}} and s["tracked"] == 2 and s["resolved"] == 1


def test_creator_trust_formula(db):
    dep = "0x" + "aa" * 20
    assert creator_trust(db, dep)[0] == 50                                   # unknown creator: neutral
    for i in range(4):
        db.x("INSERT INTO launches(token, deployer, ts, graduated) VALUES(?,?,?,?)", (f"0xt{i}", dep, 1, 1 if i == 0 else 0))
    t, info = creator_trust(db, dep)
    assert info == {"launches": 4, "grads": 1, "rugged": 0, "grew": 0}
    assert t == 50 - 9 + 8                                                  # 3 extra launches, 1 graduation
    db.x("INSERT INTO outcomes(token, verdict, outcome, resolved) VALUES('0xt0','avoid','rugged',1)")
    assert creator_trust(db, dep)[0] == 50 - 9 + 8 - 20
    for i in range(20):
        db.x("INSERT INTO launches(token, deployer, ts) VALUES(?,?,?)", (f"0xs{i}", dep, 1))
    assert creator_trust(db, dep)[0] == max(0, 50 - 30 + 8 - 20)            # the launch penalty is capped at 30
