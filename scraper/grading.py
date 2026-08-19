"""
Expected value of buying a card raw and having it graded.

This is the one question in this domain where every input is an observed
number and the answer is a dollar figure with a decision attached:

    buy a raw copy for R, pay G to grade it, and it comes back a 10 about
    `gem_rate` of the time and something lower the rest of the time.

    EV = gem_rate * (net PSA 10 price) + (1 - gem_rate) * (net fallback price)
         - R - G

It is also the only use of GemRate's data that is actually sound. The gem rate
is a probability, and here it weights two outcomes — which is what a
probability is for. Earlier versions of this tool used it to *predict a price
ratio*, asking what premium a 10 "should" command over a 9 given how hard it is
to pull. That failed because the premium depends on demand, and scarcity
nobody wants is worth nothing.

Nothing here is predicted. Raw, PSA 9 and PSA 10 prices are all observed sold
prices; the gem rate is measured by PSA; grading and selling costs are known.
"""

# What you actually keep from a sale. eBay's final value fee plus payment
# processing runs to roughly 13% for most sellers; shipping a slab is a few
# dollars more. Ignoring these turns a losing trade into a winning one on
# paper, so they are applied to every sale price before anything is compared.
SELL_FEE_PCT = 0.13
SHIP_COST = 5.0

# What it costs to get one card graded, all in: PSA's cheapest tier plus
# postage both ways at bulk rates. Deliberately not optimistic.
DEFAULT_GRADING_COST = 25.0

# A card that misses the 10 is not automatically a 9 — it can come back 8 or
# worse, and those sell for far less. We only know the gem rate, not the full
# distribution, so the non-gem outcome is valued at the PSA 9 price with a
# haircut rather than pretending every miss is a 9.
NON_GEM_HAIRCUT = 0.70


def net_proceeds(price):
    """What actually lands in your pocket from a sale."""
    if not price:
        return 0.0
    return max(0.0, price * (1.0 - SELL_FEE_PCT) - SHIP_COST)


def grading_ev(m, grading_cost=DEFAULT_GRADING_COST):
    """Attach the raw-to-graded economics to a card.

    Returns True if it could be computed. Needs a raw price, a PSA 10 price
    and a PSA 9 price — all three observed.
    """
    raw = m.get("raw_median")
    p10 = m.get("p10_median")
    p9 = m.get("p9_median")
    gem = m.get("gem_rate_pct")

    if not (raw and p10 and p9) or gem is None:
        m["grade_ev"] = None
        m["grade_edge"] = None
        return False

    p = max(0.0, min(1.0, gem / 100.0))
    win = net_proceeds(p10)
    lose = net_proceeds(p9) * NON_GEM_HAIRCUT

    expected_sale = p * win + (1.0 - p) * lose
    cost = raw + grading_cost

    m["ev_win"] = round(win, 2)
    m["ev_lose"] = round(lose, 2)
    m["ev_cost"] = round(cost, 2)
    m["grade_ev"] = round(expected_sale, 2)
    m["grade_edge"] = round(expected_sale - cost, 2)
    # Return on the money actually put at risk, which is what decides whether a
    # thin dollar edge is worth the weeks of turnaround.
    m["grade_roi_pct"] = round(100.0 * (expected_sale - cost) / cost, 1) if cost else None
    # How often it has to gem just to break even. Compare against the real gem
    # rate: a card needing 40% when it gems 12% is a losing trade however big
    # the headline PSA 10 price looks.
    if win > lose:
        m["breakeven_gem_pct"] = round(100.0 * (cost - lose) / (win - lose), 1)
    else:
        m["breakeven_gem_pct"] = None
    return True


def rank_by_ev(candidates, min_edge_usd=20.0, min_roi_pct=15.0, min_sales=3):
    """Cards worth buying raw, best expected profit first."""
    from .value import confidence

    out = []
    for m in candidates:
        if not grading_ev(m):
            continue
        conf = m.get("confidence")
        if conf is None:
            conf = confidence(m, min_sales)
            m["confidence"] = conf
        if conf <= 0:
            continue
        if (m.get("raw_sales") or 0) < min_sales:
            continue
        if m["grade_edge"] < min_edge_usd:
            continue
        if (m.get("grade_roi_pct") or 0) < min_roi_pct:
            continue
        m["score"] = round(m["grade_edge"] * conf, 2)
        out.append(m)

    out.sort(key=lambda m: m["score"], reverse=True)
    return out
