"""Phase 5: listing-episode scaffolding. Capture only, nothing surfaced.

A card that is listed, delisted and relisted is one episode chain. Chains are
keyed by certification number where it exists, because that is the only
identifier that is genuinely unique to a physical slab.

Where the cert is absent the fallback is deliberately narrow: seller AND
bucket AND image-hash must all agree, and consecutive prices must sit within
15%. Seller + bucket alone is NOT enough — dealers hold several copies of the
same card in the same grade, and merging those would manufacture relist chains
that never happened.
"""
import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone

from . import config, matching, reference

EPISODES = config.DATA_DIR / "episodes.jsonl"
PRICE_TOLERANCE = 0.15


def image_hash(url):
    """eBay image URLs embed a content id; the size suffix varies, so strip it."""
    if not url:
        return None
    match = re.search(r"/images/g/([^/]+)/", url)
    token = match.group(1) if match else url
    return hashlib.sha1(token.encode()).hexdigest()[:12]


def cert_from(listing, aspect):
    for source in (aspect or {}, listing):
        value = (source.get("cert_number")
                 or (source.get("aspects") or {}).get("Certification Number"))
        if value:
            digits = re.sub(r"\D", "", str(value))
            if len(digits) >= 6:
                return digits
    # Some sellers put the cert in the title as a bare 8-9 digit run.
    match = re.search(r"\b(\d{8,9})\b", listing.get("title") or "")
    return match.group(1) if match else None


def main():
    rows, aspects, gone = reference.load_corpus()
    now = datetime.now(timezone.utc)

    chains = defaultdict(list)
    cert_hits = 0
    for r in rows:
        key, _, _ = matching.bucket_key(r, aspects.get(r.get("itemId")))
        cert = cert_from(r, aspects.get(r.get("itemId")))
        if cert:
            cert_hits += 1
            chains[("cert", cert)].append((r, "cert"))
        elif key:
            ih = image_hash(r.get("imageUrl"))
            if ih and r.get("sellerUsername"):
                chains[("inferred", r["sellerUsername"], key, ih)].append((r, "inferred"))

    records = []
    for chain_key, entries in chains.items():
        entries.sort(key=lambda e: e[0].get("itemCreationDate") or "")
        if chain_key[0] == "inferred" and len(entries) > 1:
            # Enforce price continuity: a jump beyond tolerance means these are
            # different physical cards, not the same one relisted.
            kept = [entries[0]]
            for row, conf in entries[1:]:
                prev = kept[-1][0].get("price")
                cur = row.get("price")
                if prev and cur and abs(cur - prev) / prev <= PRICE_TOLERANCE:
                    kept.append((row, conf))
            entries = kept
        if len(entries) < 1:
            continue

        rows_only = [e[0] for e in entries]
        dates = [d for d in (r.get("itemCreationDate") for r in rows_only) if d]
        ended = [r for r in rows_only if r.get("itemId") in gone]
        best_offer = any("BEST_OFFER" in (r.get("buyingOptions") or [])
                         for r in rows_only)
        days = None
        if dates:
            first = reference._parse(min(dates))
            if first:
                days = round((now - first).total_seconds() / 86400, 1)
        records.append({
            "chainId": "|".join(str(p) for p in chain_key),
            "confidence": chain_key[0],
            "episodes": len(rows_only),
            "itemIds": [r["itemId"] for r in rows_only],
            "firstSeen": min(dates) if dates else None,
            "cumulativeDaysOnMarket": days,
            "prices": [r.get("price") for r in rows_only],
            "endedEpisodes": len(ended),
            "bestOffer": best_offer,
            "bucket": matching.bucket_key(rows_only[0],
                                          aspects.get(rows_only[0].get("itemId")))[0],
        })

    # Only chains with a real history are persisted. Single-episode chains are
    # recomputable from the listing corpus on any run, and writing ~19k of them
    # rewrote 39k lines of JSONL every 15 minutes — churn the date-partitioned
    # design exists to avoid. Sorted by chainId so diffs stay minimal and
    # reviewable rather than reshuffling on every run.
    persisted = [r for r in records if r["episodes"] >= 2]
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(EPISODES, "w") as fh:
        for rec in sorted(persisted, key=lambda r: r["chainId"]):
            fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")

    multi = [r for r in records if r["episodes"] >= 2]
    by_conf = defaultdict(int)
    for r in records:
        by_conf[r["confidence"]] += 1
    lines = [
        f"listings scanned: {len(rows):,}",
        f"cert numbers found: {cert_hits:,} ({cert_hits/max(1,len(rows))*100:.1f}%)",
        f"chains: {len(records):,}  (cert {by_conf['cert']:,}, "
        f"inferred {by_conf['inferred']:,})",
        f"chains with 2+ episodes: {len(multi):,} (only these are stored)",
    ]
    print("\n".join("[episodes] " + l for l in lines))
    for rec in sorted(multi, key=lambda r: -r["episodes"])[:8]:
        print(f"[episodes]   {rec['episodes']}x {rec['confidence']:8} "
              f"{rec['prices']} {(rec['bucket'] or '')[:48]}")
    (config.ROOT / "episodes_summary.txt").write_text("\n".join(lines) + "\n")

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"chains={len(records)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
