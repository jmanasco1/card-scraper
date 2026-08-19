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
from .grading import rank_by_ev
from .ranking import analyze, cohort_stats, measured, rank, select_for_pricing
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
    if stats.get("no_raw_prices"):
        return ("Graded prices came back, but no raw (ungraded) sales were found for "
                "these cards, and the raw price is what the grading maths is bought "
                "at. Vintage cards in particular rarely trade raw. Try a scan weighted "
                "to modern cards.")
    if stats.get("measured_but_no_dislocation"):
        return ("Priced and measured {n} cards, but none showed a PSA 10 falling far "
                "enough against its own PSA 9 to flag. Their prices and price history "
                "are listed below anyway. In a pool of the most-traded cards in the "
                "hobby that is a plausible answer, not a fault."
                ).format(n=stats["measured_but_no_dislocation"])
    if stats.get("priced_ok_but_no_trend"):
        return ("Prices came back fine, but not enough sales per card to measure a "
                "trend: each grade needs at least 6 usable sales and these had "
                "fewer. Raise --limit so more cards are tried, or loosen the "
                "population band in config.json toward more heavily-traded cards.")
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

        ranked = rank_by_ev(
            targets,
            min_edge_usd=filters.get("min_edge_usd", 20),
            min_roi_pct=filters.get("min_roi_pct", 15),
            min_sales=filters["min_sales_per_grade"],
        )
        stats = cohort_stats(targets)

        print(f"\nFitted premium curve: {model['kind']}, "
              f"{model.get('n_fit', model['n'])}/{model['n']} cards, R²={model['r_squared']:.2f}")
        if model["kind"] == "loglog":
            print(f"  slope {model['slope']:.2f} — "
                  f"{'scarcer 10s do command higher premiums in this pool' if model['slope'] > 0.15 else 'this pool barely prices scarcity at all, treat results as weak'}")
        dated = sum(1 for m in targets if m.get("p10_trend") and m.get("p9_trend"))
        priced = sum(1 for m in targets if m.get("p10_median") and m.get("p9_median"))
        with_raw = sum(1 for m in targets if m.get("raw_median"))
        print(f"{with_raw} of {len(targets)} also had raw (ungraded) sales, "
              f"which is what the grading maths needs.")
        if priced and not dated:
            lookup_stats["priced_ok_but_no_trend"] = priced
        if priced and not with_raw:
            lookup_stats["no_raw_prices"] = priced
        if dated and not ranked:
            lookup_stats["measured_but_no_dislocation"] = dated
        print(f"{dated} of {len(targets)} cards had enough dated sales at both "
              f"grades to measure a trend; {len(ranked)} show a dislocation.")

        problem = diagnose(lookup_stats, bool(ranked))
        if problem:
            print(f"\n!! {problem}")

        # Show every card we priced, not only the ones clearing the threshold.
        # Show every card the maths could be run on, findings flagged.
        from .grading import grading_ev
        shown = [m for m in targets if grading_ev(m)]
        for m in shown:
            m.setdefault("is_finding", False)
        for m in ranked:
            m["is_finding"] = True
        shown.sort(key=lambda m: m.get("grade_edge") or 0, reverse=True)
        write_outputs(sport, shown[: limits["max_results"]], model, stats, uni_report,
                      problem=problem, findings=len(ranked))
        print(f"\nDone. Open the report:  results/report_{sport}.html")
    finally:
        client.close()


FIELDS = [
    "is_finding", "grade_edge", "grade_roi_pct", "breakeven_gem_pct", "score", "confidence",
    "raw_median", "raw_sales", "ev_cost", "grade_ev",
    "year", "set", "card", "number", "parallel",
    "total_pop", "gem_pop", "gem_rate_pct",
    "p10_was", "p10_now", "p10_change_pct", "p10_sales",
    "p9_was", "p9_now", "p9_change_pct", "p9_sales",
    "liquidity", "ebay_url_10", "ebay_url_9", "url",
]


def flatten_trends(rows):
    """Lift the nested trend dicts into flat columns for CSV."""
    for r in rows:
        for pref in ("p10", "p9"):
            t = r.get(f"{pref}_trend") or {}
            r[f"{pref}_was"] = t.get("older_median")
            r[f"{pref}_now"] = t.get("recent_median")
            r[f"{pref}_change_pct"] = t.get("change_pct")
    return rows


def write_outputs(sport, ranked, model, stats, uni_report, problem=None, findings=0):
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
        report_mod.render(sport, ranked, model, stats, uni_report, problem=problem,
                          findings=findings)
    )

    with open(RESULTS / f"latest_{sport}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in flatten_trends(list(ranked)):
            w.writerow(r)

    md = [f"# GemRate Value Scan — {sport.title()} — {stamp}\n"]
    md.append(
        "Every figure is an observed sold price. Each grade shows what the card "
        "used to fetch, what it fetches now, and how many sales are behind each. "
        "The last column is the difference between the two moves — a PSA 10 "
        "falling while its own PSA 9 holds is dislocated against itself. "
        "★ marks a card that cleared the flag threshold.\n"
    )
    if stats:
        md.append(
            f"{stats['priced']} cards priced at both grades, median year "
            f"{stats.get('median_year')}, median population "
            f"{stats.get('median_pop', 0):,}.\n"
        )
    if findings == 0 and ranked:
        md.append(
            "> No card's PSA 10 fell far enough against its own PSA 9 to flag. "
            "The prices below are still real — sort by the last column to see "
            "which came closest.\n"
        )
    md.append(
        "\n| # | Card | Year | Set | Pop | Gem % | PSA 10 | PSA 9 | 10 vs 9 | Trust | Check |"
    )
    md.append("|---|------|------|-----|-----|-------|--------|-------|---------|-------|-------|")

    def move(r, pref):
        t = r.get(f"{pref}_trend")
        if not t:
            return "—"
        return (f"${t['older_median']:,.0f} → ${t['recent_median']:,.0f} "
                f"({t['change_pct']:+.0f}%, n={t['older_n']}+{t['recent_n']})")

    for i, r in enumerate(ranked[:25], 1):
        mark = "★" if r.get("is_finding") else str(i)
        md.append(
            f"| {mark} | {r['card']} #{r['number']} | {r['year']} | {r['set']} "
            f"| {r['total_pop']:,} | {r['gem_rate_pct']}% "
            f"| {move(r, 'p10')} | {move(r, 'p9')} "
            f"| {(r.get('dislocation_pct') or 0):+.0f} pts "
            f"| {r.get('confidence')} "
            f"| [10]({r.get('ebay_url_10','')}) · [9]({r.get('ebay_url_9','')}) |"
        )
    md.append(
        "\n**A dislocation is a question, not an answer.** The 10 falling behind "
        "its own 9 can mean the 10 is cheap, or that several 10s hit the market "
        "at once. Open both eBay links and check the comps are the same card "
        "before acting on any row.\n"
    )
    (RESULTS / f"summary_{sport}.md").write_text("\n".join(md) + "\n")


if __name__ == "__main__":
    main()
