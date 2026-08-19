"""
Offline end-to-end test.

The live path can't be exercised from CI — Cloudflare blocks datacenter IPs —
so the pipeline is tested against a simulated card universe and a simulated
eBay. The point is to prove the scoring chain surfaces genuinely underpriced
cards and doesn't just re-rank by fame the way the old version did.
"""

import math
import statistics
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.gemrate import CardRow, extract_rowdata
from scraper.ranking import analyze, rank, select_for_pricing
from scraper.value import confidence, fit_cohort, score_card
from scraper import comps

FILTERS = {
    "min_total_population": 300, "max_total_population": 20000,
    "min_gem_rate_pct": 0.1, "max_gem_rate_pct": 60, "min_sales_per_grade": 3,
}
WEIGHTS = {"liquidity": 0.15}

# The market's true relationship in this simulation: a scarcer 10 commands a
# bigger premium over the 9.
TRUE_SLOPE = 0.55


def simulate_universe(n=200, seed=11):
    """A pool spanning the full range of years, populations and gem rates."""
    rng = random.Random(seed)
    cards = []
    for i in range(n):
        cards.append(CardRow(
            set_name=rng.choice(["Topps Chrome", "Prizm", "Select", "Donruss Optic"]),
            card_name=f"Player {i}",
            card_number=str(i),
            url="https://example.test/c",
            total_pop=rng.randint(400, 18000),
            gem_pop=0,
            gem_rate_pct=round(rng.uniform(0.4, 50.0), 2),
            year=str(rng.randint(1990, 2023)),
            parallel="Base",
        ))
    return cards


def simulate_prices(m, rng, mispricing=1.0, sales=12):
    """Give a card eBay prices consistent with the true curve, times a
    deliberate mispricing factor (<1 = the 10 is cheap)."""
    fair_gap = math.exp(0.2 + TRUE_SLOPE * math.log(100 / max(m["gem_rate_pct"], 0.1)))
    gap = fair_gap * mispricing * rng.uniform(0.93, 1.07)
    p9 = rng.uniform(30, 500)
    m["p9_median"], m["p9_sales"], m["p9_spread"] = round(p9, 2), sales, 0.3
    m["p10_median"], m["p10_sales"], m["p10_spread"] = round(p9 * gap, 2), sales, 0.3


def simulate_trend(m, p10_move=0.0, p9_move=0.0, n=12):
    """Attach dated price history to a card: `*_move` is the fractional change
    from its earlier sales to its recent ones."""
    def trend(median, move):
        older = median
        recent = median * (1.0 + move)
        return {"older_median": round(older, 2), "recent_median": round(recent, 2),
                "older_n": n - max(2, n // 3), "recent_n": max(2, n // 3),
                "change_pct": round(100.0 * move, 1),
                "first": "2025-01-01", "last": "2025-06-01"}
    m["p10_trend"] = trend(m["p10_median"], p10_move)
    m["p9_trend"] = trend(m["p9_median"], p9_move)


class TestValueModel(unittest.TestCase):
    def test_recovers_true_slope(self):
        rng = random.Random(3)
        cards = [analyze(c, FILTERS) for c in simulate_universe()]
        cards = [c for c in cards if c]
        for m in cards:
            simulate_prices(m, rng)
        model = fit_cohort(cards)
        self.assertEqual(model["kind"], "loglog")
        self.assertAlmostEqual(model["slope"], TRUE_SLOPE, delta=0.12)
        self.assertGreater(model["r_squared"], 0.8)

    def test_surfaces_dislocated_cards_not_famous_ones(self):
        """The 10 falling while its own 9 holds is the finding. Population,
        which the very first version of this tool ranked on, earns nothing."""
        rng = random.Random(5)
        cards = [m for c in simulate_universe() if (m := analyze(c, FILTERS))]
        for m in cards:
            simulate_prices(m, rng, sales=15)
            simulate_trend(m, p10_move=rng.uniform(-0.04, 0.04),
                           p9_move=rng.uniform(-0.04, 0.04))

        dislocated = cards[:3]
        for m in dislocated:
            simulate_trend(m, p10_move=-0.35, p9_move=0.0)

        # A hugely popular card whose grades moved together is not a finding,
        # however famous it is.
        famous = cards[10]
        famous["total_pop"] = 19000
        simulate_trend(famous, p10_move=-0.30, p9_move=-0.30)

        ranked = rank(cards, WEIGHTS, FILTERS["min_sales_per_grade"])
        top3 = [id(m) for m in ranked[:3]]
        for m in dislocated:
            self.assertIn(id(m), top3, "planted dislocation missed the top 3")
        self.assertNotIn(id(famous), [id(m) for m in ranked],
                         "both grades moving together is not a dislocation")

    def test_ten_running_ahead_is_not_a_finding(self):
        """A 10 outpacing its 9 is a hot card, not a cheap one."""
        rng = random.Random(8)
        cards = [m for c in simulate_universe(40) if (m := analyze(c, FILTERS))]
        for m in cards:
            simulate_prices(m, rng, sales=15)
            simulate_trend(m, p10_move=0.40, p9_move=0.0)
        self.assertEqual(rank(cards, WEIGHTS, 3), [])

    def test_cards_without_dated_sales_do_not_rank(self):
        rng = random.Random(2)
        cards = [m for c in simulate_universe(40) if (m := analyze(c, FILTERS))]
        for m in cards:
            simulate_prices(m, rng, sales=15)   # no trend attached
        self.assertEqual(rank(cards, WEIGHTS, 3), [])

    def test_bigger_dislocation_ranks_higher(self):
        rng = random.Random(9)
        cards = [m for c in simulate_universe(60) if (m := analyze(c, FILTERS))]
        for m in cards:
            simulate_prices(m, rng, sales=15)
            simulate_trend(m, p10_move=0.0, p9_move=0.0)
        big, small = cards[0], cards[1]
        simulate_trend(big, p10_move=-0.50, p9_move=0.05)
        simulate_trend(small, p10_move=-0.18, p9_move=0.0)

        ranked = rank(cards, WEIGHTS, FILTERS["min_sales_per_grade"])
        self.assertIs(ranked[0], big)
        self.assertIn(id(small), [id(m) for m in ranked])
        self.assertGreater(big["score"], small["score"])

    def test_liquidity_never_promotes_an_overpriced_card(self):
        """A heavily-traded card that is richly priced must still rank below a
        thinly-traded one that is cheap."""
        from scraper.ranking import score
        rich_liquid = {"discount_pct": -40.0, "p10_sales": 50, "p9_sales": 50}
        cheap_thin = {"discount_pct": 20.0, "p10_sales": 6, "p9_sales": 6}
        self.assertLess(score(rich_liquid, 1.0, WEIGHTS), score(cheap_thin, 1.0, WEIGHTS))
        # And liquidity must not inflate a negative edge toward zero either.
        self.assertLessEqual(score(rich_liquid, 1.0, WEIGHTS), -40.0)

    def test_unpriced_cards_are_excluded(self):
        cards = [m for c in simulate_universe(30) if (m := analyze(c, FILTERS))]
        model = fit_cohort(cards)          # nothing priced at all
        self.assertEqual(model["kind"], "flat")
        for m in cards:
            self.assertFalse(score_card(m, model))
        self.assertEqual(rank(cards, WEIGHTS, 3), [])

    def test_confidence_gates_thin_comps(self):
        thin = {"p10_sales": 3, "p9_sales": 3}
        deep = {"p10_sales": 25, "p9_sales": 25}
        self.assertLess(confidence(thin, 3), confidence(deep, 3))
        self.assertEqual(confidence({"p10_sales": 1, "p9_sales": 30}, 3), 0.0)


class TestSampling(unittest.TestCase):
    def test_stratified_sample_spans_scarcity_range(self):
        cards = [m for c in simulate_universe(300) if (m := analyze(c, FILTERS))]
        picked = select_for_pricing(cards, 40)
        self.assertEqual(len(picked), 40)
        rates = [m["gem_rate_pct"] for m in picked]
        full = [m["gem_rate_pct"] for m in cards]
        # The sample must cover most of the pool's gem-rate range, or the fit
        # has no x-variation to work with.
        coverage = (max(rates) - min(rates)) / (max(full) - min(full))
        self.assertGreater(coverage, 0.85)

    def test_sample_smaller_than_limit_returns_all(self):
        cards = [m for c in simulate_universe(10) if (m := analyze(c, FILTERS))]
        self.assertEqual(len(select_for_pricing(cards, 40)), len(cards))


class TestFilters(unittest.TestCase):
    def test_population_band_excludes_blue_chips(self):
        blue_chip = CardRow("Fleer", "Michael Jordan", "57", "", 31213, 341, 1.09, "1986", "Base")
        self.assertIsNone(analyze(blue_chip, FILTERS),
                          "mega-pop blue chip should be filtered out of a value scan")

    def test_population_band_excludes_illiquid(self):
        thin = CardRow("Obscure", "Nobody", "4", "", 42, 3, 7.1, "2015", "Base")
        self.assertIsNone(analyze(thin, FILTERS))

    def test_wide_gem_rate_band_keeps_common_cards(self):
        common = CardRow("Prizm", "Someone", "88", "", 2000, 800, 40.0, "2019", "Base")
        self.assertIsNotNone(analyze(common, FILTERS),
                             "high gem rates must stay in — the curve needs that end")


class TestRowDataParsing(unittest.TestCase):
    def test_extracts_embedded_dataset(self):
        html = (
            "<script>var RowData = JSON.parse('"
            '[{"card_number": "8", "gems": 127, "name": "Michael Jordan", '
            '"parallel": "Base", "set_name": "Fleer Sticker", "total": 23927, '
            '"year": "1986", "grader": "psa", "trend_links": {"psa": "/population-trend?x=1"}}]'
            "');</script>"
        )
        rows = extract_rowdata(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Michael Jordan")

    def test_missing_marker_returns_empty(self):
        self.assertEqual(extract_rowdata("<html>nothing here</html>"), [])


class TestCompHygiene(unittest.TestCase):
    CARD = {"year": "2003", "set": "Topps Chrome", "card": "LeBron James",
            "number": "111", "parallel": "Base"}

    def test_grade_isolation(self):
        self.assertTrue(comps.title_matches(
            "2003-04 Topps Chrome LeBron James #111 RC PSA 10 GEM MINT", self.CARD, 10))
        self.assertFalse(comps.title_matches(
            "2003-04 Topps Chrome LeBron James #111 RC PSA 9", self.CARD, 10))
        self.assertFalse(comps.title_matches(
            "2003 Topps Chrome LeBron #111 PSA 10 + PSA 9 lot", self.CARD, 10))

    def test_rejects_wrong_card(self):
        self.assertFalse(comps.title_matches(
            "1996 Topps Chrome LeBron James #111 PSA 10", self.CARD, 10))
        self.assertFalse(comps.title_matches(
            "2003-04 Topps Chrome LeBron James #113 PSA 10", self.CARD, 10))

    def test_rejects_non_singles(self):
        for bad in ("REPRINT", "Custom Card", "Card lot of 10 -"):
            self.assertFalse(comps.title_matches(
                f"2003-04 Topps Chrome LeBron James #111 {bad} PSA 10", self.CARD, 10), bad)

    def test_trim_drops_outliers(self):
        self.assertEqual(comps._trimmed([10, 90, 100, 102, 105, 108, 110, 900]),
                         [90, 100, 102, 105, 108, 110])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestDiagnostics(unittest.TestCase):
    """A run that scores nothing must say which stage broke. The first real
    run produced an empty report whose only explanation was a guess about
    captchas; these lock in that each failure mode names itself."""

    def test_each_failure_mode_is_named(self):
        from scraper.scan import diagnose
        self.assertIn("captcha", diagnose({"attempted": 5, "captcha_blocked": 7}, False))
        self.assertIn("markup", diagnose({"attempted": 5, "no_listings": 10,
                                          "listings_seen": 0}, False))
        self.assertIn("too strict", diagnose({"attempted": 5, "listings_seen": 200,
                                              "all_filtered": 9}, False))
        self.assertIn("network", diagnose({"attempted": 5, "nav_failed": 4}, False))

    def test_silent_only_when_pricing_actually_worked(self):
        from scraper.scan import diagnose
        self.assertIsNone(diagnose({"attempted": 40}, True))

    def test_zero_lookups_is_never_silent(self):
        """Staying quiet here is what let a poisoned cache look like a genuine
        'no results' answer across repeated runs."""
        from scraper.scan import diagnose
        self.assertIsNotNone(diagnose({}, False))


class TestRelaxedMatching(unittest.TestCase):
    """Requiring the card number in the title threw away honest listings that
    simply omit it, which is a prime suspect for a run that priced nothing."""

    CARD = {"year": "1986", "set": "Fleer", "card": "Michael Jordan",
            "number": "57", "parallel": "Base"}

    def test_strict_pass_requires_the_number(self):
        t = "1986 Fleer Michael Jordan Rookie RC PSA 10"
        self.assertFalse(comps.title_matches(t, self.CARD, 10, require_number=True))

    def test_relaxed_pass_accepts_a_missing_number(self):
        t = "1986 Fleer Michael Jordan Rookie RC PSA 10"
        self.assertTrue(comps.title_matches(t, self.CARD, 10, require_number=False))

    def test_relaxed_pass_still_rejects_a_different_number(self):
        t = "1986 Fleer Michael Jordan #99 PSA 10"
        self.assertFalse(comps.title_matches(t, self.CARD, 10, require_number=False))

    def test_relaxed_pass_still_enforces_grade_and_year(self):
        self.assertFalse(comps.title_matches(
            "1986 Fleer Michael Jordan Rookie PSA 9", self.CARD, 10, require_number=False))
        self.assertFalse(comps.title_matches(
            "1992 Fleer Michael Jordan Rookie PSA 10", self.CARD, 10, require_number=False))


class TestCache(unittest.TestCase):
    """A failed lookup was being cached as a result, so re-runs replayed the
    failure for the length of the TTL without ever contacting eBay again."""

    def _card(self):
        return {"year": "1986", "set": "Fleer", "card": "Michael Jordan",
                "number": "57", "parallel": "Base"}

    def test_failed_lookup_is_not_cached(self):
        from scraper import cache as C
        cache = {}
        m = self._card()
        m.update({"p10_median": None, "p10_sales": 0, "p9_median": None, "p9_sales": 0})
        self.assertFalse(C.put(cache, m))
        self.assertEqual(cache, {})
        self.assertFalse(C.get(cache, m, 7))

    def test_half_priced_lookup_is_not_cached(self):
        from scraper import cache as C
        cache = {}
        m = self._card()
        m.update({"p10_median": 500.0, "p10_sales": 6, "p9_median": None, "p9_sales": 0})
        self.assertFalse(C.put(cache, m), "a ratio needs both grades to be useful")

    def test_successful_lookup_round_trips(self):
        from scraper import cache as C
        cache = {}
        trend = {"older_median": 500.0, "recent_median": 480.0,
                 "older_n": 6, "recent_n": 3, "change_pct": -4.0}
        m = self._card()
        m.update({"p10_median": 500.0, "p10_sales": 8, "p10_spread": 0.3,
                  "p10_trend": trend,
                  "p9_median": 120.0, "p9_sales": 9, "p9_spread": 0.2,
                  "p9_trend": dict(trend)})
        self.assertTrue(C.put(cache, m))
        fresh = self._card()
        self.assertTrue(C.get(cache, fresh, 7))
        self.assertEqual(fresh["p10_median"], 500.0)
        self.assertEqual(fresh["p9_median"], 120.0)

    def test_all_cached_run_is_reported_not_silent(self):
        from scraper.scan import diagnose
        msg = diagnose({}, False)
        self.assertIsNotNone(msg)
        self.assertIn("cache", msg)


class TestBadCompRejection(unittest.TestCase):
    """Everything here is drawn from the first run that returned real numbers,
    which listed a $550 Larry Bird PSA 10 as $37,000 of value."""

    def test_ten_cheaper_than_nine_is_rejected(self):
        from scraper.value import priceable
        # 1981 Topps Larry Bird: PSA 9 $1,180, PSA 10 $550. A 10 below its own 9
        # means the two searches found different cards.
        m = {"p9_median": 1180.0, "p10_median": 550.0}
        self.assertFalse(priceable(m))
        self.assertIn("different cards", m["rejected"])

    def test_normal_premium_is_kept(self):
        from scraper.value import priceable
        self.assertTrue(priceable({"p9_median": 120.0, "p10_median": 600.0}))

    def test_prediction_never_extrapolates_past_the_data(self):
        """A 0.49% gem rate sits far outside any real pool; an unclamped fit
        predicted a 31x premium there and invented $37k of fair value."""
        rng = random.Random(21)
        cards = [m for c in simulate_universe(120) if (m := analyze(c, FILTERS))]
        for m in cards:
            simulate_prices(m, rng)
        model = fit_cohort(cards)
        rates = [m["gem_rate_pct"] for m in cards]
        widest = model["predict"](min(rates))
        absurd = model["predict"](0.001)   # far beyond anything observed
        self.assertLessEqual(absurd, widest + 1e-9,
                             "prediction must be pinned at the observed edge")

    def test_trivial_dollar_edges_are_not_findings(self):
        """A $5 gap on a $15 card is noise, not an opportunity."""
        rng = random.Random(4)
        cards = [m for c in simulate_universe(60) if (m := analyze(c, FILTERS))]
        for m in cards:
            simulate_prices(m, rng)
        penny = cards[0]
        penny.update({"p9_median": 8.0, "p10_median": 15.0, "p9_sales": 50,
                      "p10_sales": 53, "p9_spread": 0.3, "p10_spread": 0.3})
        model = fit_cohort(cards)
        for m in cards:
            score_card(m, model)
        ranked = rank(cards, WEIGHTS, 3, min_edge_usd=25.0)
        self.assertNotIn(id(penny), [id(m) for m in ranked])


class TestPaddingAndOutliers(unittest.TestCase):
    """eBay pads a thin result set with loosely-related listings under a
    "Results matching fewer words" heading. Counting those priced a 1990 Hoops
    common at $569 while a heavily-traded Kobe priced correctly — the giveaway
    being that only obscure cards were wrong."""

    def test_mad_filter_survives_heavy_high_side_contamination(self):
        real = [1.5, 2, 2, 2.5, 3, 2, 2.25, 3.5, 2, 2.75, 1.75, 2.5]
        contaminated = real + [450, 569, 720, 380]
        clean = statistics.median(comps._trimmed(real))
        got = statistics.median(comps._trimmed(contaminated))
        self.assertAlmostEqual(got, clean, delta=0.75,
                               msg="wrong-card listings must not move the median")

    def test_identical_prices_with_one_outlier(self):
        prices = [20.0] * 8 + [1500.0]
        self.assertLess(max(comps._trimmed(prices)), 100.0)

    def test_small_sets_are_left_alone(self):
        self.assertEqual(comps._trimmed([10.0, 12.0, 11.0]), [10.0, 11.0, 12.0])

    def test_padding_markers_cover_ebays_wording(self):
        for phrase in ("Results matching fewer words", "Shop on eBay"):
            self.assertTrue(
                any(m in phrase.lower() for m in comps._PADDING_MARKERS),
                f"{phrase!r} must be recognised as the start of padding")


class TestParallelSeparation(unittest.TestCase):
    """Titles here are verbatim from a real run's audit. A base PSA 9 Cooper
    Flagg trades near $46; its Refractor near $370. Counting both as one card
    inflated every median the scraper produced."""

    FLAGG = {"year": "2025", "set": "Topps Chrome", "card": "Cooper Flagg",
             "number": "251", "parallel": "Base"}
    MORANT = {"year": "2019", "set": "Panini Chronicles", "card": "Ja Morant",
              "number": "165", "parallel": "Base"}

    def test_base_search_rejects_parallels(self):
        for t in ("COOPER FLAGG 2025-26 TOPPS CHROME REFRACTOR ROOKIE RC #251 MAVERICKS PSA 9",
                  "2025 Topps Chrome Cooper Flagg X-Fractor RC #251 PSA 9 MINT Rookie",
                  "2025-26 Topps Chrome - Cooper Flagg #251 RayWave Refractor (RC) PSA 9"):
            self.assertIsNotNone(comps.reject_reason(t, self.FLAGG, 9), t[:50])

    def test_base_search_keeps_base(self):
        for t in ("2025-26 Topps Chrome Cooper Flagg RC Rookie #251 Mavericks PSA 9",
                  "2025 TOPPS CHROME #251 COOPER FLAGG ROOKIE RC PSA 9",
                  "2025-26 Topps Chrome - Cooper Flagg #251 (RC) PSA 9"):
            self.assertIsNone(comps.reject_reason(t, self.FLAGG, 9), t[:50])

    def test_colour_parallels_rejected_for_base(self):
        for t in ("2019-20 Panini Chronicles - Luminance Pink #165 Ja Morant RC PSA 10",
                  "Ja Morant 2019 Panini Chronicles Luminance 165 Rookie Bronze PSA 10"):
            self.assertIsNotNone(comps.reject_reason(t, self.MORANT, 10), t[:50])

    def test_colour_in_the_player_name_is_not_a_parallel(self):
        """Draymond Green's base card must not be read as a green parallel."""
        card = {"year": "2012", "set": "Topps Chrome", "card": "Draymond Green",
                "number": "22", "parallel": "Base"}
        self.assertIsNone(comps.reject_reason(
            "2012-13 Topps Chrome Draymond Green #22 RC PSA 10", card, 10))

    def test_serial_numbered_cards_are_parallels(self):
        self.assertIsNotNone(comps.reject_reason(
            "2025 Topps Chrome Cooper Flagg #251 PSA 9 /99", self.FLAGG, 9))

    def test_requesting_a_parallel_requires_it(self):
        card = dict(self.FLAGG, parallel="Refractor")
        self.assertIsNone(comps.reject_reason(
            "2025 Topps Chrome Cooper Flagg Refractor #251 PSA 9", card, 9))
        self.assertIsNotNone(comps.reject_reason(
            "2025 Topps Chrome Cooper Flagg #251 PSA 9", card, 9))

    def test_bare_card_number_is_accepted(self):
        """'...Grizzlies 165 PSA 9' was being discarded for lacking a '#'."""
        self.assertIsNone(comps.reject_reason(
            "2019-20 Panini Chronicles 165 Ja Morant RC Luminance The Dunk Rookie PSA 10",
            self.MORANT, 10))

    def test_missing_year_is_tolerated_but_wrong_year_is_not(self):
        self.assertIsNone(comps.reject_reason(
            "Panini Chronicles Luminance Ja Morant Rookie #165 Grizzlies PSA 10",
            self.MORANT, 10))
        self.assertIsNotNone(comps.reject_reason(
            "2021-22 Panini Chronicles Ja Morant #165 PSA 10", self.MORANT, 10))


class TestPredictionBand(unittest.TestCase):
    """A single 'worth $919' figure invited the obvious objection that nothing
    sells for $919. The fit supports a range, and only cards below it are
    findings."""

    def _pool(self, n=80, seed=13):
        rng = random.Random(seed)
        cards = [m for c in simulate_universe(n, seed=seed) if (m := analyze(c, FILTERS))]
        for m in cards:
            simulate_prices(m, rng)
        return cards

    def test_band_brackets_the_point_estimate(self):
        cards = self._pool()
        model = fit_cohort(cards)
        for m in cards:
            score_card(m, model)
        for m in cards:
            self.assertLessEqual(m["fair_low"], m["fair_p10"] + 1e-6)
            self.assertGreaterEqual(m["fair_high"], m["fair_p10"] - 1e-6)

    def test_card_inside_the_band_is_not_a_finding(self):
        cards = self._pool()
        model = fit_cohort(cards)
        for m in cards:
            score_card(m, model)
        inside = [m for m in cards if not m["below_band"]]
        self.assertTrue(inside, "fixture should contain normally-priced cards")
        ranked = rank(cards, WEIGHTS, 3)
        for m in inside:
            self.assertNotIn(id(m), [id(r) for r in ranked])

    def test_tiny_cohort_falls_back_instead_of_fitting_noise(self):
        """Eight cards cannot support a three-parameter fit."""
        cards = self._pool(n=12, seed=5)[:8]
        model = fit_cohort(cards)
        self.assertEqual(model["kind"], "flat",
                         "a cohort this small must not pretend to a fitted curve")


class TestCacheKeepsTrends(unittest.TestCase):
    """The ranking reads the trend fields. Leaving them out of the cache meant
    a cached card returned with prices, no history, and no way to rank — a run
    reporting 53 cards priced and 0 ranked."""

    def _card(self):
        return {"year": "1996", "set": "Skybox Premium", "card": "Kobe Bryant",
                "number": "55", "parallel": "Base"}

    def test_trend_survives_a_round_trip(self):
        from scraper import cache as C
        trend = {"older_median": 600.0, "recent_median": 430.0,
                 "older_n": 6, "recent_n": 3, "change_pct": -28.3}
        m = self._card()
        m.update({"p10_median": 430.0, "p10_sales": 9, "p10_trend": trend,
                  "p9_median": 197.0, "p9_sales": 12, "p9_trend": dict(trend, change_pct=1.0)})
        cache = {}
        self.assertTrue(C.put(cache, m))
        back = self._card()
        self.assertTrue(C.get(cache, back, 7))
        self.assertEqual(back["p10_trend"]["change_pct"], -28.3)

    def test_entry_without_a_trend_is_not_reused(self):
        from scraper import cache as C
        m = self._card()
        m.update({"p10_median": 430.0, "p9_median": 197.0})   # priced, no history
        cache = {}
        self.assertFalse(C.put(cache, m))
