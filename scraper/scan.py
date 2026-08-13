"""
Main orchestrator.

Usage:
    python -m scraper.scan --sport basketball
    python -m scraper.scan                      (uses default_sport from config)

Flow (single-stage, since GemRate rebuilt their site):
    1. Load the Top Cards report for the chosen sport/category.
    2. Pull the embedded RowData dataset (population + gem rate per card).
    3. Apply the pop / gem-rate filters, rank, write CSV + JSON + markdown.
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


def load_config():
    with open(ROOT / "config.json") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", default=None)
    parser.add_argument("--reset", action="store_true", help="(kept for compatibility; no-op)")
    parser.add_argument("--max-sets", type=int, default=None, help="(kept for compatibility; no-op)")
    parser.add_argument("--max-cards", type=int, default=None, help="(kept for compatibility; no-op)")
    parser.add_argument("--debug", action="store_true", help="log page status and rows extracted")
    args = parser.parse_args()

    cfg = load_config()
    sport = args.sport or cfg["default_sport"]
    category = cfg["category_map"].get(sport)
    if not category:
        sys.exit(f"Unknown sport '{sport}'. Options: {', '.join(cfg['category_map'])}")

    filters = cfg["filters"]
    limits = cfg["scan_limits"]
    grader = cfg["grader"]

    print(f"=== GemRate scan: {sport} ({grader.upper()}) ===")
    client = GemRateClient(delay_seconds=limits["request_delay_seconds"], debug=args.debug)

    try:
        cards = client.fetch_top_cards(category, grader)
        if not cards:
            sys.exit(
                "No cards found — either Cloudflare blocked the page (GitHub "
                "datacenter IPs are blocked; set the GEMRATE_PROXY secret to a "
                "residential proxy) or the page layout changed. Re-run with "
                "--debug and check the output."
            )
        print(f"Loaded {len(cards)} cards from the Top Cards report.")

        candidates = [m for c in cards if (m := analyze(c, filters))]
        print(f"{len(candidates)} pass pop>={filters['min_total_population']}, "
              f"gem rate<={filters['max_gem_rate_pct']}%")

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
        "score", "year", "set", "card", "number", "parallel",
        "total_pop", "gem_pop", "gem_rate_pct", "url",
    ]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in ranked:
            w.writerow(r)

    md = [f"# GemRate Scan — {sport.title()} — {stamp}\n"]
    md.append("High population + low gem rate, ranked by scarcity × volume. "
              "GemRate no longer publishes sale prices, so verify solds manually.\n")
    md.append("| # | Card | Year | Set | Parallel | Pop | Gem % | Score |")
    md.append("|---|------|------|-----|----------|-----|-------|-------|")
    for i, r in enumerate(ranked[:20], 1):
        md.append(
            f"| {i} | {r['card']} #{r['number']} | {r['year']} | {r['set']} "
            f"| {r['parallel']} | {r['total_pop']:,} | {r['gem_rate_pct']}% | {r['score']} |"
        )
    (RESULTS / f"summary_{sport}.md").write_text("\n".join(md) + "\n")


if __name__ == "__main__":
    main()
