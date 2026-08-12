# GemRate Scanner

Finds cards with **high population + low gem rate + upward price momentum** — the ones the market is actively repricing but hasn't finished repricing.

## How it works

Two-stage funnel to keep request volume polite:

1. **Stage 1 (cheap):** Scrapes set checklist pages for every card's population and gem rate. Filters immediately.
2. **Stage 2 (targeted):** Only cards passing the pop/gem filters get their card page scraped for recent sale comps.
3. **Rank:** Momentum (50%) + gem-rate tightness scaled by population (30%) + sales activity (20%). Weights are in `config.json`.

Runs are **checkpointed** — each scheduled run picks up where the last one stopped, so the full database gets covered over successive runs without any single run taking hours.

## Setup

1. Push this repo to GitHub.
2. Go to **Actions → GemRate Scan → Run workflow** — pick your sport from the dropdown.
3. Results land in `results/` as CSV, JSON, and a markdown summary table, committed back to the repo.

The weekly scheduled run (Monday) uses `default_sport` in `config.json`.

## Tuning

Everything lives in `config.json`:

- `filters` — min pop (300), max gem rate (20%), min price ($20), min recent sales (3 in 60 days)
- `ranking_weights` — momentum / tightness / activity
- `scan_limits` — sets per run, card pages per run, delay between requests, max results

Too many results? Tighten filters. Too few? Loosen them.

## First-run reality check

GemRate is a JavaScript app and doesn't document its internal endpoints. The scraper handles this two ways:

- **JSON interception** (preferred): captures the structured API responses the site loads and parses them with tolerant key-matching.
- **DOM fallback**: parses rendered tables if no usable JSON appears.

Expect the first run to need a selector or key-name adjustment once you see real responses — that's normal for any scraper against an undocumented site. Run locally first with `headless=False` in `gemrate.py` to watch it work:

```bash
pip install -r requirements.txt
playwright install chromium
python -m scraper.scan --sport basketball
```

## Notes

- Delay between requests is 1.5s by default. Don't lower it aggressively — this relies on their free tier staying scrape-tolerant.
- The free tier caps comps at ~5 recent sales, so momentum is a short-window signal. Treat results as a shortlist to verify manually (eBay solds), not a buy list.
