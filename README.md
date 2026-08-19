> **This repo now holds two independent tools.**
> - **eBay Graded Card Scanner** (`ebay_scanner/`) — continuously records newly-listed
>   graded sports cards in a price band. Data collection only. **See
>   [eBay Graded Card Scanner](#ebay-graded-card-scanner) below.**
> - **GemRate Scanner** (`scraper/`) — the original population/gem-rate tool, documented first.

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


---

# eBay Graded Card Scanner

Records **newly-listed graded sports cards between $75 and $400** from the eBay
Browse API, once every 15 minutes, appending to a git-friendly JSONL log.

**This is data collection only.** There is deliberately no scoring, no
alerting, and no valuation logic. The point is to build a continuous record
first so you can eyeball how much mispricing actually exists before deciding
what, if anything, is worth scoring.

## What each run does

1. Mints (or reuses) an eBay **application** access token via the
   client-credentials OAuth flow.
2. Reads the remaining daily Browse quota from the Developer Analytics
   `getRateLimits` endpoint. **If fewer than 500 calls remain, the run logs a
   warning and aborts before searching.**
3. Verifies the configured category IDs against the live **Taxonomy API**
   rather than trusting a hardcoded guess. The verification is cached to
   `data/categories.json` for 7 days, so it costs no calls on most runs.
4. Searches `item_summary/search`, sorted `newlyListed`, up to 3 pages of 200.
5. **Dedupes on `itemId`** against everything already stored. Most runs are
   mostly repeats; only genuinely new IDs go further.
6. Enriches the new IDs with bulk item detail (20 per call) to pull
   `localizedAspects` — grader, grade, cert number and friends.
7. Appends to `data/YYYY-MM-DD.jsonl` and commits, **skipping the commit
   entirely when nothing new was found.**
8. Writes a GitHub Step Summary: new listings, total stored, calls used, quota
   remaining, the 10 cheapest new items, and per-field coverage.

## Setup

Add two repository secrets under **Settings → Secrets and variables → Actions**:

| Secret | eBay developer console field |
|---|---|
| `EBAY_CLIENT_ID` | **App ID (Client ID)** |
| `EBAY_CLIENT_SECRET` | **Cert ID (Client Secret)** |

Both must come from a **Production** keyset, not Sandbox — the sandbox
environment has essentially no real graded-card inventory, so a sandbox-keyed
run goes green while collecting nothing useful.

Then: **Actions → eBay Graded Card Scan → Run workflow**. Choose `probe` to dump
live API response shapes, or `collect` for a real run.

## Scheduling reality

The workflow is set to `*/15 * * * *`, but **GitHub does not honor that
precisely.** Scheduled workflows are queued on a best-effort basis and are
delayed — sometimes by many minutes — when the Actions fleet is under load. High
frequency crons are the first to be dropped, and runs during peak hours are
routinely skipped rather than merely postponed. Treat 15 minutes as a ceiling on
frequency, not a guarantee. Because listings are deduped by `itemId` and sorted
`newlyListed`, a missed run is self-healing as long as fewer than 600 matching
listings appeared in the gap.

Note also that **`schedule` only fires from the repository's default branch.**
On any other branch the cron is inert and only `workflow_dispatch` works.

### Actions minutes

This repo is **private**, so Actions minutes are metered (2,000/month on the
free tier). A 15-minute cron is ~2,880 runs/month; even at one minute per run
that overruns the free allowance. Options: make the repo public (unlimited free
minutes), accept the billing, or widen the cron to `*/30` or hourly.

## Rate limits

The Browse API allows **5,000 calls/day** at the application level. A typical
run costs about 5 calls plus one per 20 new listings, so the 15-minute schedule
lands well inside the budget. Quota is read and logged at the start of every
run, and the run aborts below 500 remaining.

## Stored fields

Each JSONL line holds `itemId`, `title`, `price`, `currency`, `itemWebUrl`,
`itemCreationDate`, seller username / feedback score / feedback percentage,
`condition`, image URL, and the **full raw aspects blob**. These aspects are
also lifted into their own columns when present:

`grader` · `grade` · `cert_number` · `season` · `set_name` · `player` · `card_number`

Aspect names vary between listings, so each column resolves from a list of
candidate names (`Professional Grader`, `Grader`, `Grading Company`, …) and
records which name actually matched in a companion `*_aspect` field. The raw
blob is kept verbatim precisely so you can measure how often fields are
missing — the Step Summary reports per-field coverage every run.

## Ad-hoc querying

```bash
python -m ebay_scanner.load_sqlite            # -> cards.db
python -m ebay_scanner.load_sqlite --rebuild  # drop and reload
```

Then query normally:

```sql
SELECT grader, grade, COUNT(*), ROUND(AVG(price), 2)
FROM listings
WHERE grader IS NOT NULL
GROUP BY grader, grade
ORDER BY COUNT(*) DESC;
```

`cards.db` is gitignored — it is a derived artifact, rebuildable from the JSONL
at any time.

## Configuration

`ebay_scanner/config.json`:

| Key | Default | Meaning |
|---|---|---|
| `category_ids` | `["261328"]` | Verified against the Taxonomy API at runtime |
| `price_min` / `price_max` | `75` / `400` | USD band |
| `buying_options` | `["FIXED_PRICE"]` | Excludes auctions |
| `sort` | `newlyListed` | |
| `limit` / `max_pages` | `200` / `3` | Up to 600 listings per run |
| `quota_abort_threshold` | `500` | Abort below this many remaining calls |
| `aspect_filter` | `{"Graded": ["Yes"]}` | Narrows to graded cards |
| `enrich_with_get_items` | `true` | Needed for aspects; costs 1 call per 20 new items |

## Running locally

```bash
pip install -r ebay_scanner/requirements.txt
export EBAY_CLIENT_ID=...  EBAY_CLIENT_SECRET=...
python -m ebay_scanner.probe      # dump live response shapes
python -m ebay_scanner.collect    # real run
```
