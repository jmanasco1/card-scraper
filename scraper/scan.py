"""
Main orchestrator.

Usage:
    python -m scraper.scan --sport basketball
    python -m scraper.scan                      (uses default_sport from config)

Flow:
    1. Load GemRate's Top Cards report for the sport -> population + gem rate.
    2. Keep cards passing the pop / gem-rate filters, pre-rank by scarcity.
    3. For the top candidates, pull recent PSA-10 sold comps from eBay and
       compute price momentum.
    4. Rank by momentum + scarcity + sales activity, write CSV/JSON/markdown.
"""

import argparse
import csv
import json
import math
import sys
from datetime import date
from pathlib import Path

from .gemrate import GemRateClient
from . import comps as comps_mod
from .ranking import analyze, rank

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"


def load_config():
    with open(ROOT / "config.json") as f:
        return json.load(f)


def scarcity(m, max_gem_rate):
    return (1.0 - m["gem_rate_pct"] / max_gem_rate) * math.log10(max(m["total_pop"], 10))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", default=None)
    parser.add_argument("--reset", action="store_true", help="(kept for compatibility; no-op)")
    parser.add_argument("--max-sets", type=int, default=None, help="(kept for compatibility; no-op)")
    parser.add_argument("--max-cards", type=int, default=None, help="max candidates to look up on eBay")
    parser.add_argument("--debug", action="store_true", help="log page status and per-card comp counts")
    args = parser.parse_args()

    cfg = load_config()
    sport = args.sport or cfg["default_sport"]
    category = cfg["category_map"].get(sport)
    if not category:
        sys.exit(f"Unknown sport '{sport}'. Options: {', '.join(cfg['category_map'])}")

    filters = cfg["filters"]
    limits = cfg["scan_limits"]
    grader = cfg["grader"]
    max_gem = filters["max_gem_rate_pct"]
    lookup_limit = args.max_cards or limits.get("max_comp_lookups", 40)

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
        candidates.sort(key=lambda m: scarcity(m, max_gem), reverse=True)
        print(f"{len(candidates)} pass pop>={filters['min_total_population']}, "
              f"gem rate<={max_gem}%")

        # ---- eBay comps for the strongest scarcity candidates --------------
        lookups = candidates[:lookup_limit]
        print(f"\nLooking up recent eBay PSA-10 comps for top {len(lookups)} candidates...")
        for i, m in enumerate(lookups, 1):
            query = comps_mod.build_query(m)
            comps = comps_mod.fetch_comps(
                client.page, query, delay=limits["request_delay_seconds"], debug=args.debug
            )
            summary = comps_mod.summarize(comps, filters.get("min_recent_sales", 3))
            if summary:
                m.update(summary)
            if not args.debug and i % 10 == 0:
                print(f"  {i}/{len(lookups)} looked up")

        # Optionally drop cards whose latest comp is below the price floor
        min_price = filters.get("min_price_usd", 0)
        ranked_pool = [
            m for m in candidates
            if m["latest_comp"] is None or m["latest_comp"] >= min_price
        ]

        ranked = rank(ranked_pool, cfg["ranking_weights"], max_gem)
        ranked = ranked[: limits["max_results"]]
        with_comps = sum(1 for r in ranked if r["recent_sales"])
        print(f"{with_comps} of the top {len(ranked)} have recent eBay comps for momentum.")
        write_outputs(sport, ranked)
        print(f"\nDone. {len(ranked)} ranked results written to results/")
    finally:
        client.close()


def write_outputs(sport, ranked):
    RESULTS.mkdir(exist_ok=True)
    stamp = date.today().isoformat()

    (RESULTS / f"latest_{sport}.json").write_text(
        json.dumps({"date": stamp, "results": ranked}, indent=2)
    )

    csv_path = RESULTS / f"latest_{sport}.csv"
    fields = [
        "score", "year", "set", "card", "number", "parallel",
        "total_pop", "gem_pop", "gem_rate_pct",
        "recent_sales", "oldest_comp", "latest_comp", "momentum_pct", "url",
    ]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in ranked:
            w.writerow(r)

    md = [f"# GemRate Scan — {sport.title()} — {stamp}\n"]
    md.append("Ranked by price momentum (recent eBay PSA-10 solds) + gem-rate "
              "scarcity + sales activity. A dash in the comp columns means too "
              "few recent sales to judge momentum — verify manually.\n")
    md.append("| # | Card | Year | Set | Pop | Gem % | Comps | $ trend | Mom. | Score |")
    md.append("|---|------|------|-----|-----|-------|-------|---------|------|-------|")
    for i, r in enumerate(ranked[:25], 1):
        if r["recent_sales"]:
            trend = f"${r['oldest_comp']:,.0f}→${r['latest_comp']:,.0f}"
            mom = f"{r['momentum_pct']:+.0f}%"
            n = str(r["recent_sales"])
        else:
            trend, mom, n = "—", "—", "—"
        md.append(
            f"| {i} | {r['card']} #{r['number']} | {r['year']} | {r['set']} "
            f"| {r['total_pop']:,} | {r['gem_rate_pct']}% | {n} | {trend} | {mom} | {r['score']} |"
        )
    (RESULTS / f"summary_{sport}.md").write_text("\n".join(md) + "\n")


if __name__ == "__main__":
    main()
