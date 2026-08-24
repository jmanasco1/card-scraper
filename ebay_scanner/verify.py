"""Verify a flagged listing against live eBay before alerting on it.

The stored corpus is a 45-day accumulation with no liveness guarantee: the
re-check pass only sweeps a 7-day window of newly-collected listings, so the
backfilled standing inventory that supplies nearly every comp is never
revisited. A bucket can therefore show 27 comps when 26 of them have sold,
which produces three failure modes reported from the field: alerts on cards
with no live competition at all, alerts against references built from dead
listings, and alerts on listings that are not actually the cheapest because a
cheaper one was never collected.

Corpus buckets stay the shortlist — they cost no API calls. This module is the
gate: it re-asks eBay what is on sale right now for that exact card, and the
alert only survives if the live market agrees.
"""

import re

from . import backfill, matching, query

# A live query returning fewer than this cannot establish a market price. This
# is deliberately far below the corpus MIN_COMPS: the corpus accumulates 45
# days of arrivals, while this counts only what is on sale at this instant.
MIN_LIVE_COMPS = 4

# The candidate must be the cheapest live listing to be worth an alert. A small
# tolerance absorbs the case where an equal-priced twin sorts ahead of it.
LOWEST_TOLERANCE = 0.01

CARD_NO_IN_KEY = re.compile(r"^[a-z0-9-]+$")


def slice_index(cfg):
    """Index slices by the bucket-key fields they produce.

    backfill stamps set_name from the slice definition itself, so a slice's
    (year, set core, grader, grade) reproduces exactly the first two and last
    two fields of every bucket key built from it. Matching on those is exact;
    matching set names by substring is not, and mapped a 1992 Topps card onto
    the 2024 Topps Chrome slice when it was tried.
    """
    index = {}
    for sl in backfill.build_slices(cfg):
        year, set_core = matching.split_year(sl["set"])
        key = (year or "____", set_core,
               matching.normalize_grader(sl["grader"]),
               matching.normalize_grade(sl["grade"]))
        index.setdefault(key, sl)
    return index


def slice_for_bucket(index, bucket_key):
    """Recover the slice a bucket was collected under, or None."""
    parts = bucket_key.split("|")
    if len(parts) < 6:
        return None
    return index.get((parts[0], parts[1], parts[4], parts[5]))


def _card_number(bucket_key):
    """Bucket keys are year|set|card_number|parallel|grader|grade."""
    parts = bucket_key.split("|")
    return parts[2] if len(parts) >= 3 else ""


def live_params(cfg, sl, bucket_key):
    """Search parameters for the live listings of one bucket's card.

    Pins the slice's own aspect filter, so Set, Professional Grader and Grade
    are constrained by eBay rather than by our title parse, and pins the card
    number as an aspect. Sorted by price so the cheapest arrives first.
    """
    params = query.search_params(cfg, 0)
    base = params.get("aspect_filter") or f"categoryId:{cfg['category_ids'][0]}"
    parts = [base, sl["aspect_filter"]]
    # Card Number must be pinned as an aspect, not passed as a keyword. As a
    # keyword "6" matched any title containing a 6, so the LeBron #6 check came
    # back with 99 unrelated cards priced $11-$20 and called a $15 listing
    # overpriced.
    card_no = _card_number(bucket_key)
    if card_no and CARD_NO_IN_KEY.match(card_no):
        parts.append("Card Number:{%s}" % card_no.upper())
    params["aspect_filter"] = ",".join(parts)
    params["sort"] = "price"
    params["limit"] = 100
    params.pop("offset", None)
    return params


def _price(item):
    try:
        return float(item["price"]["value"])
    except (KeyError, TypeError, ValueError):
        return None


def check(client, cfg, sl, bucket_key, item_id, price, player=None):
    """Ask eBay what this card is selling for right now.

    Returns a verdict dict, or None when the query could not be built. The
    caller decides; this only reports what the live market shows.
    """
    if not sl:
        return None
    params = live_params(cfg, sl, bucket_key)
    # Set plus card number still collides across products sharing a Set value:
    # Victor Wembanyama #136 and a Rose Namajunas UFC #136 landed in one bucket.
    # The player name separates them where the title yields one.
    if player:
        params["q"] = player
    body = client.search(params)
    items = body.get("itemSummaries") or []

    comps, present = [], False
    for it in items:
        p = _price(it)
        if p is None:
            continue
        if it.get("itemId") == item_id:
            present = True
            continue
        comps.append(p)
    comps.sort()

    verdict = {
        "live_total": body.get("total"),
        "live_comps": len(comps),
        "live_low": comps[0] if comps else None,
        "live_prices": comps[:10],
        "still_listed": present,
    }
    if len(comps) >= MIN_LIVE_COMPS:
        verdict["live_reference"] = round(
            sum(comps[:5]) / len(comps[:5]), 2)
    verdict["is_lowest"] = bool(
        comps and price <= comps[0] + LOWEST_TOLERANCE)
    return verdict


def passes(verdict):
    """An alert survives only if the live market backs every claim we make."""
    if not verdict:
        return False, "no live query"
    if not verdict["still_listed"]:
        return False, "listing no longer live"
    if verdict["live_comps"] < MIN_LIVE_COMPS:
        return False, f"only {verdict['live_comps']} live comps"
    if not verdict["is_lowest"]:
        return False, f"not lowest live BIN (${verdict['live_low']:.2f} exists)"
    return True, "verified"
