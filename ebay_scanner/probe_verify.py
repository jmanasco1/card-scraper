"""Show what the live verification query returns for already-flagged listings.

Wiring live verification straight into alerting would be trusting an untested
query. This prints the live market for each recent flag next to the corpus
numbers that produced it, so the targeting can be judged before it gates
anything.
"""

import json
import os

from . import auth, config, verify
from .client import EbayClient


def recent_flags(limit):
    try:
        rows = [json.loads(l) for l in open("data/flags.jsonl")]
    except FileNotFoundError:
        return []
    rows.sort(key=lambda r: r.get("saving") or 0, reverse=True)
    return rows[:limit]


def main():
    cfg = config.load()
    cid, secret = config.credentials()
    token, _ = auth.get_token(cid, secret)
    client = EbayClient(token, cfg["marketplace_id"])

    index = verify.slice_index(cfg)
    print(f"[probe-verify] {len(index)} slices indexed")

    limit = int(os.environ.get("VERIFY_PROBE_LIMIT", "12"))
    flags = recent_flags(limit)
    print(f"[probe-verify] checking {len(flags)} flags\n")

    kept = 0
    for f in flags:
        key = f["bucket"]
        sl = verify.slice_for_bucket(index, key)
        print("=" * 92)
        print(f"{f['title'][:88]}")
        print(f"  corpus: ${f['price']:.2f} vs ref ${f['reference']:.2f} "
              f"(n={f['comp_count']})   bucket={key}")
        if not sl:
            print("  live:   NO SLICE MATCH - cannot verify")
            continue
        print(f"  slice:  {sl['name']}")
        try:
            v = verify.check(client, cfg, sl, key, f["itemId"], f["price"])
        except Exception as exc:                      # noqa: BLE001
            print(f"  live:   query failed: {exc}")
            continue
        ok, why = verify.passes(v)
        kept += ok
        low = f"${v['live_low']:.2f}" if v["live_low"] is not None else "-"
        print(f"  live:   total={v['live_total']} comps={v['live_comps']} "
              f"low={low} still_listed={v['still_listed']}")
        print(f"  prices: {v['live_prices']}")
        print(f"  verdict: {'KEEP' if ok else 'DROP'} - {why}")
    print("=" * 92)
    print(f"[probe-verify] {kept}/{len(flags)} would survive live verification")


if __name__ == "__main__":
    main()
