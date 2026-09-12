"""Readiness: how close the worm is to trading real money, measured from evidence only.

Four parts, each 0-100, weighted into one number. None of them can be raised by hand:
  exit rule proven   the strategy lab has resolved cases and its best rule is net positive after costs
  warnings right     verdicts that reached their 24-hour check and proved right
  runway             a real treasury that covers the 90-day reserve
  surplus            real money above the reserve to trade with
Demo treasury counts for nothing. Real trades unlock at READY_AT and start at the minimum size."""
import os

from . import lab

READY_AT = int(os.environ.get("WH_READY_AT", "80"))
CASES_FULL = int(os.environ.get("WH_READY_CASES", "30"))        # resolved lab cases for full marks on volume
VERDICTS_FULL = int(os.environ.get("WH_READY_VERDICTS", "20"))  # checked verdicts for full marks on volume
EDGE_FULL = 0.10       # best arm's average net return per dollar risked that earns full marks
ACCURACY_FULL = 0.70   # share of checked verdicts that proved right that earns full marks
WEIGHTS = {"lab": 0.35, "accuracy": 0.30, "runway": 0.20, "surplus": 0.15}


def _pct(x):
    return max(0, min(100, int(round(100 * x))))


def compute(brain_summary, lab_summary, runway, treasury_is_demo, min_trade_usd=10.0):
    parts = []

    # 1. exit rule proven
    arms = lab_summary.get("arms") or []
    best = next((a for a in arms if a["n"] >= lab.LAB_MIN_N and a["mean_ret"] is not None), None)
    resolved = lab_summary.get("cases_resolved") or 0
    volume = min(1.0, resolved / CASES_FULL)
    edge = max(0.0, min(1.0, best["mean_ret"] / EDGE_FULL)) if best else 0.0
    if best is None:
        most = max((a["n"] for a in arms), default=0)
        detail = "%d resolved cases; no exit rule has %d cases yet (%d more)" % (resolved, lab.LAB_MIN_N, max(0, lab.LAB_MIN_N - most))
    else:
        detail = "best rule %s averages %+.0f%% net after costs over %d cases" % (best["arm"], best["mean_ret"] * 100, best["n"])
    parts.append({"id": "lab", "label": "exit rule proven", "score": _pct(0.5 * volume + 0.5 * edge),
                  "detail": detail, "positive": bool(best and best["mean_ret"] > 0)})

    # 2. warnings right: avoid verdicts that went bad, healthy verdicts that did not
    card = brain_summary.get("scorecard") or {}
    avoid = dict(card.get("avoid") or {})
    healthy = dict(card.get("looks healthy") or {})
    n_avoid = sum(v for k, v in avoid.items() if k != "unknown")
    n_healthy = sum(v for k, v in healthy.items() if k != "unknown")
    right = (avoid.get("rugged", 0) + avoid.get("dumped", 0)) + (n_healthy - healthy.get("rugged", 0) - healthy.get("dumped", 0))
    n = n_avoid + n_healthy
    accuracy = (right / n) if n else None
    if n:
        score = 0.4 * min(1.0, n / VERDICTS_FULL) + 0.6 * min(1.0, accuracy / ACCURACY_FULL)
        detail = "%d of %d checked verdicts proved right (%d%%); %d checks for full marks" % (right, n, round(100 * accuracy), VERDICTS_FULL)
    else:
        score, detail = 0.0, "no verdict has reached its 24-hour check yet"
    parts.append({"id": "accuracy", "label": "warnings right", "score": _pct(score), "detail": detail,
                  "accuracy_pct": round(100 * accuracy) if accuracy is not None else None, "checked": n})

    # 3. runway, real money only
    treasury = float(runway.get("treasury_usd") or 0)
    reserve_days = runway.get("reserve_days") or 90
    if treasury_is_demo or treasury <= 0:
        parts.append({"id": "runway", "label": "runway", "score": 0, "detail": "no real treasury yet; demo money counts for nothing"})
    else:
        days = float(runway.get("runway_days_no_income") or 0)
        parts.append({"id": "runway", "label": "runway", "score": _pct(days / reserve_days),
                      "detail": "%.0f days of costs covered; the reserve is %d days" % (days, reserve_days)})

    # 4. surplus above the reserve, real money only
    if treasury_is_demo or treasury <= 0:
        parts.append({"id": "surplus", "label": "surplus", "score": 0, "detail": "no real surplus yet"})
    else:
        surplus = float(runway.get("surplus_usd") or 0)
        parts.append({"id": "surplus", "label": "surplus", "score": _pct(max(0.0, surplus) / min_trade_usd),
                      "detail": "$%.2f above the reserve; the first trade needs $%.0f" % (surplus, min_trade_usd)})

    total = int(round(sum(WEIGHTS[p["id"]] * p["score"] for p in parts)))
    ready = bool(total >= READY_AT and parts[0]["positive"] and runway.get("can_invest") and not treasury_is_demo)
    nxt = next((p for p in parts if p["score"] < 100), None)
    return {"score": total, "ready_at": READY_AT, "ready": ready, "parts": parts, "weights": WEIGHTS,
            "next": (nxt["label"] + ": " + nxt["detail"]) if nxt else "every part is at full marks",
            "gate": "real trades unlock at %d%% readiness and start at the minimum size; the number only moves on evidence" % READY_AT}
