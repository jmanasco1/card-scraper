"""Measure how many graded cards hide in the bucket both filters miss.

Per-item aspects need getItems (HTTP 403 for this app), so "matches neither
filter" cannot be sampled directly. Instead sample condition-ungraded listings
and correct for the known aspect-graded contamination inside that set:

    S1 = conditionIds:{4000}                      (contains the neither bucket
                                                   plus aspect-only listings)
    S2 = conditionIds:{4000} + aspect Graded:{Yes} (the contaminant alone)

    rate_neither = (rate_S1 - w * rate_S2) / (1 - w)

where w is the aspect-only share of condition-ungraded, taken from live totals.
"""
import re

from . import auth, config, query
from .client import EbayClient

GRADERS = "PSA|BGS|SGC|CGC|CSG|HGA|TAG|ISA|GMA|BCCG|BVG|KSA|PGI|BECKETT"
TOKEN_RE = re.compile(rf"\b({GRADERS})\b", re.I)
GRADE_RE = re.compile(
    rf"\b({GRADERS})\s*\.?\s*"
    r"(?:GEM\s*-?\s*MT|GEM\s*MINT|MINT|NM-?MT|NM|EX-?MT|EX|VG-?EX|VG|AUTH(?:ENTIC)?)?"
    r"\s*\.?\s*(10|9\.5|9|8\.5|8|7\.5|7|6\.5|6|5\.5|5|4\.5|4|3\.5|3|2\.5|2|1\.5|1)\b",
    re.I,
)
SAMPLE = 500


def collect(client, cfg, extra_filter, aspect, label):
    """Page through up to SAMPLE listings, returning (total, titles)."""
    titles, total = [], None
    for page in range(3):
        params = query.search_params(cfg, page * 200)
        if not aspect:
            params.pop("aspect_filter", None)
        params["filter"] = params["filter"] + "," + extra_filter
        params["limit"] = 200
        body = client.search(params)
        if total is None:
            total = body.get("total")
        items = body.get("itemSummaries") or []
        titles.extend(i.get("title") or "" for i in items)
        if len(items) < 200 or len(titles) >= SAMPLE:
            break
    titles = titles[:SAMPLE]
    print(f"[sample] {label:46} total={total:>10,}  sampled={len(titles)}")
    return total, titles


def rates(titles, label):
    tok = sum(1 for t in titles if TOKEN_RE.search(t))
    full = sum(1 for t in titles if GRADE_RE.search(t))
    n = len(titles) or 1
    print(f"[rate] {label:30} grader token {tok:4}/{n}={tok/n*100:5.1f}%   "
          f"grader+grade {full:4}/{n}={full/n*100:5.1f}%")
    return tok / n, full / n


def main():
    cfg = config.load()
    cid, secret = config.credentials()
    token, _ = auth.get_token(cid, secret)
    client = EbayClient(token, cfg["marketplace_id"])

    print("\n########## SAMPLES ##########")
    t1, s1 = collect(client, cfg, "conditionIds:{4000}", False, "S1 condition=Ungraded")
    t2, s2 = collect(client, cfg, "conditionIds:{4000}", True,
                     "S2 condition=Ungraded AND aspect Graded:{Yes}")

    w = (t2 / t1) if (t1 and t2) else 0.0
    print(f"\n[weight] aspect-graded share of condition-ungraded = {t2:,}/{t1:,} = {w*100:.2f}%")

    print("\n########## TITLE RATES ##########")
    tok1, full1 = rates(s1, "S1 (contaminated)")
    tok2, full2 = rates(s2, "S2 (contaminant)")

    print("\n########## CORRECTED — THE NEITHER BUCKET ##########")
    for name, r1, r2 in [("grader token", tok1, tok2), ("grader+grade", full1, full2)]:
        corrected = (r1 - w * r2) / (1 - w) if w < 1 else float("nan")
        print(f"[neither] {name:14} {corrected*100:6.2f}%   "
              f"(raw {r1*100:.2f}% minus {w*100:.2f}% contamination at {r2*100:.2f}%)")

    print("\n[sample] examples from S1 whose titles look graded:")
    shown = 0
    for t in s1:
        if GRADE_RE.search(t):
            print(f"[sample]   * {t[:92]}")
            shown += 1
            if shown >= 10:
                break
    if not shown:
        print("[sample]   (none)")

    print(f"\n[sample] calls used: {client.call_count}")


if __name__ == "__main__":
    main()
