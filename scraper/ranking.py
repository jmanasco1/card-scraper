"""
Ranking: which cards look undervalued / about to move.

Signal comes from three parts, normalized 0-1 across the candidate pool and
weighted per config.json:

  price_momentum      Recent eBay PSA-10 sale prices trending up vs. the older
                      half of the window. This is the "market is repricing it"
                      signal (from scraper/comps.py, via eBay — GemRate no
                      longer publishes prices).
  gem_rate_tightness  Low gem rate on a big population = scarce in gem grade
                      and heavily traded. Scaled by log10(population).
  sales_activity      How many recent comps we found — a busy card is a
                      confirmed trend, not one odd auction.

Cards with too few comps to judge momentum still rank on scarcity, just with a
neutral momentum contribution.
"""

import math


def analyze(card, filters):
    """Apply the population / gem-rate filters. Returns a metrics dict, or
    None if the card doesn't qualify. Comp fields are filled in later."""
    if card.total_pop < filters["min_total_population"]:
        return None
    if not (0 < card.gem_rate_pct <= filters["max_gem_rate_pct"]):
        return None
    return {
        "set": card.set_name,
        "year": card.year,
        "card": card.card_name,
        "number": card.card_number,
        "parallel": card.parallel,
        "url": card.url,
        "total_pop": card.total_pop,
        "gem_pop": card.gem_pop,
        "gem_rate_pct": card.gem_rate_pct,
        "recent_sales": 0,
        "latest_comp": None,
        "oldest_comp": None,
        "momentum_pct": 0.0,
    }


def rank(candidates, weights, max_gem_rate):
    """Normalize the components across the pool and sort by score."""
    if not candidates:
        return []

    def norm(values):
        lo, hi = min(values), max(values)
        if hi == lo:
            return [0.5] * len(values)
        return [(v - lo) / (hi - lo) for v in values]

    momentum = norm([c.get("momentum_pct", 0.0) for c in candidates])

    tight_raw = [
        (1.0 - c["gem_rate_pct"] / max_gem_rate) * math.log10(max(c["total_pop"], 10))
        for c in candidates
    ]
    tightness = norm(tight_raw)
    activity = norm([c.get("recent_sales", 0) for c in candidates])

    w_mom = weights.get("price_momentum", 0.5)
    w_tight = weights.get("gem_rate_tightness", 0.3)
    w_act = weights.get("sales_activity", 0.2)
    total_w = (w_mom + w_tight + w_act) or 1.0

    for i, c in enumerate(candidates):
        c["score"] = round(
            (w_mom * momentum[i] + w_tight * tightness[i] + w_act * activity[i]) / total_w,
            4,
        )

    return sorted(candidates, key=lambda c: c["score"], reverse=True)
