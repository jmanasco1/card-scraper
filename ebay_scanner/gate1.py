"""Gate 1 report: does bucketing actually group anything."""
import json
import random
from collections import Counter, defaultdict

from . import config, matching


def load():
    rows = []
    for path in sorted(config.DATA_DIR.glob("*.jsonl")):
        if path.name in ("aspects.jsonl", "lifecycle.jsonl", "recheck_state.json"):
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
    return rows, aspects


def main():
    rows, aspects = load()
    buckets = defaultdict(list)
    methods, reasons, unbucketed = Counter(), Counter(), []

    for r in rows:
        key, method, reason = matching.bucket_key(r, aspects.get(r["itemId"]))
        if key:
            buckets[key].append(r)
            methods[method] += 1
        else:
            reasons[f"{method}:{reason}"] += 1
            unbucketed.append((r, method, reason))

    keyed = sum(methods.values())
    n = len(rows)
    sizes = sorted((len(v) for v in buckets.values()), reverse=True)
    big = [s for s in sizes if s >= 5]

    print(f"listings                {n:,}")
    print(f"complete bucket key     {keyed:,} = {keyed/n*100:.1f}%")
    print(f"  via aspects           {methods['aspects']:,} = {methods['aspects']/n*100:.1f}%")
    print(f"  via title parsing     {methods['title']:,} = {methods['title']/n*100:.1f}%")
    print(f"distinct buckets        {len(buckets):,}")
    print(f"buckets with 5+         {len(big):,} = "
          f"{len(big)/len(buckets)*100:.1f}% of buckets")
    if keyed:
        print(f"listings in a 5+ bucket {sum(big):,} = {sum(big)/n*100:.1f}% of all listings")
    print(f"median bucket size      {sizes[len(sizes)//2] if sizes else 0}")

    print("\n--- 20 largest buckets ---")
    for key, items in sorted(buckets.items(), key=lambda kv: -len(kv[1]))[:20]:
        prices = sorted(i["price"] for i in items if i.get("price"))
        span = f"${prices[0]:.0f}-${prices[-1]:.0f}" if prices else "—"
        print(f"  {len(items):4}x  {span:>14}  {key[:70]}")
        print(f"        e.g. {(items[0].get('title') or '')[:82]}")

    print("\n--- why listings failed to bucket ---")
    for reason, count in reasons.most_common(10):
        print(f"  {count:6} ({count/n*100:5.1f}%)  {reason}")

    print("\n--- 20 random unbucketable listings ---")
    random.seed(7)
    for r, method, reason in random.sample(unbucketed, min(20, len(unbucketed))):
        print(f"  [{method}] {reason}")
        print(f"      {(r.get('title') or '')[:86]}")


if __name__ == "__main__":
    main()
