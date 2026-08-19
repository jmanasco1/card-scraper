"""
Relative-value model: which PSA 10s are cheap for how hard they are to get.

The old scanner ranked on population + gem rate, which is a fame detector —
the most-graded cards are the most famous ones, and low gem rates just mean
"old cardboard with bad centering". Neither says anything about price, so the
output was a list of expensive vintage blue chips.

This module scores the thing that actually is an inefficiency: **the grade
gap**. For any card the market pays a premium for the PSA 10 over the PSA 9:

    gap = median(PSA 10 solds) / median(PSA 9 solds)

That premium *should* scale with how hard the 10 is to get. A card that gems
2% of the time has a genuinely scarce 10 and should trade at a fat multiple; a
card that gems 40% of the time should not. When a card's actual gap sits well
below what its gem rate implies, the 10 is cheap — that is the inefficiency.

We don't assume the relationship, we fit it from the scanned cohort:

    log(gap) = a + b * log(100 / gem_rate)

fitted by least squares over every card we priced, then

    value_ratio = actual_gap / expected_gap

`value_ratio` < 1 means the 10 trades below the cohort's own pricing of that
scarcity -> underpriced. > 1 means richly priced.

Two properties matter here:

  * It is **cohort-relative**. Each card is judged against how this particular
    pool prices scarcity, so a modern-set scan and a vintage scan are each
    scored on their own terms. Nothing biases it toward old cards.
  * It is **price-anchored**. Unlike the old score, a card cannot rank without
    real sold prices on both sides, so the ranking can never collapse into a
    constant the way the previous one did.
"""

import math
import statistics

# Gem rates at or below this are treated as this value. Sub-0.1% rates come
# from tiny gem counts on huge populations, where one more 10 moves the rate
# by half, and log(100/rate) would otherwise dominate the fit.
MIN_GEM_RATE = 0.10

# Below this many cards we can't fit a slope worth trusting, so we fall back
# to a flat model (expected gap = cohort median gap).
MIN_COHORT_FOR_FIT = 8

# Fraction of worst-fitting cards dropped before the final refit. The whole
# point of the scan is to find cards that sit far off the curve, but on a
# single pass those same cards drag the curve toward themselves and shrink
# their own measured discount. One trimmed refit gives us a baseline built
# from the cards that price normally, and measures the outliers against that.
TRIM_FRACTION = 0.20


def _scarcity_x(gem_rate_pct):
    """Model input: log of 'how many graded per gem'. A 2% gem rate -> log(50),
    a 40% rate -> log(2.5). Higher = harder 10."""
    return math.log(100.0 / max(gem_rate_pct, MIN_GEM_RATE))


def _fit_loglog(points):
    """Least-squares fit of y = a + b*x over [(x, y)]. Returns (a, b)."""
    n = len(points)
    mean_x = sum(p[0] for p in points) / n
    mean_y = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mean_x) ** 2 for p in points)
    if sxx == 0:
        return mean_y, 0.0
    sxy = sum((p[0] - mean_x) * (p[1] - mean_y) for p in points)
    b = sxy / sxx
    return mean_y - b * mean_x, b


def _r_squared(points, a, b):
    """How much of the cohort's gap variation the fit explains. Low values mean
    the cohort prices scarcity inconsistently — which is itself worth knowing,
    so we report it rather than hiding it."""
    n = len(points)
    mean_y = sum(p[1] for p in points) / n
    ss_tot = sum((p[1] - mean_y) ** 2 for p in points)
    if ss_tot == 0:
        return 0.0
    ss_res = sum((p[1] - (a + b * p[0])) ** 2 for p in points)
    return max(0.0, 1.0 - ss_res / ss_tot)


def priceable(m):
    """A card can be scored only if both grades have real sold prices."""
    return bool(m.get("p10_median")) and bool(m.get("p9_median")) and m["p9_median"] > 0


def fit_cohort(candidates):
    """Fit the scarcity->premium curve over every priceable candidate.

    Returns a dict describing the fitted model, including a `predict`
    callable mapping gem_rate_pct -> expected gap.
    """
    priced = [m for m in candidates if priceable(m)]
    gaps = [m["p10_median"] / m["p9_median"] for m in priced]

    if len(priced) < MIN_COHORT_FOR_FIT:
        median_gap = statistics.median(gaps) if gaps else 1.0
        return {
            "kind": "flat",
            "n": len(priced),
            "median_gap": median_gap,
            "r_squared": 0.0,
            "predict": lambda _rate: median_gap,
        }

    points = [
        (_scarcity_x(m["gem_rate_pct"]), math.log(gap))
        for m, gap in zip(priced, gaps)
        if gap > 0
    ]
    a, b = _fit_loglog(points)

    # Refit on the well-behaved core so the outliers we're hunting don't set
    # the standard they're judged against.
    keep = len(points) - int(len(points) * TRIM_FRACTION)
    trimmed = points
    if keep >= MIN_COHORT_FOR_FIT:
        trimmed = sorted(points, key=lambda p: abs(p[1] - (a + b * p[0])))[:keep]
        a, b = _fit_loglog(trimmed)

    return {
        "kind": "loglog",
        "n": len(priced),
        "n_fit": len(trimmed),
        "intercept": a,
        "slope": b,
        "median_gap": statistics.median(gaps),
        # R^2 is reported over the trimmed core the curve was actually fitted
        # to; it describes how consistently the normal cards price scarcity.
        "r_squared": _r_squared(trimmed, a, b),
        "predict": lambda rate: math.exp(a + b * _scarcity_x(rate)),
    }


def score_card(m, model):
    """Attach gap / expected-gap / value_ratio to one candidate.

    Returns True if the card was scoreable, False if it lacked prices.
    """
    if not priceable(m):
        m["gap"] = None
        m["expected_gap"] = None
        m["value_ratio"] = None
        m["discount_pct"] = None
        return False

    gap = m["p10_median"] / m["p9_median"]
    expected = model["predict"](m["gem_rate_pct"])
    m["gap"] = round(gap, 2)
    m["expected_gap"] = round(expected, 2)
    if expected > 0:
        ratio = gap / expected
        m["value_ratio"] = round(ratio, 3)
        # How far below fair the 10 trades, as a percentage. Positive = cheap.
        m["discount_pct"] = round(100.0 * (1.0 - ratio), 1)
    else:
        m["value_ratio"] = None
        m["discount_pct"] = None
    return True


def confidence(m, min_sales):
    """0-1 confidence that this card's gap is real and not comp noise.

    Thin comps make a ratio meaningless, so this scales the final score rather
    than filtering silently — a high-discount card built on 3 sales should not
    outrank a moderate one built on 20.
    """
    n10 = m.get("p10_sales", 0)
    n9 = m.get("p9_sales", 0)
    if n10 < min_sales or n9 < min_sales:
        return 0.0
    # Saturates at ~12 sales a side; more than that adds little certainty.
    depth = min(1.0, (min(n10, n9) - min_sales + 1) / 12.0)
    # Wide dispersion within a grade means the "median" is standing in for a
    # mix of different cards (wrong parallel, wrong year), so trust it less.
    spread_penalty = 1.0
    for key in ("p10_spread", "p9_spread"):
        s = m.get(key)
        if s is not None and s > 0.6:
            spread_penalty *= 0.6
    return round(max(0.0, min(1.0, depth * spread_penalty)), 3)
