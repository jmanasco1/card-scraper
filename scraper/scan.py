"""
Main orchestrator.

Usage:
    python -m scraper.scan --sport basketball
    python -m scraper.scan                      (uses default_sport from config)

Flow:
    1. Enumerate sets for the chosen sport (checkpointed — successive runs
       continue where the last one stopped, so the full database gets covered
       across scheduled runs without any single run taking hours).
    2. Stage 1: scrape each set checklist, apply pop/gem-rate filters.
    3. Stage 2: for survivors only, scrape the card page for recent comps.
    4. Apply price/activity filters, rank, write CSV + JSON + markdown summary.
"""

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

from .gemrate import GemRateClient
from .ranking import analyze, rank

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
CHECKPOINT = ROOT / "results" / ".checkpoint.json"


def load_config():
    with open(ROOT / "config.json") as f:
        return json.load(f)


def load_checkpoint(sport):
    if CHECKPOINT.exists():
        data = json.loads(CHECKPOINT.read_text())
        return data.get(sport, {"done_sets": []})
    return {"done_sets": []}


def save_checkpoint(sport, state):
    data = json.loads(CHECKPOINT.read_text()) if CHECKPOINT.exists() else {}
    data[sport] = state
    CHECKPOINT.write_text(json.dumps(data, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", default=None)
    parser.add_argument("--reset", action="store_true", help="ignore checkpoint, start over")
    parser.add_argument("--max-sets", type=int, default=None, help="override scan_limits.max_sets_per_run")
    parser.add_argument("--max-cards", type=int, default=None, help="override scan_limits.max_card_pages_per_run")
    parser.add_argument("--debug", action="store_true", help="log captured network responses for each page")
    args = parser.parse_args()

    cfg = load_config()
    sport = args.sport or cfg["default_sport"]
    category = cfg["category_map"].get(sport)
    if not category:
        sys.exit(f"Unknown sport '{sport}'. Options: {', '.join(cfg['category_map'])}")

    filters = cfg["filters"]
    limits = cfg["scan_limits"]
    if args.max_sets is not None:
        limits["max_sets_per_run"] = args.max_sets
    if args.max_cards is not None:
        limits["max_card_pages_per_run"] = args.max_cards
    checkpoint = {"done_sets": []} if args.reset else load_checkpoint(sport)

    print(f"=== GemRate scan: {sport} ({cfg['grader'].upper()}) ===")
    client = GemRateClient(delay_seconds=limits["request_delay_seconds"], debug=args.debug)

    try:
        # ---- Stage 0: sets ------------------------------------------------
        sets = client.list_sets(cfg["grader"], category)
        print(f"Found {len(sets)} sets total; {len(checkpoint['done_sets'])} already scanned.")
        todo = [s for s in sets if s["url"] not in checkpoint["done_sets"]]
        todo = todo[: limits["max_sets_per_run"]]

        # ---- Stage 1: checklists + pop/gem filter -------------------------
        survivors = []
        for i, s in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] {s['name']}")
            try:
                cards = client.scrape_set(s["url"], s["name"])
            except Exception as e:
                print(f"  ! failed: {e}")
                continue
            passed = [
                c for c in cards
                if c.total_pop >= filters["min_total_population"]
                and 0 < c.gem_rate_pct <= filters["max_gem_rate_pct"]
            ]
            print(f"  {len(cards)} cards, {len(passed)} pass pop/gem filters")
            survivors.extend(passed)
            checkpoint["done_sets"].append(s["url"])
            save_checkpoint(sport, checkpoint)

        # ---- Stage 2: comps for survivors ---------------------------------
        survivors = survivors[: limits["max_card_pages_per_run"]]
        print(f"\nFetching comps for {len(survivors)} candidates...")
        candidates = []
        for i, card in enumerate(survivors, 1):
            try:
                client.scrape_card(card)
            except Exception as e:
                print(f"  ! {card.card_name}: {e}")
                continue
            metrics = analyze(card, filters)
            if metrics:
                candidates.append(metrics)
            if i % 25 == 0:
                print(f"  {i}/{len(survivors)} done, {len(candidates)} qualify so far")

        # ---- Rank + output ------------------------------------------------
        ranked = rank(candidates, cfg["ranking_weights"], filters["max_gem_rate_pct"])
        ranked = ranked[: limits["max_results"]]
        write_outputs(sport, ranked)
        print(f"\nDone. {len(ranked)} ranked results written to results/")

    finally:
        client.close()


def write_outputs(sport, ranked):
    RESULTS.mkdir(exist_ok=True)
    stamp = date.today().isoformat()

    json_path = RESULTS / f"latest_{sport}.json"
    json_path.write_text(json.dumps({"date": stamp, "results": ranked}, indent=2))

    csv_path = RESULTS / f"latest_{sport}.csv"
    fields = [
        "score", "set", "card", "number", "total_pop", "gem_pop", "gem_rate_pct",
        "recent_sales", "oldest_comp", "latest_comp", "momentum_pct", "url",
    ]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in ranked:
            w.writerow(r)

    md = [f"# GemRate Scan — {sport.title()} — {stamp}\n"]
    md.append("| # | Card | Set | Pop | Gem % | Last 2 comps | Momentum | Score |")
    md.append("|---|------|-----|-----|-------|--------------|----------|-------|")
    for i, r in enumerate(ranked[:20], 1):
        md.append(
            f"| {i} | {r['card']} #{r['number']} | {r['set']} | {r['total_pop']:,} "
            f"| {r['gem_rate_pct']}% | ${r['oldest_comp']:,.0f} → ${r['latest_comp']:,.0f} "
            f"| {r['momentum_pct']:+.1f}% | {r['score']} |"
        )
    (RESULTS / f"summary_{sport}.md").write_text("\n".join(md) + "\n")


if __name__ == "__main__":
    main()
