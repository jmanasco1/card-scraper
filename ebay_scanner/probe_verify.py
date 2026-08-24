"""Show the live market beside our claimed reference for real candidates.

Not a status check. This exists to answer one question with evidence: when the
scanner says a card is underpriced, is the comparison real? It rebuilds the
candidate list exactly as the scanner does, then for each one prints what eBay
actually has on sale right now - the whole cheap end of the distribution, not a
summary - so the numbers can be judged rather than trusted.
"""

import os
from datetime import datetime, timezone

from . import auth, config, matching, reference, scanner, verify
from .client import EbayClient


def candidates():
    """Rebuild the scanner's candidate list, top-ranked first."""
    rows, aspects, gone = reference.load_corpus()
    now = datetime.now(timezone.utc)
    refs, _, buckets = reference.build(rows, aspects, gone, now)
    seen = scanner.already_flagged()
    out = []
    for r in rows:
        if r.get("itemId") in seen or r.get("itemId") in gone:
            continue
        price = r.get("price")
        if price is None or not (scanner.ACT_MIN <= price <= scanner.ACT_MAX):
            continue
        key, method, _ = matching.bucket_key(r, aspects.get(r.get("itemId")))
        if method not in scanner.TRUSTED_METHODS:
            continue
        ref = refs.get(key) if key else None
        if not ref or ref["comp_count"] < reference.MIN_COMPS:
            continue
        if price > scanner.DISCOUNT * ref["reference"]:
            continue
        peer = reference.price_bucket([e[0] for e in buckets.get(key, [])],
                                      now, exclude_item=r["itemId"])
        if not peer or price > scanner.DISCOUNT * peer["reference"]:
            continue
        parsed = matching.parse_title(r.get("title"))
        grader, grade = key.split("|")[4], key.split("|")[5]
        if not parsed["grade"] or str(parsed["grade"]) != str(grade):
            continue
        if parsed["grader"] and parsed["grader"] != grader:
            continue
        out.append({"itemId": r["itemId"], "title": r.get("title"),
                    "price": price, "bucket": key,
                    "reference": peer["reference"],
                    "comp_count": peer["comp_count"],
                    "saving": round(peer["reference"] - price, 2)})
    out.sort(key=lambda c: -c["saving"])
    return out


def main():
    cfg = config.load()
    cid, secret = config.credentials()
    token, _ = auth.get_token(cid, secret)
    client = EbayClient(token, cfg["marketplace_id"])
    index = verify.slice_index(cfg)

    cands = candidates()
    limit = int(os.environ.get("VERIFY_PROBE_LIMIT", "15"))
    print(f"[probe-verify] {len(cands)} candidates; checking the top {limit}\n")

    kept = 0
    for c in cands[:limit]:
        sl = verify.slice_for_bucket(index, c["bucket"])
        print("=" * 94)
        print(f"{(c['title'] or '')[:92]}")
        print(f"  we claim: ${c['price']:.2f} vs reference ${c['reference']:.2f} "
              f"(corpus n={c['comp_count']}) -> ${c['saving']:.2f} saving")
        if not sl:
            print("  live:     no slice match")
            continue
        try:
            v = verify.check(client, cfg, sl, c["bucket"],
                             c["itemId"], c["price"])
        except Exception as exc:                          # noqa: BLE001
            print(f"  live:     query failed: {exc}")
            continue
        ok, why = verify.passes(v)
        kept += ok
        prices = v["live_prices"]
        print(f"  live:     {v['live_comps']} comps of {v['live_returned']} "
              f"returned (eBay total {v['live_total']}), still_listed="
              f"{v['still_listed']}")
        print(f"  cheapest listed right now: "
              + (", ".join(f"${p:,.2f}" for p in prices) if prices else "none"))
        print(f"  verdict:  {'KEEP' if ok else 'DROP'} - {why}")
    print("=" * 94)
    print(f"[probe-verify] {kept} of the top {min(limit, len(cands))} "
          f"would alert")


if __name__ == "__main__":
    main()
