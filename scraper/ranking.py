"""
Filtering, sampling, and the final score.

The previous version of this module ranked on population and gem rate, which
between them encode "famous" and "old" and say nothing about price. This one
ranks on measured underpricing from scraper/value.py, scaled by how much the
underlying comps can be trusted.
"""

import statistics


def analyze(card, filters):
    """Apply the universe filters. Returns a metrics dict, or None.

    Note the population filter is now a *band*, not a floor. The floor keeps
    out cards too thin to have a real market; the ceiling keeps out the
    mega-population blue chips, which are the most heavily watched cards in the
    hobby and therefore the least likely to be mispriced.
    """
    pop = card.total_pop
    if pop < filters["min_total_population"]:
        return None
    if filters.get("max_total_population") and pop > filters["max_total_population"]:
        return None

    rate = card.gem_rate_pct
    # A wide gem-rate band is deliberate: the value model fits a curve of
    # premium against scarcity, and it needs cards spread across the scarcity
    # range to fit anything at all. Filtering to only-scarce cards would
    # flatten the x-axis and make the fit meaningless.
    if not (filters["min_gem_rate_pct"] <= rate <= filters["max_gem_rate_pct"]):
        return None

    return {
        "set": card.set_name,
        "year": card.year,
        "card": card.card_name,
        "number": card.card_number,
        "parallel": card.parallel,
        "url": card.url,
        "total_pop": pop,
        "gem_pop": card.gem_pop,
        "gem_rate_pct": rate,
        "p10_median": None, "p10_sales": 0, "p10_spread": None,
        "p9_median": None, "p9_sales": 0, "p9_spread": None,
        "gap": None, "expected_gap": None, "value_ratio": None, "discount_pct": None,
    }


def select_for_pricing(candidates, limit):
    """Choose which candidates to spend eBay lookups on.

    Every lookup is slow and may cost a captcha, so we can only price a few
    dozen per run. Picking the "most promising" by any single metric would be a
    mistake: the model fits premium against gem rate, so a sample bunched at one
    end of the gem-rate range has no x-variation and yields a garbage curve.

    So we stratify — sort by gem rate, cut into `limit` even buckets, and take
    the median-population card from each. That spans the scarcity range while
    favouring cards with enough population to have a real market.
    """
    if limit >= len(candidates):
        return list(candidates)
    if limit <= 0:
        return []

    ordered = sorted(candidates, key=lambda m: m["gem_rate_pct"])
    picked = []
    n = len(ordered)
    for i in range(limit):
        lo = (i * n) // limit
        hi = max(lo + 1, ((i + 1) * n) // limit)
        bucket = ordered[lo:hi]
        bucket.sort(key=lambda m: m["total_pop"])
        picked.append(bucket[len(bucket) // 2])
    return picked


def liquidity(m):
    """Total observed sales across both grades — how easily you could actually
    transact. A big paper discount on a card that sells twice a year isn't an
    opportunity you can act on."""
    return m.get("p10_sales", 0) + m.get("p9_sales", 0)


def dislocation(m):
    """How far this card's PSA 10 has moved against its own PSA 9.

    Both grades are the same card, same player, same collector base, so
    whatever drives demand moves them together. When the 10 falls and the 9
    doesn't, that gap is a real dislocation in the thing itself — not an
    inference from how other cards are priced. Returns percentage points,
    negative when the 10 has fallen relative to the 9, or None when either
    grade lacks enough dated sales to say.
    """
    t10, t9 = m.get("p10_trend"), m.get("p9_trend")
    if not t10 or not t9:
        return None
    return round(t10["change_pct"] - t9["change_pct"], 1)


def score(m, conf, weights):
    """Expected edge, in percentage points of underpricing.

    Kept deliberately interpretable: a score of 30 means "trades about 30%
    below what this cohort pays for that level of scarcity, discounted for how
    trustworthy the comps are". Negative means richly priced.
    """
    if m.get("discount_pct") is None:
        return None
    edge = m["discount_pct"] * conf
    # A small liquidity kicker so that, between two similar discounts, the one
    # you can actually buy and resell ranks higher. Applied only to positive
    # edges: being easy to trade doesn't make an overpriced card less
    # overpriced, and scaling a negative edge by it would rank the worst cards
    # up rather than down.
    if edge > 0:
        liq_w = weights.get("liquidity", 0.15)
        edge *= 1.0 + min(1.0, liquidity(m) / 40.0) * liq_w
    return round(edge, 2)


def rank(candidates, weights, min_sales, min_edge_usd=25.0, min_move_pct=15.0):
    """Rank on within-card price dislocation, best first.

    Cards are listed when their PSA 10 has fallen meaningfully against their
    own PSA 9. Nothing here depends on predicting what a card *should* cost:
    the cross-card model that tried to do that produced fair values spanning
    prices no card had ever sold for, because the 10/9 premium varies from
    roughly 2x to 20x for reasons no available field explains. A card measured
    against its own recent history needs none of that.
    """
    from .value import confidence

    scored = []
    for m in candidates:
        conf = confidence(m, min_sales)
        m["confidence"] = conf
        m["liquidity"] = liquidity(m)
        m["dislocation_pct"] = dislocation(m)

        if conf <= 0 or m["dislocation_pct"] is None:
            continue
        # Only the 10 falling behind counts; a 10 running ahead of its 9 is a
        # hot card, not a cheap one.
        if m["dislocation_pct"] > -min_move_pct:
            continue

        t10 = m["p10_trend"]
        drop_usd = t10["older_median"] - t10["recent_median"]
        if drop_usd < min_edge_usd:
            continue

        m["drop_usd"] = round(drop_usd, 2)
        # Magnitude of the dislocation, damped by how trustworthy the comps are.
        m["score"] = round(abs(m["dislocation_pct"]) * conf, 1)
        m["is_finding"] = True
        scored.append(m)

    scored.sort(key=lambda m: m["score"], reverse=True)
    return scored


def measured(candidates, min_sales):
    """Every card we successfully priced at both grades, with its history.

    Reporting only the cards that clear the dislocation threshold means a run
    where nothing clears it shows an empty page, which is indistinguishable
    from a run that failed — and throws away fifty-odd cards of real, verified
    sale prices that were expensive to collect. Findings are flagged; the rest
    are still worth seeing.
    """
    from .value import confidence

    out = []
    for m in candidates:
        if not (m.get("p10_trend") and m.get("p9_trend")):
            continue
        m.setdefault("confidence", confidence(m, min_sales))
        m.setdefault("liquidity", liquidity(m))
        if m.get("dislocation_pct") is None:
            m["dislocation_pct"] = dislocation(m)
        m.setdefault("is_finding", False)
        m.setdefault("score", round(abs(m["dislocation_pct"] or 0) * (m["confidence"] or 0), 1))
        out.append(m)

    out.sort(key=lambda m: m.get("dislocation_pct") or 0)
    return out


def cohort_stats(candidates):
    """Descriptive numbers for the run header, so the output says what pool the
    discounts are measured against."""
    priced = [m for m in candidates if m.get("gap")]
    if not priced:
        return {}
    years = [int(m["year"]) for m in priced if str(m["year"]).isdigit()]
    return {
        "priced": len(priced),
        "median_gap": round(statistics.median(m["gap"] for m in priced), 2),
        "median_year": int(statistics.median(years)) if years else None,
        "median_pop": int(statistics.median(m["total_pop"] for m in priced)),
    }
