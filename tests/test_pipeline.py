"""
Offline end-to-end test.

The live path can't be exercised from CI — Cloudflare blocks datacenter IPs —
so the pipeline is tested against a simulated card universe and a simulated
eBay. The point is to prove the scoring chain surfaces genuinely underpriced
cards and doesn't just re-rank by fame the way the old version did.
"""

import math
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

    def test_surfaces_planted_bargains_not_famous_cards(self):
        rng = random.Random(5)
        cards = [m for c in simulate_universe() if (m := analyze(c, FILTERS))]
        for m in cards:
            simulate_prices(m, rng)

        # Plant three cards whose 10 trades at ~45% of fair value.
        bargains = cards[:3]
        for m in bargains:
            simulate_prices(m, rng, mispricing=0.45, sales=15)
        # And one hugely popular card priced exactly fairly — the sort of card
        # the old pop-based ranking always put on top.
        famous = cards[10]
        famous["total_pop"] = 19000
        simulate_prices(famous, rng, mispricing=1.0, sales=40)

        model = fit_cohort(cards)
        for m in cards:
            score_card(m, model)
        ranked = rank(cards, WEIGHTS, FILTERS["min_sales_per_grade"])

        # The three genuinely underpriced cards must take the top three slots,
        # ahead of everything the noise throws up.
        top3 = [id(m) for m in ranked[:3]]
        for m in bargains:
            self.assertIn(id(m), top3, "planted bargain missed the top 3")
        # The famous card is priced fairly. Its rank among the noise below the
        # cliff is not meaningful, but its *score* must be an order of
        # magnitude off a real bargain — population alone earns nothing.
        self.assertNotIn(id(famous), top3)
        weakest_bargain = min(m["score"] for m in bargains)
        self.assertLess(famous["score"], weakest_bargain / 5,
                        "fairly-priced high-pop card scored near a real bargain")

        # There should be a visible cliff between the real finds and the noise,
        # which is what makes the output actionable rather than a ranked blur.
        self.assertGreater(ranked[2]["score"], ranked[3]["score"] * 3,
                           "no separation between genuine finds and noise")

    def test_score_separates_cheap_from_rich(self):
        """The old scan's fatal flaw was a score that was 70% constant, so
        everything landed in a 0.04 band. The new score must put real daylight
        between an underpriced card and an overpriced one."""
        rng = random.Random(9)
        cards = [m for c in simulate_universe(60) if (m := analyze(c, FILTERS))]
        for m in cards:
            simulate_prices(m, rng)
        cheap, rich = cards[0], cards[1]
        simulate_prices(cheap, rng, mispricing=0.5, sales=15)
        simulate_prices(rich, rng, mispricing=2.0, sales=15)

        model = fit_cohort(cards)
        for m in cards:
            score_card(m, model)
        ranked = rank(cards, WEIGHTS, FILTERS["min_sales_per_grade"])

        self.assertIs(ranked[0], cheap, "underpriced card should rank first")
        # Overpriced cards are no longer listed at all: this is a list of things
        # to buy, and a card the model calls expensive is not a finding.
        self.assertNotIn(id(rich), [id(m) for m in ranked],
                         "an overpriced card must not appear on a buy list")
        self.assertGreater(cheap["score"], 50.0)

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
        m = self._card()
        m.update({"p10_median": 500.0, "p10_sales": 8, "p10_spread": 0.3,
                  "p9_median": 120.0, "p9_sales": 9, "p9_spread": 0.2})
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
