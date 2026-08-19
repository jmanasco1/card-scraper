# GemRate Value Scanner

Finds PSA 10s that trade **below what their scarcity is worth** — cards where
the market isn't charging much for a gem that's genuinely hard to pull.

## What changed, and why

The first version of this tool ranked cards on population + gem rate. That
sounds like a scarcity screen, but it isn't one:

- **Population is a fame metric.** The most-graded cards are the most famous
  ones, because everyone owns them and everyone grades them.
- **A low gem rate mostly means "old."** 1980s cardboard has bad centering and
  print quality, so vintage always gems under 2%.
- The scoring formula multiplied by `log10(population)`, so it actively
  rewarded being famous.

The result was a list of Michael Jordan and Larry Bird rookies — the most
watched, most liquid, most efficiently priced cards in the hobby. Exactly where
inefficiency *isn't*. Worse, no card in that run had any price attached, so 70%
of the score was a constant and the ranking was close to noise.

This version measures something that can actually be mispriced.

## The signal: the grade gap

For any card, the market pays a premium for the PSA 10 over the PSA 9:

```
gap = median(PSA 10 solds) / median(PSA 9 solds)
```

That premium *should* scale with how hard the 10 is to get. A card that gems 2%
of the time has a genuinely scarce 10 and should trade at a fat multiple; a card
that gems 40% of the time should not.

Rather than assume the relationship, the scanner fits it from the cards it
priced this run:

```
log(gap) = a + b · log(100 / gem_rate)
```

then compares each card to the curve:

```
value_ratio = actual_gap / expected_gap
```

Below 1 means the 10 trades under what this pool pays for that level of
scarcity — the 10 is cheap for how hard it is. That's the finding.

Two properties matter:

- **It's cohort-relative.** Every card is judged against how this particular
  pool prices scarcity, so nothing biases it toward old cards.
- **It's price-anchored.** A card cannot rank without real sold prices on both
  sides, so the score can never collapse into a constant the way the old one
  did.

The curve is fitted twice — once on everything, then again after dropping the
worst-fitting 20% — so the outliers being hunted don't set the standard they're
judged against.

## Running it

**Run this locally.** It cannot work on GitHub Actions: Cloudflare blocks
datacenter IPs from gemrate.com, and eBay serves captchas to automation that a
human has to clear. Both are fine on a home connection with a visible browser.

On macOS or Linux, `run.sh` does the whole setup and then runs the scan:

```bash
./run.sh                      # basketball, prices 40 cards
./run.sh --limit 10           # quick trial run
./run.sh --sport baseball
```

It creates a virtualenv, installs dependencies, and reuses Google Chrome if you
already have it. To drive it by hand instead:

```bash
pip install -r requirements.txt
playwright install chrome

# Windows:      set GEMRATE_HEADFUL=1
# macOS/Linux:  export GEMRATE_HEADFUL=1
python -m scraper.scan --sport basketball --debug
```

The run will pause and prompt you when eBay throws a captcha — solve it in the
Chrome window and it continues. Prices are cached in
`results/.price_cache.json` for a week, so a re-run with tweaked filters
doesn't make you solve them all again.

When it finishes, `run.sh` opens **`results/report_<sport>.html`** in your
browser — a sortable table with both eBay searches one click from every row.
The same data is written as CSV (for spreadsheets) and markdown (for GitHub).

### Options

| Flag | Meaning |
|------|---------|
| `--sport` | basketball, baseball, football, hockey, soccer, tcg |
| `--universe wide` | (default) sweep per-year reports to reach the long tail |
| `--universe top` | the old all-time top-cards pool — a fame contest, kept for comparison |
| `--limit N` | how many cards to price on eBay this run (default 40) |
| `--debug` | log page status, row counts and per-card comp counts |

## Reading the output

| Column | Meaning |
|--------|---------|
| `gap` | actual PSA 10 / PSA 9 price ratio |
| `fair` | what this cohort pays for that gem rate |
| `Disc.` | how far below fair the 10 trades — positive is cheap |
| `Conf.` | 0–1, how much comp depth backs the number |
| `Edge` | discount × confidence, plus a small liquidity kicker |

**Verify every hit by hand.** Click both eBay links and check the comps are
really the same card. Thin comps are the usual reason a discount turns out to
be a mirage, which is what `Conf.` is there to warn you about. A low `R²` in the
run header means the pool prices scarcity inconsistently and the whole set of
discounts is noisy — price more cards with `--limit` before trusting it.

## Known limits

- **The universe may still be capped.** GemRate's endpoints are undocumented.
  The scanner asks `/top-cards` for per-year slices to reach beyond the ~100
  all-time blue chips, and it *measures at runtime* whether the site honors
  that — if the per-year request comes back identical to the unfiltered one, it
  says so and stops rather than burning page loads. If that happens, run
  `python -m scraper.recon` and the real set/year URLs can be wired in.
- **eBay title matching is strict but not perfect.** Comps must carry the exact
  grade, the right year and the right card number, and lots/reprints/customs
  are dropped. Some mismatches will still get through, which is what the
  dispersion penalty in the confidence score is guarding against.
- **This is a shortlist, not a buy list.** It tells you where to look, not what
  to buy.
- **No pop-velocity signal yet.** Gem rate is a snapshot. A card whose PSA 10
  population is climbing fast has expanding supply and should be getting
  cheaper — the natural next signal, and it needs snapshots accumulated over
  time.

## Tests

```bash
python -m unittest discover -s tests -v
```

The live path can't be exercised in CI, so the pipeline is tested against a
simulated card universe and a simulated eBay: the tests plant underpriced cards
and assert they surface above a fairly-priced high-population card, that the fit
recovers the true scarcity slope, and that eBay title matching rejects wrong
grades, wrong years, wrong card numbers, lots and reprints.

## Debug tooling

- `scraper/probe.py` — tests whether the current egress can reach gemrate.com
  via TLS impersonation across five browser profiles.
- `scraper/recon.py` — loads pages in a real browser and dumps titles, links and
  every captured JSON/XHR endpoint, so the parsers can be kept in sync.
