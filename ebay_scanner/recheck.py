"""Re-check stored listings and record which have disappeared.

Disappearance is the only available proxy for "sold", and it needs a pass that
revisits listings — the collector only ever records new ones.

Per-item getItem works (200) but costs one call per listing, which does not fit
the 5,000/day budget at ~29k new listings a day. The itemStartDate window search
returns 200 live listings per call instead, so this walks creation-time windows,
treats everything stored-but-absent as gone, and writes an append-only lifecycle
log. Cost is ~6 calls per hour-window rather than one per listing.
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import auth, config, query, store
from .client import EbayClient

LIFECYCLE = config.DATA_DIR / "lifecycle.jsonl"


def _parse(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def load_gone():
    """itemIds already recorded as disappeared, so they are not re-reported."""
    gone = {}
    if not LIFECYCLE.exists():
        return gone
    with open(LIFECYCLE) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("itemId"):
                    gone[rec["itemId"]] = rec
    return gone


def window_key(created, hours):
    dt = _parse(created)
    if not dt:
        return None
    bucket = dt.replace(minute=0, second=0, microsecond=0)
    bucket -= timedelta(hours=bucket.hour % hours)
    return bucket


def live_ids_in_window(client, cfg, start, end, max_pages):
    """Every currently-live itemId created inside [start, end)."""
    live = set()
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    window = f"itemStartDate:[{start.strftime(fmt)}..{end.strftime(fmt)}]"
    total = None
    for page in range(max_pages):
        params = query.search_params(cfg, page * cfg["limit"])
        params["filter"] = params["filter"] + "," + window
        params["limit"] = cfg["limit"]
        body = client.search(params)
        if total is None:
            total = body.get("total")
        items = body.get("itemSummaries") or []
        live.update(i.get("itemId") for i in items if i.get("itemId"))
        if len(items) < cfg["limit"]:
            break
    return live, total, window


def main():
    cfg = config.load()
    cid, secret = config.credentials()
    token, _ = auth.get_token(cid, secret)
    client = EbayClient(token, cfg["marketplace_id"])

    hours = int(cfg.get("recheck_window_hours", 2))
    lookback = int(cfg.get("recheck_lookback_days", 7))
    max_windows = int(cfg.get("recheck_max_windows", 12))
    max_pages = int(cfg.get("recheck_max_pages", 12))

    records = []
    for path in sorted(config.DATA_DIR.glob("*.jsonl")):
        if path.name == "lifecycle.jsonl":
            continue
        with open(path) as fh:
            for line in fh:
                if line.strip():
                    records.append(json.loads(line))

    gone = load_gone()
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback)

    buckets = defaultdict(list)
    for r in records:
        if r.get("itemId") in gone:
            continue
        key = window_key(r.get("itemCreationDate"), hours)
        if key and key >= cutoff:
            buckets[key].append(r)

    print(f"[recheck] {len(records)} stored, {len(gone)} already gone, "
          f"{sum(len(v) for v in buckets.values())} live candidates "
          f"across {len(buckets)} window(s) of {hours}h")

    # Oldest windows first: those listings have had the most time to sell.
    order = sorted(buckets)[:max_windows]
    now = datetime.now(timezone.utc).isoformat()
    newly_gone = []

    for start in order:
        end = start + timedelta(hours=hours)
        candidates = buckets[start]
        live, total, window = live_ids_in_window(client, cfg, start, end, max_pages)
        missing = [r for r in candidates if r["itemId"] not in live]
        print(f"[recheck] {start:%m-%d %H:%M} +{hours}h  candidates={len(candidates):4} "
              f"live_seen={len(live):4} total={total}  gone={len(missing)}")

        if total and len(live) < total:
            print(f"[recheck]   ::warning:: only paged {len(live)} of {total} live "
                  f"listings in this window; raise recheck_max_pages")
            continue  # incomplete sweep would falsely mark listings gone

        for r in missing:
            created = _parse(r.get("itemCreationDate"))
            hours_listed = ((datetime.now(timezone.utc) - created).total_seconds() / 3600
                            if created else None)
            newly_gone.append({
                "itemId": r["itemId"],
                "itemCreationDate": r.get("itemCreationDate"),
                "firstSeenAt": r.get("firstSeenAt"),
                "disappearedBy": now,
                "hoursListed": round(hours_listed, 1) if hours_listed else None,
                "price": r.get("price"),
                "title": r.get("title"),
                "sellerUsername": r.get("sellerUsername"),
                "conditionId": r.get("conditionId"),
            })

    if newly_gone:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LIFECYCLE, "a") as fh:
            for rec in newly_gone:
                fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
        print(f"[recheck] recorded {len(newly_gone)} newly disappeared listings")
    else:
        print("[recheck] no newly disappeared listings")

    summary = config.ROOT / "recheck_summary.txt"
    lines = [
        f"windows checked: {len(order)}",
        f"newly gone: {len(newly_gone)}",
        f"total gone all time: {len(gone) + len(newly_gone)}",
        f"calls used: {client.call_count}",
    ]
    if newly_gone:
        hrs = sorted(r["hoursListed"] for r in newly_gone if r["hoursListed"])
        if hrs:
            lines.append(f"median hours listed before disappearing: "
                         f"{hrs[len(hrs)//2]:.1f}")
    print("\n".join("[recheck] " + l for l in lines))
    summary.write_text("\n".join(lines) + "\n")

    import os
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"newly_gone={len(newly_gone)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
