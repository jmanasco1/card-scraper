"""
Main orchestrator: find PSA 10s that trade below what their scarcity is worth.

Flow:
    1. Build the widest card universe the site will give us (scraper/gemrate).
    2. Filter to a population band that excludes both illiquid cards and the
       mega-pop blue chips, keeping a wide gem-rate spread (scraper/ranking).
    3. Stratified-sample the cards to price, so the fitted curve spans the
       whole scarcity range instead of one corner of it.
    4. Price each at PSA 10 and PSA 9 on eBay (scraper/comps), cached on disk.
    5. Fit the cohort's scarcity->premium curve and score each card's discount
       against it (scraper/value).
    6. Write CSV / JSON / markdown, best edge first.

Usage:
    python -m scraper.scan --sport basketball
    python -m scraper.scan --sport basketball --limit 60 --debug
    python -m scraper.scan --sport basketball --universe top   (old blue-chip pool)
"""

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

from . import cache as cache_mod
from . import comps as comps_mod
from . import report as report_mod
from .gemrate import GemRateClient
from .ranking import analyze, cohort_stats, rank, select_for_pricing
from .value import fit_cohort, score_card

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"


def load_config():
    with open(ROOT / "config.json") as f:
        return json.load(f)


def build_universe(client, cfg, category, grader, mode, debug):
    """Return (cards, report). `mode` is 'wide' (sweep years) or 'top'."""
    if mode == "top":
        return client.fetch_top_cards(category, grader), {"year_param_honored": None}
    u = cfg.get("universe", {})
    years = list(range(u.get("year_from", 1990), u.get("year_to", 2023) + 1))
    return client.walk_universe(category, grader, years=years, debug=debug)


def price_candidates(client, targets, cfg, debug, fresh=False):
    """Price each target on eBay, using and updating the on-disk cache."""
    limits = cfg["scan_limits"]
    ttl = limits.get("cache_ttl_days", 7)
    cache = cache_mod.load()
    hits = 0
    stats = {}

    print(f"\nPricing {len(targets)} cards on eBay (PSA 10 + PSA 9).")
    print("Solve any captcha in the browser window when prompted — the run waits for you.\n")

    for i, m in enumerate(targets, 1):
        if not fresh and cache_mod.get(cache, m, ttl):
            hits += 1
            if debug:
                print(f"  [{i}/{len(targets)}] cached: {m['year']} {m['set']} {m['card']}")
            continue
        if debug:
            print(f"  [{i}/{len(targets)}] {m['year']} {m['set']} {m['card']} #{m['number']}")
        comps_mod.price_card(
            client.page, m,
            delay=limits["request_delay_seconds"], debug=debug, interactive=True,
            stats=stats,
        )
        if cache_mod.put(cache, m):
            cache_mod.save(cache)
        if not debug and i % 10 == 0:
            print(f"  priced {i}/{len(targets)}")

    if hits:
        print(f"  ({hits} of {len(targets)} came from cache — delete "
              f"results/.price_cache.json to force a refresh)")

    write_audit(stats)
    return stats


def write_audit(stats):
    """Dump exactly what eBay returned, per search.

    When prices come back wrong the question is always the same — what did the
    page actually contain, and which rule threw it away — and that cannot be
    answered from the outside. This writes it to one small file that can be
    read or sent on directly.
    """
    audit = stats.get("audit")
    if not audit:
        return
    RESULTS.mkdir(exist_ok=True)
    out = ["eBay comp audit", "=" * 78,
           "Each block is one search. 'kept' lines became comps; 'rejected' lines",
           "show which rule discarded them. Padding that eBay appends to a thin",
           "result set should not appear at all.", ""]
    for e in audit:
        out.append("=" * 78)
        out.append(f"QUERY: {e['query']}   (PSA {e['grade']})")
        out.append(f"  listings captured: {e['listings']}   matching: {e.get('matching','?')}")
        out.append(f"  prices used: {e.get('prices') or 'none'}")
        if e["kept"]:
            out.append(f"  --- kept ({len(e['kept'])}) ---")
            out.extend("    " + k for k in e["kept"][:12])
        if e["rejects"]:
            out.append(f"  --- rejected ({len(e['rejects'])}) ---")
            out.extend("    " + r for r in e["rejects"][:15])
        out.append("")
    path = RESULTS / "comp_audit.txt"
    path.write_text("\n".join(out))
    print(f"\nWrote {path} — send this if the prices look wrong.")


def diagnose(stats, priced_ok):
    """Turn the lookup counters into one sentence naming what went wrong.
    Returns None when pricing broadly worked."""
    attempted = stats.get("attempted", 0)
    if priced_ok:
        return None
    if not attempted:
        return ("No eBay lookups ran at all. Every card was served from the price "
                "cache, so nothing was fetched. Delete results/.price_cache.json "
                "and re-run, or pass --fresh.")
    if stats.get("captcha_blocked"):
        return ("eBay served a captcha on {n} of {a} lookups and it was never cleared, "
                "so no prices came back. Re-run and solve the captcha in the browser "
                "window when it appears — the scan waits for you."
                ).format(n=stats["captcha_blocked"], a=attempted * 2)
    if stats.get("no_listings") and not stats.get("listings_seen"):
        return ("eBay returned pages with no recognisable listings on them ({n} lookups). "
                "That usually means eBay changed its result markup, or the searches "
                "genuinely have no sold results. Re-run with --debug to see the pages."
                ).format(n=stats["no_listings"])
    if stats.get("all_filtered"):
        return ("eBay showed {seen} listings but every one was rejected as not matching "
                "the card ({n} lookups). The title filter is likely too strict for these "
                "cards — re-run with --debug to see which titles were thrown out."
                ).format(seen=stats.get("listings_seen", 0), n=stats["all_filtered"])
    if stats.get("nav_failed"):
        return ("Could not load eBay for {n} lookups — a network problem rather than a "
                "scraping one.").format(n=stats["nav_failed"])
    return ("Prices came back empty and the cause is not obvious. Re-run with --debug "
            "and check the per-card lines.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", default=None)
    parser.add_argument("--universe", choices=("wide", "top"), default="wide",
                        help="'wide' sweeps per-year reports for the long tail; "
                             "'top' uses only the all-time top-cards report")
    parser.add_argument("--limit", type=int, default=None,
                        help="how many cards to price on eBay this run")
    parser.add_argument("--fresh", action="store_true",
                        help="ignore the price cache and re-fetch every card")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    sport = args.sport or cfg["default_sport"]
    category = cfg["category_map"].get(sport)
    if not category:
        sys.exit(f"Unknown sport '{sport}'. Options: {', '.join(cfg['category_map'])}")

    filters = cfg["filters"]
    limits = cfg["scan_limits"]
    grader = cfg["grader"]
    lookup_limit = args.limit or limits.get("max_price_lookups", 40)

    print(f"=== GemRate value scan: {sport} ({grader.upper()}) ===")
    try:
        client = GemRateClient(delay_seconds=limits["request_delay_seconds"], debug=args.debug)
    except Exception as e:
        sys.exit(
            f"Could not start a browser: {e}\n\n"
            "The scan drives a real, visible browser — headless mode is the "
            "clearest signal\nCloudflare's bot check looks for, and eBay "
            "captchas need a window you can see.\n\n"
            "Fix: install Google Chrome from https://www.google.com/chrome/ "
            "and re-run.\nIf Chrome is already installed, run "
            "`playwright install chrome` in this folder."
        )

    try:
        cards, uni_report = build_universe(client, cfg, category, grader, args.universe, args.debug)
        if not cards:
            sys.exit(
                "No cards found — either Cloudflare blocked the page (datacenter "
                "IPs are blocked; run this from a home connection, or set "
                "GEMRATE_PROXY to a residential proxy) or the page layout "
                "changed. Re-run with --debug, then `python -m scraper.recon`."
            )
        print(f"\nUniverse: {len(cards)} distinct cards.")

        candidates = [m for c in cards if (m := analyze(c, filters))]
        print(f"{len(candidates)} inside the population band "
              f"({filters['min_total_population']:,}–{filters['max_total_population']:,}) "
              f"and gem-rate band ({filters['min_gem_rate_pct']}–{filters['max_gem_rate_pct']}%).")
        if not candidates:
            sys.exit("Nothing passed the filters — widen them in config.json.")

        targets = select_for_pricing(candidates, lookup_limit)
        for m in targets:
            m["ebay_url_10"] = comps_mod.build_ebay_url(m, 10)
            m["ebay_url_9"] = comps_mod.build_ebay_url(m, 9)

        lookup_stats = price_candidates(client, targets, cfg, args.debug, args.fresh)

        model = fit_cohort(targets)
        for m in targets:
            score_card(m, model)

        ranked = rank(targets, cfg["ranking_weights"], filters["min_sales_per_grade"],
                      filters.get("min_edge_usd", 25))
        stats = cohort_stats(targets)

        print(f"\nFitted premium curve: {model['kind']}, "
              f"{model.get('n_fit', model['n'])}/{model['n']} cards, R²={model['r_squared']:.2f}")
        if model["kind"] == "loglog":
            print(f"  slope {model['slope']:.2f} — "
                  f"{'scarcer 10s do command higher premiums in this pool' if model['slope'] > 0.15 else 'this pool barely prices scarcity at all, treat results as weak'}")
        print(f"{len(ranked)} cards scoreable with comps at both grades.")

        problem = diagnose(lookup_stats, bool(ranked))
        if problem:
            print(f"\n!! {problem}")

        write_outputs(sport, ranked[: limits["max_results"]], model, stats, uni_report,
                      problem=problem)
        print(f"\nDone. Open the report:  results/report_{sport}.html")
    finally:
        client.close()


FIELDS = [
    "score", "discount_pct", "confidence", "year", "set", "card", "number", "parallel",
    "total_pop", "gem_pop", "gem_rate_pct",
    "p9_median", "p9_sales", "p10_median", "p10_sales",
    "gap", "expected_gap", "value_ratio", "liquidity",
    "ebay_url_10", "ebay_url_9", "url",
]


def write_outputs(sport, ranked, model, stats, uni_report, problem=None):
    RESULTS.mkdir(exist_ok=True)
    stamp = date.today().isoformat()

    (RESULTS / f"latest_{sport}.json").write_text(json.dumps({
        "date": stamp,
        "model": {k: v for k, v in model.items() if k != "predict"},
        "cohort": stats,
        "universe": uni_report,
        "results": ranked,
    }, indent=2))

    # The page people actually read. Written first so that even if a later
    # writer trips, the human-facing output exists.
    (RESULTS / f"report_{sport}.html").write_text(
        report_mod.render(sport, ranked, model, stats, uni_report, problem=problem)
    )

    with open(RESULTS / f"latest_{sport}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in ranked:
            w.writerow(r)

    md = [f"# GemRate Value Scan — {sport.title()} — {stamp}\n"]
    md.append(
        "Ranks PSA 10s by how far **below** the cohort's own pricing of scarcity "
        "they trade. `gap` is the actual PSA 10 / PSA 9 price ratio; `fair` is what "
        "this pool pays for that gem rate. A discount means the 10 is cheap for how "
        "hard it is to pull.\n"
    )
    if stats:
        md.append(
            f"Cohort: {stats['priced']} cards priced at both grades, median year "
            f"{stats.get('median_year')}, median population {stats.get('median_pop'):,}, "
            f"median 10/9 ratio {stats['median_gap']}×. "
            f"Curve fit R² {model['r_squared']:.2f}"
            + (f", slope {model['slope']:.2f}.\n" if model["kind"] == "loglog" else ".\n")
        )
    if model["r_squared"] < 0.25:
        md.append(
            "> **Weak fit.** This pool doesn't price scarcity consistently, so the "
            "discounts below are noisy. Price more cards (`--limit`) before trusting them.\n"
        )
    md.append(
        "\n| # | Card | Year | Set | Pop | Gem % | PSA 9 | PSA 10 | Gap | Fair | Disc. | Conf. | Edge | Check |"
    )
    md.append("|---|------|------|-----|-----|-------|-------|--------|-----|------|-------|-------|------|-------|")
    for i, r in enumerate(ranked[:25], 1):
        md.append(
            f"| {i} | {r['card']} #{r['number']} | {r['year']} | {r['set']} "
            f"| {r['total_pop']:,} | {r['gem_rate_pct']}% "
            f"| ${r['p9_median']:,.0f} ({r['p9_sales']}) "
            f"| ${r['p10_median']:,.0f} ({r['p10_sales']}) "
            f"| {r['gap']}× | {r['expected_gap']}× | {r['discount_pct']:+.0f}% "
            f"| {r['confidence']} | {r['score']:+.1f} "
            f"| [10]({r['ebay_url_10']}) · [9]({r['ebay_url_9']}) |"
        )
    md.append(
        "\n**Read it like this:** a high edge means the market is charging little "
        "for a 10 that is genuinely hard to get. Verify every one by hand before "
        "buying — click the PSA 9 and PSA 10 links and check the comps are really "
        "the same card. Thin comps (low counts) are the usual reason a discount "
        "turns out to be a mirage.\n"
    )
    (RESULTS / f"summary_{sport}.md").write_text("\n".join(md) + "\n")


if __name__ == "__main__":
    main()
