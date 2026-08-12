"""
Ranking: which cards should be bought first.

Score = w1 * price_momentum + w2 * gem_rate_tightness + w3 * sales_activity
All three components are normalized to 0-1 across the candidate pool, so the
weights in config.json directly express relative importance.

  price_momentum      %% change from oldest to newest comp in the window.
                      A card that went $256 -> $375 in six weeks scores high.
  gem_rate_tightness  Low gem rate on a big population = scarcity the market
                      may not have priced in yet. Scaled by log(population) so
                      an 8%% gem rate on 8,000 cards beats 8%% on 350 cards.
  sales_activity      Number of qualifying recent sales. More sales = the
                      trend is confirmed, not one weird auction.
"""

import math
from datetime import date, timedelta


def analyze(card, filters):
    """Compute per-card metrics. Returns None if the card fails filters."""
    window = filters["recent_sales_window_days"]
    cutoff = (date.today() - timedelta(days=window)).isoformat()

    comps = sorted(
        [c for c in card.comps if c["date"] >= cutoff],
        key=lambda c: c["date"],
    )

    if len(comps) < filters["min_recent_sales"]:
        return None

    latest_price = comps[-1]["price"]
    oldest_price = comps[0]["price"]
    if latest_price < filters["min_price_usd"]:
        return None

    momentum_pct = 0.0
    if oldest_price > 0:
        momentum_pct = 100.0 * (latest_price - oldest_price) / oldest_price

    return {
        "set": card.set_name,
        "card": card.card_name,
        "number": card.card_number,
        "url": card.url,
        "total_pop": card.total_pop,
        "gem_pop": card.gem_pop,
        "gem_rate_pct": card.gem_rate_pct,
        "grade_breakdown": card.grade_breakdown,
        "recent_sales": len(comps),
        "oldest_comp": oldest_price,
        "latest_comp": latest_price,
        "momentum_pct": round(momentum_pct, 1),
        "comps": comps,
    }


def rank(candidates, weights, max_gem_rate):
    """Normalize components across the pool and produce a sorted list."""
    if not candidates:
        return []

    def norm(values):
        lo, hi = min(values), max(values)
        if hi == lo:
            return [0.5] * len(values)
        return [(v - lo) / (hi - lo) for v in values]

    momentum = norm([c["momentum_pct"] for c in candidates])

    # tightness: (how far below the gem-rate ceiling) * log-scaled population
    tight_raw = [
        (1.0 - c["gem_rate_pct"] / max_gem_rate) * math.log10(max(c["total_pop"], 10))
        for c in candidates
    ]
    tightness = norm(tight_raw)

    activity = norm([c["recent_sales"] for c in candidates])

    for i, c in enumerate(candidates):
        c["score"] = round(
            weights["price_momentum"] * momentum[i]
            + weights["gem_rate_tightness"] * tightness[i]
            + weights["sales_activity"] * activity[i],
            4,
        )

    return sorted(candidates, key=lambda c: c["score"], reverse=True)
