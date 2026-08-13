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

Run locally to watch it work (a residential IP passes Cloudflare automatically):

```bash
pip install -r requirements.txt
playwright install chrome chromium
python -m scraper.scan --sport basketball --max-sets 2 --debug
```

## ⚠️ Cloudflare + GitHub Actions: a proxy is required

gemrate.com sits behind **Cloudflare**, which serves a managed "Just a moment…" challenge. On a normal home/residential connection a real browser clears it automatically. **GitHub-hosted runners use Azure datacenter IPs, which Cloudflare blocks outright** — the challenge never clears no matter the browser or fingerprint.

This was verified end-to-end: real Google Chrome (headful under xvfb, with anti-fingerprint patches) and `curl_cffi` TLS-impersonation across five browser profiles all returned `403 Just a moment...` from the runner. The block is IP-reputation based, not a fingerprint problem, so no client-side trick fixes it.

**To run the scan on GitHub Actions you must route it through a residential/mobile egress:**

1. Get a residential or mobile proxy (Bright Data, Oxylabs, IPRoyal, …) or a scraping API that bundles residential IPs + Cloudflare solving (ScraperAPI, ZenRows, Scrapfly).
2. Add the endpoint as a repo secret named **`GEMRATE_PROXY`** (Settings → Secrets and variables → Actions), format `http://user:pass@host:port`.
3. Re-run the workflow. The scraper (`launch_browser`) and the probe both honor `GEMRATE_PROXY` automatically.

Alternatives that avoid a proxy entirely:
- Run the scan on a **self-hosted runner** on a residential connection, or
- Run `python -m scraper.scan` on your **own machine** and commit `results/` yourself.

### Debug tooling

Run the workflow with **debug = true** (or pass `--debug` locally) to get:
- `scraper/probe.py` — tests whether the current egress can reach the site via `curl_cffi` TLS impersonation (prints status per browser profile).
- `scraper/recon.py` — loads pages in the real browser and dumps titles, links, and every captured JSON/XHR endpoint, so the parsers can be kept in sync once traffic actually gets through.

## Notes

- Delay between requests is 1.5s by default. Don't lower it aggressively — this relies on their free tier staying scrape-tolerant.
- The free tier caps comps at ~5 recent sales, so momentum is a short-window signal. Treat results as a shortlist to verify manually (eBay solds), not a buy list.
