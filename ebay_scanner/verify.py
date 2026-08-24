"""Verify a flagged listing against live eBay before alerting on it.

The stored corpus is a 45-day accumulation with no liveness guarantee: the
re-check pass only sweeps a 7-day window of newly-collected listings, so the
backfilled standing inventory that supplies nearly every comp is never
revisited. A bucket can therefore show 27 comps when 26 of them have sold,
which produces three failure modes reported from the field: alerts on cards
with no live competition, alerts against references built from dead listings,
and alerts on listings that are not the cheapest because a cheaper one was
never collected.

Corpus buckets stay the shortlist - they cost no API calls. This module is the
gate: it re-asks eBay what is on sale right now, and the alert survives only if
the live market agrees.

Two measurement rounds shaped what this trusts:

Card number cannot be pinned as an aspect. Sending Card Number:{US1} did not
error - eBay silently dropped it and returned the whole slice, taking one
query from 70 results to 2,539 and another to the full 12,232. Filters this
API ignores rather than rejects have burned us before, so the card number is
sent as a keyword and then enforced again on the returned titles here, where
no server-side behaviour can quietly disable it.

Liveness cannot be inferred from a result page. Absence from the first 100
results by price says nothing when the query matched thousands, and that read
wrongly pronounced live listings dead. getItem answers it definitively: 404
means gone.
"""

import re

from . import backfill, config, matching, query

# A live query returning fewer than this cannot establish a market price. Far
# below the corpus MIN_COMPS deliberately: the corpus accumulates 45 days of
# arrivals, this counts only what is on sale at this instant.
MIN_LIVE_COMPS = 4

# The candidate must be the cheapest live listing to be worth an alert. The
# tolerance absorbs an equal-priced twin sorting ahead of it.
LOWEST_TOLERANCE = 0.01

SAFE_CARD_NO = re.compile(r"^[A-Za-z0-9-]{1,8}$")


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


def card_number(bucket_key):
    """Bucket keys are year|set|card_number|parallel|grader|grade."""
    parts = bucket_key.split("|")
    if len(parts) < 3:
        return None
    value = parts[2].strip()
    return value if value and SAFE_CARD_NO.match(value) else None


def title_matcher(card_no):
    """Regex deciding whether a title really is this card number.

    A bare short number matches far too much - '6' appears in most titles - so
    a purely numeric card number must carry the '#'. Alphanumerics like US1 or
    83T are distinctive enough to stand alone. Rejecting a legitimate comp only
    shrinks the comp count and suppresses an alert, so the strict direction is
    the safe one.
    """
    escaped = re.escape(card_no)
    if card_no.isdigit():
        return re.compile(rf"#\s*{escaped}\b", re.I)
    return re.compile(rf"#?\s*\b{escaped}\b", re.I)


def _price(item):
    try:
        return float(item["price"]["value"])
    except (KeyError, TypeError, ValueError):
        return None


def live_params(cfg, sl, card_no):
    """Search parameters for the live listings of one bucket's card."""
    params = query.search_params(cfg, 0)
    base = params.get("aspect_filter") or f"categoryId:{cfg['category_ids'][0]}"
    params["aspect_filter"] = base + "," + sl["aspect_filter"]
    if card_no:
        params["q"] = card_no
    params["sort"] = "price"
    params["limit"] = 100
    params.pop("offset", None)
    return params


def is_live(client, item_id):
    """True if the listing is still purchasable, False if eBay 404s it."""
    resp = client.get(f"{config.API_HOST}/buy/browse/v1/item/{item_id}",
                      allow_status=(404, 400))
    return resp.status_code == 200


def check(client, cfg, sl, bucket_key, item_id, price, player=None):
    """Ask eBay what this card is selling for right now.

    Returns a verdict dict, or None when the query could not be built. The
    caller decides; this only reports what the live market shows.
    """
    if not sl:
        return None
    card_no = card_number(bucket_key)
    if not card_no:
        return None

    params = live_params(cfg, sl, card_no)
    # Set plus card number still collides across products sharing a Set value:
    # Victor Wembanyama #136 and a Rose Namajunas UFC #136 landed in one bucket.
    # A player name separates them where one is available.
    if player:
        params["q"] = f"{card_no} {player}"
    body = client.search(params)
    items = body.get("itemSummaries") or []

    wants = title_matcher(card_no)
    comps = []
    for it in items:
        if it.get("itemId") == item_id:
            continue
        if not wants.search(it.get("title") or ""):
            continue
        p = _price(it)
        if p is not None:
            comps.append(p)
    comps.sort()

    verdict = {
        "live_total": body.get("total"),
        "live_returned": len(items),
        "live_comps": len(comps),
        "live_low": comps[0] if comps else None,
        "live_prices": comps[:10],
        "still_listed": is_live(client, item_id),
    }
    if len(comps) >= MIN_LIVE_COMPS:
        verdict["live_reference"] = round(sum(comps[:5]) / len(comps[:5]), 2)
    verdict["is_lowest"] = bool(comps and price <= comps[0] + LOWEST_TOLERANCE)
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
