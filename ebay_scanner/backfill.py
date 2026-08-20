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


def build_slices(cfg):
    """Cross-product of configured sets and grade tiers.

    Every slice pins Set, Professional Grader and Grade in the aspect filter,
    so all three are known from the query and need no enrichment call.
    """
    explicit = cfg.get("slices")
    if explicit:
        return explicit
    graders = cfg.get("slice_graders") or {}
    sets = cfg.get("slice_sets")
    if not sets:
        # Ranked by live listing volume rather than hand-picked. 3,360 distinct
        # sets exist; the top slice of them carries most of the tradeable volume.
        path = config.DATA_DIR / "set_volumes.json"
        try:
            ranked = json.loads(path.read_text())["ranked"]
        except (OSError, ValueError, KeyError):
            print("[backfill] no set_volumes.json; run mode probe-sets first")
            return []
        sets = [r["set"] for r in ranked[:int(cfg.get("slice_top_sets", 40))]
                if r["set"] and r["set"].lower() != "not specified"]
    out = []
    for grade in cfg.get("slice_grades") or []:
        value = graders.get(grade["grader"])
        if not value:
            print(f"[backfill] no aspect value for grader {grade['grader']}, skipping")
            continue
        for set_name in sets:
            out.append({
                "name": f"{set_name}|{grade['grader']}{grade['grade']}".lower()
                        .replace(" ", "-"),
                "set": set_name,
                "grader": grade["grader"],
                "grade": grade["grade"],
                "aspect_filter": (f"Professional Grader:{{{value}}},"
                                  f"Grade:{{{grade['grade']}}},Set:{{{set_name}}}"),
            })
    return out


def slice_params(cfg, offset, window=None, sl=None):
    params = query.search_params(cfg, offset)
    sl = sl or {}
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
    print(f"[backfill] slices={[s['name'] for s in (cfg.get('slices') or [])]}")
    print(f"[backfill] budget={budget} calls, {days}d lookback in {hours}h windows")

    seen = store.load_seen_ids()
    print(f"[backfill] {len(seen)} itemIds already stored")
    slices = build_slices(cfg)
    per_run = int(cfg.get("backfill_slices_per_run", 0) or 0)
    if per_run and len(slices) > per_run:
        state_path = config.DATA_DIR / "backfill_state.json"
        try:
            done = set(json.loads(state_path.read_text()).get("covered", []))
        except (OSError, ValueError):
            done = set()
        pending = [s for s in slices if s["name"] not in done]
        if not pending:                      # full pass complete, start again
            done, pending = set(), slices
        slices = pending[:per_run]
        done.update(s["name"] for s in slices)
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(
            {"covered": sorted(done), "total": len(build_slices(cfg))},
            indent=2) + "\n")
        print(f"[backfill] rotating: {len(slices)} slices this run, "
              f"{len(done)}/{len(build_slices(cfg))} of the grid covered")
    per_slice = max(1, budget // max(1, len(slices)))

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    started = datetime.now(timezone.utc).isoformat()

    from . import fields
    by_day, new_total, windows_done = {}, 0, 0

    # Newest windows first: recent standing inventory is the most relevant
    # comparison set, and older listings age out of the 45-day reference rule.
    for sl in slices:
      slice_budget = min(budget, client.call_count + per_slice)
      print(f"\n[backfill] === {sl['name']} ({sl['set']} {sl['grader']} {sl['grade']}) ===")
      for step in range(int(days * 24 / hours)):
        if client.call_count >= slice_budget:
            print("[backfill] budget exhausted")
            break
        end = now - timedelta(hours=hours * step)
        start = end - timedelta(hours=hours)
        window = f"itemStartDate:[{start.strftime(fmt)}..{end.strftime(fmt)}]"
        total, got = None, 0
        for page in range(int(cfg.get("backfill_max_pages", 25))):
            if client.call_count >= slice_budget:
                break
            body = client.search(slice_params(cfg, page * cfg["limit"], window, sl))
            if total is None:
                total = body.get("total")
            items = body.get("itemSummaries") or []
            for item in items:
                iid = item.get("itemId")
                if iid and iid not in seen:
                    seen.add(iid)
                    rec = fields.build_record(item, None, first_seen=started)
                    rec["source"] = "backfill"
                    # The query pinned these, so they are known without any
                    # per-item enrichment call.
                    rec["sliceName"] = sl["name"]
                    rec["set_name"] = sl["set"]
                    rec["grader"] = sl["grader"]
                    rec["grade"] = sl["grade"]
                    day = (rec.get("itemCreationDate") or started)[:10]
                    by_day.setdefault(day, []).append(rec)
                    got += 1
            if len(items) < cfg["limit"]:
                break
        windows_done += 1
        new_total += got
        print(f"[backfill] {start:%Y-%m-%d} +{hours}h  total={total}  new={got}"
              f"  (calls {client.call_count}/{slice_budget})")
        if total == 0:
            break

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
