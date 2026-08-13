"""
Ranking: which high-population, low-gem-rate cards to look at first.

GemRate no longer publishes recent sale prices (they moved to the CardLadder
integration), so the original price-momentum signal isn't available from the
site anymore. What GemRate does give per card is population + gem rate, so the
score is built from the two components that remain:

  gem_rate_tightness  A low gem rate on a big population = a card that is hard
                      to get in a gem grade and is heavily traded. Scaled by
                      log10(population) so a tight rate on 70,000 graded beats
                      the same rate on 400 graded.
  volume              log10(population) on its own — how liquid / actively
                      graded the card is.

Both are normalized 0-1 across the candidate pool. The config weights
`gem_rate_tightness` and `sales_activity` map to the two components; the old
`price_momentum` weight is folded into tightness so existing config.json files
keep working. Because GemRate dropped sale prices, treat results as a shortlist
to verify manually (eBay/CardLadder solds), not a buy list.
"""

import math


def analyze(card, filters):
    """Apply the population / gem-rate filters. Returns a metrics dict, or
    None if the card doesn't qualify."""
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
    }


def rank(candidates, weights, max_gem_rate):
    """Normalize the available components across the pool and sort."""
    if not candidates:
        return []

    def norm(values):
        lo, hi = min(values), max(values)
        if hi == lo:
            return [0.5] * len(values)
        return [(v - lo) / (hi - lo) for v in values]

    # tightness: how far below the gem-rate ceiling, scaled by log(population)
    tight_raw = [
        (1.0 - c["gem_rate_pct"] / max_gem_rate) * math.log10(max(c["total_pop"], 10))
        for c in candidates
    ]
    tightness = norm(tight_raw)
    volume = norm([math.log10(max(c["total_pop"], 10)) for c in candidates])

    # Fold the (now unavailable) price-momentum weight into tightness so
    # existing config weights still sum sensibly.
    w_tight = weights.get("gem_rate_tightness", 0.3) + weights.get("price_momentum", 0.0)
    w_vol = weights.get("sales_activity", 0.2)
    total_w = w_tight + w_vol or 1.0

    for i, c in enumerate(candidates):
        c["score"] = round(
            (w_tight * tightness[i] + w_vol * volume[i]) / total_w,
            4,
        )

    return sorted(candidates, key=lambda c: c["score"], reverse=True)
