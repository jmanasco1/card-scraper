# GemRate Scanner

Finds cards with **high population + low gem rate** — heavily graded cards that are hard to pull in a gem grade.

> **Note:** GemRate rebuilt their site and no longer publishes recent sale prices (those moved to their CardLadder integration). The original "price momentum" signal isn't available from GemRate anymore, so this tool ranks on population + gem-rate scarcity. Treat the output as a shortlist to verify manually (eBay / CardLadder solds), not a buy list.

## How it works

1. Load GemRate's **Top Cards** report `/top-cards?grader=psa&category=<sport>` in a real browser (required — the site is behind Cloudflare) and pull the dataset embedded in the page (`var RowData = JSON.parse(...)`) — the top ~100 most-graded cards, each with population + gem count.
2. **Filter:** keep cards with population ≥ min and gem rate ≤ max, pre-rank by scarcity.
3. **Comps:** each result gets a one-click **eBay "sold listings" link** so you can check recent prices by hand. Optionally (`--comps`) the tool auto-scrapes eBay PSA-10 solds and computes price momentum — but eBay throws captchas at automation, so that mode needs you to solve them in the visible browser. For hands-off automated momentum, use CardLadder (paid) — every GemRate card maps to it.
4. **Rank:** price momentum (when comps are available) + gem-rate scarcity + sales activity. Weights are in `config.json`.

Results land in `results/` as CSV, JSON, and a markdown summary table (with the eBay links).

### On sale prices / "undervalued"

GemRate removed sale prices from its site (moved to its CardLadder integration), so the price signal has to come from elsewhere. eBay is the free source but actively blocks scraping with captchas — this is the genuinely hard part of the domain and why paid tools exist. The default run therefore hands you eBay sold-search links to eyeball; `--comps` attempts automation with manual captcha-solving.

## Setup

1. Go to **Actions → GemRate Scan → Run workflow** — pick your sport from the dropdown.
2. **Important:** GitHub's servers are blocked by GemRate's Cloudflare protection (see below), so the Actions run needs a `GEMRATE_PROXY` secret. To run without a proxy, run it locally instead (see "Running locally").
3. Results are committed back to the repo under `results/`.

The weekly scheduled run (Monday) uses `default_sport` in `config.json`.

## Running locally

A normal home internet connection clears Cloudflare on its own, so local runs need no proxy:

```bash
pip install -r requirements.txt
playwright install chrome
# Windows: set GEMRATE_HEADFUL=1     (macOS/Linux: export GEMRATE_HEADFUL=1)
python -m scraper.scan --sport basketball --debug
# add --comps to auto-scrape eBay momentum (you'll solve captchas in the browser)
```

Then look in `results/` for `latest_basketball.csv` and `summary_basketball.md`. Each row has an eBay sold-listings link — click it to see recent PSA-10 sale prices for that card.

## Tuning

Everything lives in `config.json`:

- `filters` — `min_total_population` (300) and `max_gem_rate_pct` (20). (`min_price_usd` / recent-sales filters are legacy and unused now that sale prices aren't available.)
- `ranking_weights` — `gem_rate_tightness` and `sales_activity` (used as a volume weight); `price_momentum` is folded into tightness for backward compatibility.
- `scan_limits` — `request_delay_seconds`, `max_results` (per-set / per-card limits are legacy no-ops).

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
