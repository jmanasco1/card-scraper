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
#
# Two regressors plus an intercept need real data behind them. Fitting three
# parameters to eight points produces a curve that passes beautifully through
# the noise and predicts nothing, then reports a confident-looking dollar value
# built on it. Thirty is not generous — it is the point below which the
# prediction band is so wide that nothing clears it anyway.
MIN_COHORT_FOR_FIT = 30

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


def _solve(matrix):
    """Gaussian elimination with partial pivoting on an augmented matrix."""
    n = len(matrix)
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(matrix[r][col]))
        if abs(matrix[pivot][col]) < 1e-12:
            return None
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        pv = matrix[col][col]
        matrix[col] = [v / pv for v in matrix[col]]
        for r in range(n):
            if r == col:
                continue
            f = matrix[r][col]
            if f:
                matrix[r] = [v - f * w for v, w in zip(matrix[r], matrix[col])]
    return [row[n] for row in matrix]


def _fit(points):
    """Least squares of y = a + b*x1 + c*x2 over [(x1, x2, y)].

    Two regressors, not one. Fitting the grade premium on scarcity alone treats
    a junk-wax common that gems 2% of the time as if it should command the same
    premium as a star rookie that gems 2% of the time. It shouldn't: gem rate
    is a supply measure, and a premium only exists where there is demand for
    the 10. The PSA 9 price is the demand proxy we already have — a card whose
    9 trades at $200 is wanted in a way a $2 card is not — so it enters as the
    second regressor. Without it the model reliably over-values obscure,
    hard-to-gem, unwanted cards, which is exactly what it did.
    """
    n = len(points)
    if n < 3:
        mean_y = sum(p[2] for p in points) / n if n else 0.0
        return mean_y, 0.0, 0.0
    s1 = sum(p[0] for p in points)
    s2 = sum(p[1] for p in points)
    sy = sum(p[2] for p in points)
    s11 = sum(p[0] * p[0] for p in points)
    s12 = sum(p[0] * p[1] for p in points)
    s22 = sum(p[1] * p[1] for p in points)
    s1y = sum(p[0] * p[2] for p in points)
    s2y = sum(p[1] * p[2] for p in points)
    sol = _solve([[n, s1, s2, sy],
                  [s1, s11, s12, s1y],
                  [s2, s12, s22, s2y]])
    if sol is None:
        mean_y = sy / n
        return mean_y, 0.0, 0.0
    return sol[0], sol[1], sol[2]


def _demand_x(p9_median):
    """Second model input: the log of the PSA 9 price, standing in for how much
    anyone actually wants this card."""
    return math.log(max(p9_median, 1.0))


def _r_squared(points, a, b, c=0.0):
    """How much of the cohort's gap variation the fit explains. Low values mean
    the cohort prices scarcity inconsistently — which is itself worth knowing,
    so we report it rather than hiding it."""
    n = len(points)
    mean_y = sum(p[2] for p in points) / n
    ss_tot = sum((p[2] - mean_y) ** 2 for p in points)
    if ss_tot == 0:
        return 0.0
    ss_res = sum((p[2] - (a + b * p[0] + c * p[1])) ** 2 for p in points)
    return max(0.0, 1.0 - ss_res / ss_tot)


# A PSA 10 selling below its own PSA 9 is not a bargain, it is proof the two
# searches found different cards. Real 10s carry a premium; the gap is >= 1 for
# any genuinely matched pair. Anything under this is dropped as bad data rather
# than scored, because the model would otherwise read it as a huge discount —
# which is exactly how a $550 Larry Bird 10 came to be called $37,000 of value.
MIN_CREDIBLE_GAP = 1.0


def priceable(m):
    """A card can be scored only if both grades have credible sold prices."""
    if not (m.get("p10_median") and m.get("p9_median")):
        return False
    if m["p9_median"] <= 0:
        return False
    if m["p10_median"] / m["p9_median"] < MIN_CREDIBLE_GAP:
        m["rejected"] = "PSA 10 priced below PSA 9 — the two searches matched different cards"
        return False
    return True


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
            "resid_sd": 0.0,
            "predict": lambda _rate, _p9=None: median_gap,
        }

    points = [
        (_scarcity_x(m["gem_rate_pct"]), _demand_x(m["p9_median"]), math.log(gap))
        for m, gap in zip(priced, gaps)
        if gap > 0
    ]
    a, b, c = _fit(points)

    # Refit on the well-behaved core so the outliers we're hunting don't set
    # the standard they're judged against.
    keep = len(points) - int(len(points) * TRIM_FRACTION)
    trimmed = points
    if keep >= MIN_COHORT_FOR_FIT:
        trimmed = sorted(points, key=lambda p: abs(p[2] - (a + b * p[0] + c * p[1])))[:keep]
        a, b, c = _fit(trimmed)

    # Never extrapolate past the scarcity range we actually observed. A 0.49%
    # gem rate sits far outside the bulk of any pool, and an unclamped log-log
    # fit happily predicts a 30x premium there — a number with no support in
    # the data and enough leverage to invent tens of thousands of dollars of
    # "fair value". Predictions outside the observed range are pinned to its
    # edges instead.
    xs = [p[0] for p in trimmed]
    ds = [p[1] for p in trimmed]
    lo_x, hi_x = min(xs), max(xs)
    lo_d, hi_d = min(ds), max(ds)
    observed_gaps = [math.exp(p[2]) for p in trimmed]
    gap_ceiling = max(observed_gaps) * 1.25

    # How far the real cards scatter around the fitted curve. This is the
    # honest width of any prediction: quoting a single "worth $919" hides that
    # the same fit is equally happy with $600 or $1,400, and invites exactly
    # the objection that the card doesn't sell for $919. Nothing does — the
    # model describes a trend, not a price.
    residuals = [p[2] - (a + b * p[0] + c * p[1]) for p in trimmed]
    mean_r = sum(residuals) / len(residuals)
    resid_sd = (sum((r - mean_r) ** 2 for r in residuals) / max(1, len(residuals) - 3)) ** 0.5

    def predict(rate, p9=None):
        x = min(max(_scarcity_x(rate), lo_x), hi_x)
        d = min(max(_demand_x(p9 if p9 else math.exp(sum(ds) / len(ds))), lo_d), hi_d)
        # No card is worth more of a premium than the biggest one this pool
        # actually demonstrated. Predicting past that is how a $70 comp became
        # $1,578 of "fair value".
        return min(math.exp(a + b * x + c * d), gap_ceiling)

    return {
        "kind": "loglog",
        "n": len(priced),
        "n_fit": len(trimmed),
        "intercept": a,
        "slope": b,
        "median_gap": statistics.median(gaps),
        # R^2 is reported over the trimmed core the curve was actually fitted
        # to; it describes how consistently the normal cards price scarcity.
        "demand_slope": c,
        "resid_sd": resid_sd,
        "r_squared": _r_squared(trimmed, a, b, c),
        "x_range": (lo_x, hi_x),
        "gap_ceiling": gap_ceiling,
        "predict": predict,
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
    expected = model["predict"](m["gem_rate_pct"], m["p9_median"])
    m["gap"] = round(gap, 2)
    m["expected_gap"] = round(expected, 2)
    # The discount restated in dollars, which is the only form that answers
    # "what do I pay and what is it worth?". Anchored on the PSA 9 price,
    # since that is the side the market prices most consistently.
    m["fair_p10"] = round(m["p9_median"] * expected, 2)
    m["headroom_usd"] = round(m["fair_p10"] - m["p10_median"], 2)

    # The band the fit actually supports. A card only counts as underpriced if
    # it trades below the *bottom* of it — anywhere inside is consistent with
    # the card being priced normally and the model simply being imprecise.
    sd = model.get("resid_sd") or 0.0
    m["fair_low"] = round(m["p9_median"] * expected * math.exp(-sd), 2)
    m["fair_high"] = round(m["p9_median"] * expected * math.exp(sd), 2)
    m["below_band"] = m["p10_median"] < m["fair_low"]
    # Headroom measured to the conservative edge, not the midpoint.
    m["headroom_usd"] = round(m["fair_low"] - m["p10_median"], 2)
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
