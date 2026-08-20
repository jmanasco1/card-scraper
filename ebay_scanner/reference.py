"""Phase 3: reference value per bucket.

Rules, verbatim from the spec:
  - drop listings older than 45 days (a stale ask is evidence the market
    rejected that price, not evidence of value)
  - require 5+ remaining, else suppress the bucket entirely rather than guess
  - reference = median of the 5 lowest remaining asks
"""
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import config, matching

REFERENCES = config.DATA_DIR / "references.jsonl"
SNAPSHOTS = config.DATA_DIR / "reference_snapshots.jsonl"
MAX_AGE_DAYS = 45
MIN_COMPS = 5
REFERENCE_LOWEST_N = 5


def _parse(ts):
    try:
        return datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def load_corpus():
    rows = []
    for path in sorted(config.DATA_DIR.glob("*.jsonl")):
        if path.name in ("aspects.jsonl", "lifecycle.jsonl",
                         "references.jsonl", "reference_snapshots.jsonl"):
            continue
        with open(path) as fh:
            for line in fh:
                if line.strip():
                    rows.append(json.loads(line))
    aspects = {}
    apath = config.DATA_DIR / "aspects.jsonl"
    if apath.exists():
        with open(apath) as fh:
            for line in fh:
                if line.strip():
                    rec = json.loads(line)
                    if rec.get("status") == "ok":
                        aspects[rec["itemId"]] = rec
    gone = set()
    lpath = config.DATA_DIR / "lifecycle.jsonl"
    if lpath.exists():
        with open(lpath) as fh:
            for line in fh:
                if line.strip():
                    try:
                        gone.add(json.loads(line)["itemId"])
                    except (ValueError, KeyError):
                        pass
    return rows, aspects, gone


def build(rows, aspects, gone, now=None, exclude_item=None):
    """Return {bucket_key: reference_record} for buckets that qualify.

    exclude_item drops one itemId from the comp set. A listing must not help
    set the price it is being judged against — otherwise a cheap card pulls
    its own reference down and understates its discount.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)

    buckets = defaultdict(list)
    for r in rows:
        if r.get("itemId") in gone:
            continue          # sold or pulled; not a live ask
        if exclude_item and r.get("itemId") == exclude_item:
            continue          # never let a listing price itself
        key, method, _ = matching.bucket_key(r, aspects.get(r.get("itemId")))
        if key:
            buckets[key].append((r, method))

    references, stats = {}, {"buckets": len(buckets), "stale_dropped": 0,
                             "too_few": 0}
    for key, entries in buckets.items():
        fresh = []
        for r, method in entries:
            created = _parse(r.get("itemCreationDate"))
            if created and created < cutoff:
                stats["stale_dropped"] += 1
                continue
            if r.get("price") is not None:
                fresh.append((r, method))
        if len(fresh) < MIN_COMPS:
            stats["too_few"] += 1
            continue

        prices = sorted(r["price"] for r, _ in fresh)
        ages = [(now - _parse(r["itemCreationDate"])).days
                for r, _ in fresh if _parse(r.get("itemCreationDate"))]
        methods = {m for _, m in fresh}

        def pct(p):
            idx = min(len(prices) - 1, max(0, int(round(p * (len(prices) - 1)))))
            return prices[idx]

        references[key] = {
            "bucket": key,
            "reference": round(statistics.median(prices[:REFERENCE_LOWEST_N]), 2),
            "comp_count": len(prices),
            "p10": pct(0.10), "p25": pct(0.25),
            "median": round(statistics.median(prices), 2),
            "p75": pct(0.75), "p90": pct(0.90),
            "low": prices[0], "high": prices[-1],
            "oldest_days": max(ages) if ages else None,
            "newest_days": min(ages) if ages else None,
            "match_methods": sorted(methods),
            # No timestamp on the row itself: it would make every bucket differ
            # on every run even when nothing about the market changed. The daily
            # snapshot file carries the time dimension instead.
        }
    return references, stats, buckets


def main():
    rows, aspects, gone = load_corpus()
    now = datetime.now(timezone.utc)
    references, stats, buckets = build(rows, aspects, gone, now)

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(REFERENCES, "w") as fh:
        for rec in sorted(references.values(), key=lambda r: r["bucket"]):
            fh.write(json.dumps(rec, sort_keys=True) + "\n")

    # One row per bucket per day, so reference drift is visible later.
    day = now.strftime("%Y-%m-%d")
    existing = set()
    if SNAPSHOTS.exists():
        with open(SNAPSHOTS) as fh:
            for line in fh:
                if line.strip():
                    s = json.loads(line)
                    existing.add((s.get("day"), s.get("bucket")))
    with open(SNAPSHOTS, "a") as fh:
        for key, rec in references.items():
            if (day, key) in existing:
                continue
            fh.write(json.dumps({"day": day, "bucket": key,
                                 "reference": rec["reference"],
                                 "comp_count": rec["comp_count"],
                                 "median": rec["median"]}, sort_keys=True) + "\n")

    covered = sum(r["comp_count"] for r in references.values())
    total_listings = len(rows)
    lines = [
        f"listings considered: {total_listings:,}",
        f"buckets: {stats['buckets']:,}",
        f"buckets with a valid reference: {len(references):,} "
        f"({len(references)/max(1,stats['buckets'])*100:.1f}% of buckets)",
        f"listings covered by those buckets: {covered:,} "
        f"({covered/max(1,total_listings)*100:.1f}% of volume)",
        f"buckets suppressed for <{MIN_COMPS} comps: {stats['too_few']:,}",
        f"listings dropped as stale (>{MAX_AGE_DAYS}d): {stats['stale_dropped']:,}",
    ]
    print("\n".join("[reference] " + l for l in lines))
    (config.ROOT / "reference_summary.txt").write_text("\n".join(lines) + "\n")

    print("\n--- 10 sample buckets, full ask distribution ---")
    for rec in sorted(references.values(), key=lambda r: -r["comp_count"])[:10]:
        print(f"  {rec['bucket']}")
        print(f"    n={rec['comp_count']:3}  ref=${rec['reference']:<8.2f} "
              f"low=${rec['low']:.0f} p10=${rec['p10']:.0f} p25=${rec['p25']:.0f} "
              f"med=${rec['median']:.0f} p75=${rec['p75']:.0f} p90=${rec['p90']:.0f} "
              f"high=${rec['high']:.0f}")
        print(f"    age {rec['newest_days']}-{rec['oldest_days']}d  "
              f"via {','.join(rec['match_methods'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
