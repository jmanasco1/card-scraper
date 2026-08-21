"""Fetch item aspects for a sampled subset of stored listings.

The bulk getItems endpoint is 403 for this application, but single-item
getItem returns 200 with the full localizedAspects block — Professional
Grader, Grade, Certification Number, Set, Player/Athlete, Card Number,
Season. That is one call per listing, so at ~29k new listings a day against
a 5,000/day ceiling it can only ever cover a sample.

Results go to data/aspects.jsonl keyed by itemId, leaving the listing
partitions immutable and append-only. A 404 here is a definitive
disappearance, so it is also recorded to the lifecycle log.
"""
import json
import os
import random
from datetime import datetime, timezone

from . import auth, config, fields
from .client import EbayClient, browse_remaining
from .recheck import LIFECYCLE

ASPECTS = config.DATA_DIR / "aspects.jsonl"


def load_enriched():
    """itemIds already attempted, so runs do not repeat work."""
    seen = set()
    if not ASPECTS.exists():
        return seen
    with open(ASPECTS) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("itemId"):
                seen.add(rec["itemId"])
    return seen


def load_gone():
    gone = set()
    if not LIFECYCLE.exists():
        return gone
    with open(LIFECYCLE) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    gone.add(json.loads(line)["itemId"])
                except (ValueError, KeyError):
                    pass
    return gone


def load_listings():
    rows = []
    for path in sorted(config.DATA_DIR.glob("*.jsonl")):
        if path.name in ("lifecycle.jsonl", "aspects.jsonl"):
            continue
        with open(path) as fh:
            for line in fh:
                if line.strip():
                    rows.append(json.loads(line))
    return rows


def pick(rows, strategy, budget):
    """Choose which listings to spend the call budget on."""
    if strategy == "random":
        random.shuffle(rows)
        return rows[:budget]
    # Newest first: those are the listings still live and worth acting on.
    rows.sort(key=lambda r: r.get("itemCreationDate") or "", reverse=True)
    return rows[:budget]


def main():
    cfg = config.load()
    cid, secret = config.credentials()
    token, _ = auth.get_token(cid, secret)
    client = EbayClient(token, cfg["marketplace_id"])

    max_calls = int(cfg.get("enrich_max_calls_per_run", 100))
    min_quota = int(cfg.get("enrich_min_quota", 800))
    strategy = cfg.get("enrich_strategy", "newest")

    remaining, limit, _ = browse_remaining(client.rate_limits())
    if remaining is not None:
        print(f"[enrich] quota {remaining}/{limit}")
        if remaining < min_quota:
            msg = (f"skipped: quota {remaining} below enrichment floor "
                   f"{min_quota}")
            print(f"::warning::Quota {remaining} below enrichment floor "
                  f"{min_quota}; skipping enrichment this run.")
            # Always leave the summary behind — the workflow cats it, and an
            # early return without it failed the whole run.
            (config.ROOT / "enrich_summary.txt").write_text(msg + "\n")
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
                    fh.write("written=0\n")
            return 0
        # Never spend the run's budget down past the floor.
        max_calls = max(0, min(max_calls, remaining - min_quota))

    done, gone = load_enriched(), load_gone()
    candidates = [r for r in load_listings()
                  if r.get("itemId") not in done and r.get("itemId") not in gone]
    batch = pick(candidates, strategy, max_calls)
    print(f"[enrich] {len(candidates)} unenriched, {len(done)} already attempted; "
          f"fetching {len(batch)} ({strategy}, budget {max_calls})")

    now = datetime.now(timezone.utc).isoformat()
    written, ok, missing, failed = [], 0, 0, 0
    newly_gone = []

    for row in batch:
        item_id = row["itemId"]
        resp = client.get(f"{config.API_HOST}/buy/browse/v1/item/{item_id}",
                          allow_status=(400, 403, 404, 410))
        if resp.status_code == 200:
            item = resp.json()
            aspects = fields.aspects_to_dict(item.get("localizedAspects"))
            rec = {"itemId": item_id, "fetchedAt": now, "status": "ok",
                   "aspects": aspects, "aspectCount": len(aspects)}
            for column, candidates_names in fields.ASPECT_COLUMNS.items():
                value, matched = None, None
                for name in candidates_names:
                    if name in aspects:
                        value, matched = aspects[name], name
                        break
                rec[column] = value
                rec[f"{column}_aspect"] = matched
            written.append(rec)
            ok += 1
        elif resp.status_code in (404, 410):
            written.append({"itemId": item_id, "fetchedAt": now, "status": "gone"})
            newly_gone.append({
                "itemId": item_id,
                "itemCreationDate": row.get("itemCreationDate"),
                "firstSeenAt": row.get("firstSeenAt"),
                "disappearedBy": now,
                "price": row.get("price"),
                "title": row.get("title"),
                "sellerUsername": row.get("sellerUsername"),
                "conditionId": row.get("conditionId"),
                "source": "getItem-404",
            })
            missing += 1
        else:
            print(f"[enrich] {item_id} -> HTTP {resp.status_code} {resp.text[:120]}")
            failed += 1
            if resp.status_code == 403:
                print("::warning::getItem returned 403; enrichment access lost. "
                      "Stopping this run.")
                break

    if written:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(ASPECTS, "a") as fh:
            for rec in written:
                fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
    if newly_gone:
        with open(LIFECYCLE, "a") as fh:
            for rec in newly_gone:
                fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")

    coverage = {c: sum(1 for r in written if r.get(c))
                for c in fields.ASPECT_COLUMNS}
    lines = [f"fetched: {len(batch)}  ok: {ok}  gone(404): {missing}  failed: {failed}",
             f"calls used: {client.call_count}"]
    if ok:
        lines.append("field coverage in this batch:")
        for col, n in coverage.items():
            lines.append(f"  {col:14} {n:4}/{ok} = {n/ok*100:5.1f}%")
    total_enriched = len(done) + len(written)
    lines.append(f"total listings attempted all time: {total_enriched}")
    print("\n".join("[enrich] " + l for l in lines))
    (config.ROOT / "enrich_summary.txt").write_text("\n".join(lines) + "\n")

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"written={len(written)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
