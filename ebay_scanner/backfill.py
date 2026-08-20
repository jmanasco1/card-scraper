"""Sweep the standing inventory of a target slice into the corpus.

Collection only ever sees newly-listed items — roughly 29k/day spread across
the whole catalog, so copies of the same card almost never co-occur and comp
density stays at 1. The listings that *are* already sitting side by side are
the ~1.4M live ones, reachable by walking itemStartDate windows backwards.

Narrowing to a slice (an aspect-filtered grader/grade, optionally a set term)
is what makes both the sweep and the enrichment that follows converge.
"""
import json
from datetime import datetime, timedelta, timezone

from . import auth, config, query, store
from .client import EbayClient, browse_remaining


def slice_params(cfg, offset, window=None):
    params = query.search_params(cfg, offset)
    sl = cfg.get("slice") or {}
    base = params.get("aspect_filter") or f"categoryId:{cfg['category_ids'][0]}"
    extra = sl.get("aspect_filter")
    if extra:
        params["aspect_filter"] = base + "," + extra
    if sl.get("q"):
        params["q"] = sl["q"]
    if window:
        params["filter"] = params["filter"] + "," + window
    params["limit"] = cfg["limit"]
    return params


def main():
    cfg = config.load()
    cid, secret = config.credentials()
    token, _ = auth.get_token(cid, secret)
    client = EbayClient(token, cfg["marketplace_id"])

    budget = int(cfg.get("backfill_max_calls", 400))
    days = int(cfg.get("backfill_lookback_days", 60))
    hours = int(cfg.get("backfill_window_hours", 24))
    min_quota = int(cfg.get("backfill_min_quota", 600))

    remaining, limit, _ = browse_remaining(client.rate_limits())
    if remaining is not None:
        print(f"[backfill] quota {remaining}/{limit}")
        budget = max(0, min(budget, remaining - min_quota))
    print(f"[backfill] slice={json.dumps(cfg.get('slice') or {})}")
    print(f"[backfill] budget={budget} calls, {days}d lookback in {hours}h windows")

    seen = store.load_seen_ids()
    print(f"[backfill] {len(seen)} itemIds already stored")

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    started = datetime.now(timezone.utc).isoformat()

    from . import fields
    by_day, new_total, windows_done = {}, 0, 0

    # Newest windows first: recent standing inventory is the most relevant
    # comparison set, and older listings age out of the 45-day reference rule.
    for step in range(int(days * 24 / hours)):
        if client.call_count >= budget:
            print("[backfill] budget exhausted")
            break
        end = now - timedelta(hours=hours * step)
        start = end - timedelta(hours=hours)
        window = f"itemStartDate:[{start.strftime(fmt)}..{end.strftime(fmt)}]"
        total, got = None, 0
        for page in range(int(cfg.get("backfill_max_pages", 25))):
            if client.call_count >= budget:
                break
            body = client.search(slice_params(cfg, page * cfg["limit"], window))
            if total is None:
                total = body.get("total")
            items = body.get("itemSummaries") or []
            for item in items:
                iid = item.get("itemId")
                if iid and iid not in seen:
                    seen.add(iid)
                    rec = fields.build_record(item, None, first_seen=started)
                    rec["source"] = "backfill"
                    day = (rec.get("itemCreationDate") or started)[:10]
                    by_day.setdefault(day, []).append(rec)
                    got += 1
            if len(items) < cfg["limit"]:
                break
        windows_done += 1
        new_total += got
        print(f"[backfill] {start:%Y-%m-%d %H:%M} +{hours}h  total={total}  new={got}"
              f"  (calls {client.call_count}/{budget})")

    # Partition by the listing's own creation date, not today's, so the
    # date-partitioned layout keeps meaning.
    for day, recs in sorted(by_day.items()):
        recs.sort(key=lambda r: (r.get("itemCreationDate") or "", r.get("itemId")))
        path = store.append(recs, today=day)
        print(f"[backfill] wrote {len(recs):5} records to {path.name}")

    lines = [f"windows swept: {windows_done}",
             f"new listings: {new_total}",
             f"calls used: {client.call_count}",
             f"total stored: {store.total_stored()}"]
    print("\n".join("[backfill] " + l for l in lines))
    (config.ROOT / "backfill_summary.txt").write_text("\n".join(lines) + "\n")

    import os
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"new_count={new_total}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
