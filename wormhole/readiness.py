"""Readiness: how close the worm is to trading real money, measured from evidence only.

Four parts, each 0-100, weighted into one number. None of them can be raised by hand:
  strategy proven    the strategy the paper book trades (an entry rule plus the exit rule) has passed a
                     prospective paper cohort: enough closed positions, from different creators, whose
                     lower confidence bound on net return after quoted fills, gas and costs is positive
  warnings right     verdicts that reached a check show skill over the base rate: avoid verdicts rug
                     more often than average and healthy verdicts less often. The public scout's grade;
                     it moves the number, the trading gate is the paper cohort
  runway             a real treasury that covers the 90-day reserve
  surplus            real money above the reserve to trade with
Real trades unlock at READY_AT, only while a paper cohort holds a fresh pass, and start at the minimum size."""
import os

READY_AT = int(os.environ.get("WH_READY_AT", "80"))
VERDICTS_FULL = int(os.environ.get("WH_READY_VERDICTS", "20"))  # checked verdicts for full marks on volume
EDGE_FULL = 0.10       # a finished cohort's lower bound on net return per dollar risked that earns full marks
SKILL_FULL = 0.30      # skill over the base rate that earns full marks on "warnings right"
HEALTHY_MIN = 10       # "looks healthy" verdicts checked before "warnings right" may pass 50
WEIGHTS = {"lab": 0.50, "accuracy": 0.15, "runway": 0.20, "surplus": 0.15}


def _pct(x):
    return max(0, min(100, int(round(100 * x))))


def _checked(card, verdict):
    """Outcome counts for one verdict, unknown outcomes left out."""
    return {k: int(v or 0) for k, v in (card.get(verdict) or {}).items() if k != "unknown"}


def _bad(counts):
    return counts.get("rugged", 0) + counts.get("dumped", 0)


def skill(card):
    """Skill of the verdicts over the base rate, 0..1, from the scorecard of resolved outcomes.
    Half comes from avoid verdicts going bad more often than the base rate, half from healthy verdicts
    going bad less often. Returns (skill, info)."""
    avoid, healthy, mixed = _checked(card, "avoid"), _checked(card, "looks healthy"), _checked(card, "mixed")
    n_avoid, n_healthy, n_mixed = sum(avoid.values()), sum(healthy.values()), sum(mixed.values())
    n = n_avoid + n_healthy + n_mixed
    info = {"checked": n, "avoid": n_avoid, "healthy": n_healthy, "mixed": n_mixed,
            "base": None, "avoid_bad": None, "healthy_bad": None}
    if not n:
        return 0.0, info
    base = (_bad(avoid) + _bad(healthy) + _bad(mixed)) / n
    avoid_bad = (_bad(avoid) / n_avoid) if n_avoid else None
    healthy_bad = (_bad(healthy) / n_healthy) if n_healthy else None
    s = 0.0
    if avoid_bad is not None:
        s += 0.5 * max(0.0, avoid_bad - base) / max(1e-9, 1.0 - base)
    if healthy_bad is not None:
        s += 0.5 * max(0.0, base - healthy_bad) / max(1e-9, base)
    info.update(base=base, avoid_bad=avoid_bad, healthy_bad=healthy_bad)
    return min(1.0, s), info


def compute(brain_summary, lab_summary, runway, min_trade_usd=10.0):
    parts = []

    # 1. strategy proven: the paper cohort of the rule that is furthest along (strategy_validation)
    v = lab_summary.get("validation") or {}
    required = int(v.get("required") or 50)
    passed = bool(v.get("passed"))
    lcb = v.get("lcb")
    if passed:
        score, detail = 100, "paper cohort passed for %s: %+.0f%% a trade after costs, lower bound %+.0f%%" % (
            ", ".join(v.get("passed_rules") or [v.get("rule") or "the strategy"]), 100 * (v.get("mean_ret") or 0), 100 * (lcb or 0))
    else:
        filled = min(1.0, (v.get("n") or 0) / required)
        edge = max(0.0, min(1.0, lcb / EDGE_FULL)) if lcb is not None else 0.0
        score = _pct(0.5 * filled + 0.5 * edge)
        detail = "paper cohort%s: %d of %d positions closed (%d opened)" % (
            (" for " + v["rule"]) if v.get("rule") else "", v.get("n") or 0, required, v.get("enrolled") or 0)
        if lcb is not None:
            detail += "; the last finished cohort averaged %+.0f%% a trade with a lower bound of %+.0f%% (not proven)" % (
                100 * (v.get("mean_ret") or 0), 100 * lcb)
    parts.append({"id": "lab", "label": "strategy proven on paper", "score": score, "detail": detail,
                  "positive": passed, "lcb": (round(lcb, 4) if lcb is not None else None)})

    # 2. warnings right: skill over the base rate, across avoid, mixed and healthy verdicts
    card = brain_summary.get("validated_scorecard", brain_summary.get("scorecard")) or {}
    sk, info = skill(card)
    n, n_healthy = info["checked"], info["healthy"]
    avoid, healthy = _checked(card, "avoid"), _checked(card, "looks healthy")
    n_avoid = sum(avoid.values())
    right = _bad(avoid) + (n_healthy - _bad(healthy))
    accuracy = (right / (n_avoid + n_healthy)) if (n_avoid + n_healthy) else None
    if n:
        score = 0.4 * min(1.0, n / VERDICTS_FULL) + 0.6 * min(1.0, sk / SKILL_FULL)
        if n_healthy < HEALTHY_MIN:
            score = min(score, 0.5)
        detail = "%d verdicts checked: %d%% went bad overall; avoid verdicts %s bad, healthy verdicts %s bad; skill %d%% of full" % (
            n, round(100 * info["base"]),
            ("%d%%" % round(100 * info["avoid_bad"])) if info["avoid_bad"] is not None else "n/a",
            ("%d%%" % round(100 * info["healthy_bad"])) if info["healthy_bad"] is not None else "n/a",
            round(100 * min(1.0, sk / SKILL_FULL)))
        if n_healthy < HEALTHY_MIN:
            detail += "; capped at 50 until %d healthy verdicts are checked (%d so far)" % (HEALTHY_MIN, n_healthy)
    else:
        score, detail = 0.0, "no verdict has reached a check yet"
    if 'validated_scorecard' in brain_summary:
        detail += "; complete versioned assessments only; legacy history excluded"
    parts.append({"id": "accuracy", "label": "warnings right", "score": _pct(score), "detail": detail,
                  "accuracy_pct": round(100 * accuracy) if accuracy is not None else None, "checked": n,
                  "skill_pct": round(100 * sk), "base_rate_pct": round(100 * info["base"]) if info["base"] is not None else None,
                  "healthy_checked": n_healthy})

    # 3. runway, real money only
    treasury = float(runway.get("treasury_usd") or 0)
    reserve_days = runway.get("reserve_days") or 90
    if treasury <= 0:
        parts.append({"id": "runway", "label": "runway", "score": 0, "detail": "no treasury yet"})
    else:
        days = float(runway.get("runway_days_no_income") or 0)
        parts.append({"id": "runway", "label": "runway", "score": _pct(days / reserve_days),
                      "detail": "%.0f days of costs covered; the reserve is %d days" % (days, reserve_days)})

    # 4. surplus above the reserve, real money only
    if treasury <= 0:
        parts.append({"id": "surplus", "label": "surplus", "score": 0, "detail": "no real surplus yet"})
    else:
        surplus = float(runway.get("surplus_usd") or 0)
        parts.append({"id": "surplus", "label": "surplus", "score": _pct(max(0.0, surplus) / min_trade_usd),
                      "detail": "$%.2f above the reserve; the first trade needs $%.0f" % (surplus, min_trade_usd)})

    total = int(round(sum(WEIGHTS[p["id"]] * p["score"] for p in parts)))
    ready = bool(passed and total >= READY_AT and runway.get("can_invest"))
    nxt = parts[0] if not passed else next((p for p in parts if p["score"] < 100), None)
    return {"score": total, "ready_at": READY_AT, "ready": ready, "parts": parts, "weights": WEIGHTS,
            "next": (nxt["label"] + ": " + nxt["detail"]) if nxt else "every part is at full marks",
            "gate": "real trades require %d%% readiness, a fresh pass of a prospective paper cohort and real surplus above the reserve; execution safety gates, the trading budget and the daily loss breaker also apply" % READY_AT}
