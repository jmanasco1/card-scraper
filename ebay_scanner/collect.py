"""One collection run: search newly-listed graded cards, dedupe, append JSONL."""
import json
import os
from datetime import datetime, timezone

from . import auth, config, fields, query, store, taxonomy
from .client import EbayApiError, EbayClient, browse_remaining
from .summary import write_summary

GET_ITEMS_BATCH = 20


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def main():
    cfg = config.load()
    cid, secret = config.credentials()
    started = datetime.now(timezone.utc)

    token, minted = auth.get_token(cid, secret)
    client = EbayClient(token, cfg["marketplace_id"])

    # --- Quota guard, before any real work -----------------------------
    limits = client.rate_limits()
    remaining, daily_limit, reset = browse_remaining(limits)
    threshold = cfg["quota_abort_threshold"]
    if remaining is not None:
        print(f"[quota] Browse remaining={remaining}/{daily_limit} reset={reset}")
        if remaining < threshold:
            print(f"::warning::eBay Browse quota low: {remaining} calls remaining "
                  f"(threshold {threshold}). Aborting run without searching.")
            write_summary(
                aborted=True, remaining=remaining, daily_limit=daily_limit,
                threshold=threshold, calls_used=client.call_count,
                total_stored=store.total_stored(),
            )
            return 0
    else:
        print("::warning::Could not read Browse quota from getRateLimits; "
              "continuing without a quota guard this run.")

    # --- Verify the category is what we think it is ---------------------
    categories = taxonomy.verify(client, cfg)

    # --- Paginated search ----------------------------------------------
    seen, newest_stored = store.load_index()
    print(f"[collect] {len(seen)} itemIds already stored, "
          f"newest listed {newest_stored or 'n/a'}")

    summaries = {}
    total_matches = None
    pages_fetched = 0
    oldest_fetched = None
    hit_page_cap = False
    for page in range(cfg["max_pages"]):
        offset = page * cfg["limit"]
        params = query.search_params(cfg, offset)
        body = client.search(params)
        if total_matches is None:
            total_matches = body.get("total")
        page_items = body.get("itemSummaries") or []
        print(f"[collect] page {page + 1}: {len(page_items)} summaries "
              f"(offset={offset}, total={total_matches})")
        pages_fetched = page + 1
        for item in page_items:
            item_id = item.get("itemId")
            created = item.get("itemCreationDate")
            if created and (oldest_fetched is None or created < oldest_fetched):
                oldest_fetched = created
            if item_id and item_id not in seen and item_id not in summaries:
                summaries[item_id] = item
        if len(page_items) < cfg["limit"]:
            print("[collect] short page, stopping pagination")
            break
    else:
        hit_page_cap = True

    new_ids = list(summaries)
    print(f"[collect] {len(new_ids)} new itemIds after dedupe")

    # --- Coverage check -------------------------------------------------
    # Pagination reaches back only so far. If the oldest listing this run
    # fetched is newer than the newest one already stored, listings were
    # created and pushed out of reach between runs — a real, unrecoverable gap.
    coverage_gap = False
    if newest_stored and oldest_fetched and oldest_fetched > newest_stored:
        coverage_gap = True
        print(f"::warning::Coverage gap: oldest listing fetched this run "
              f"({oldest_fetched}) is newer than the newest already stored "
              f"({newest_stored}). Listings between those times were missed. "
              f"Fetched {pages_fetched} page(s)"
              f"{' and hit the page cap' if hit_page_cap else ''}; "
              f"raise max_pages or shorten the interval.")
    else:
        print(f"[collect] coverage ok — {pages_fetched} page(s) fetched, "
              f"reached back to {oldest_fetched or 'n/a'}"
              f"{', hit page cap' if hit_page_cap else ''}")

    # --- Enrich with item detail for localizedAspects -------------------
    # The Browse item-detail endpoints sit behind eBay's Buy API access grant.
    # Without it this returns 403, which costs the aspects but not the run:
    # everything item_summary/search provides is still collected.
    details = {}
    aspects_available = bool(cfg.get("enrich_with_get_items"))
    if new_ids and aspects_available:
        for batch in _chunks(new_ids, GET_ITEMS_BATCH):
            try:
                body = client.get_items(batch)
            except EbayApiError as exc:
                if exc.status in (401, 403):
                    print(f"::warning::Item detail unavailable (HTTP {exc.status}): "
                          "the application lacks eBay Buy API access, so "
                          "localizedAspects (grader, grade, cert number, set, "
                          "player, card number) cannot be collected. Listing "
                          "fields from search are stored as normal.")
                    aspects_available = False
                    details = {}
                    break
                raise
            for item in body.get("items") or []:
                if item.get("itemId"):
                    details[item["itemId"]] = item
            for warning in body.get("warnings") or []:
                print(f"[collect] getItems warning: {warning.get('message')}")
        if aspects_available:
            print(f"[collect] fetched detail for {len(details)}/{len(new_ids)} items")

    first_seen = started.isoformat()
    records = [
        fields.build_record(summaries[i], details.get(i), first_seen=first_seen)
        for i in new_ids
    ]
    records.sort(key=lambda r: (r.get("itemCreationDate") or "", r.get("itemId") or ""))

    path = store.append(records)
    if path:
        print(f"[collect] appended {len(records)} records to {path}")
    else:
        print("[collect] nothing new to write")

    total_stored = store.total_stored()
    calls_used = client.call_count
    estimated_remaining = (remaining - calls_used) if remaining is not None else None

    write_summary(
        aborted=False,
        new_records=records,
        new_count=len(records),
        total_stored=total_stored,
        calls_used=calls_used,
        remaining=remaining,
        estimated_remaining=estimated_remaining,
        daily_limit=daily_limit,
        threshold=threshold,
        total_matches=total_matches,
        pages_fetched=pages_fetched,
        hit_page_cap=hit_page_cap,
        coverage_gap=coverage_gap,
        oldest_fetched=oldest_fetched,
        newest_stored=newest_stored,
        categories=categories,
        token_minted=minted,
        partition=path.name if path else None,
        aspects_available=aspects_available,
    )

    # Signal to the workflow whether a commit is warranted.
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"new_count={len(records)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
