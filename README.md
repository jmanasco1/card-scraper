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

**eBay disables production keysets** until the application handles marketplace
account-deletion notifications. A compliant endpoint lives in
[`ebay-deletion-endpoint/`](ebay-deletion-endpoint/) — one Vercel serverless
function, no server or domain required. Deploy it and register its URL in the
eBay developer console, or the keyset stays disabled and every call returns
`invalid_client`.

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

### Why the graded aspect filter stays

The `aspect_filter Graded:{Yes}` narrows the search to listings whose seller
populated eBay's Graded aspect, which raises a fair worry: are real graded
cards being dropped because a seller skipped the field? Measured against the
live API rather than argued:

| Filter | Matches | Share of unfiltered |
|---|---|---|
| none | 2,753,577 | 100% |
| `aspect Graded:{Yes}` | 1,377,496 | 50.0% |
| `conditionIds:{2750}` | 1,333,063 | 48.4% |
| both | 1,324,929 | 48.1% |

The aspect filter is the **widest** graded filter available — wider than eBay's
own condition field. To measure what it misses, 500 condition-ungraded listings
were sampled and their titles scanned for grader names and grades, correcting
for the 0.43% of that set which does carry the aspect:

- **1.26%** of titles mention a grader at all
- **0.13%** carry a grader *and* a numeric grade

The gap between those two is the point: a title saying "PSA" is usually
`PSA-ready`, `PSA/DNA auto` or `PSA 10 candidate`, not a slab. Of 7 token hits
in 500, one was a real graded card. So the filter misses roughly 1 graded card
in 750, and dropping it would double the stored volume with raw cards to
recover that. It stays.

`has_grading_aspects` was considered and **not** added: it reads
`localizedAspects`, which needs `getItems`, which returns 403 here. With the
aspect filter on it would be `true` for every row by construction; with it off,
`false` for every row. Either way a constant, not a measurement. `conditionId`
is stored on every row instead, and is what made the test above possible.

Reproduce any of this with `mode: probe-filters` or `mode: probe-neither` in the
workflow dispatch.

### Why the price floor is $20, not $75

The $75 floor was an *acting* threshold applied at *collection* time, which
censors the data. For any card whose market value sits under $75, the only
listings that clear the filter are the overpriced ones — so any reference value
later computed from that sample is biased upward by construction, and no amount
of post-processing recovers the listings that were never recorded.

Widening to $20 costs less than expected. Measured live:

| Band | Matching listings | New listings/min |
|---|---|---|
| `[75..400]` | 1,377,693 | 10.6 |
| `[20..400]` | 3,054,089 | 20.0 |

Inventory grows 2.2x and arrival rate 1.9x — not the ~3x assumed. At 20
listings/min, a 15-minute sweep needs ~301 listings (2 pages); the configured 6
pages absorbs ~60 minutes of arrivals, which matters because GitHub defers
`*/15` schedules under load.

Filtering to an acting band stays available at query time and always will.

### Coverage gap detection

Page counts alone cannot tell you whether listings were missed. Each run
therefore compares the **oldest listing it fetched** against the **newest
listing already stored**. If the oldest fetched is newer, listings were created
and pushed beyond the pagination window between runs — a real, unbackfillable
gap — and the run emits a workflow warning naming the page count and cap state.
The Step Summary reports pages fetched, how far back the run reached, and
whether coverage is complete.

## Rate limits

The Browse API allows **5,000 calls/day** at the application level. A typical
run costs about 5 calls plus one per 20 new listings, so the 15-minute schedule
lands well inside the budget. Quota is read and logged at the start of every
run, and the run aborts below 500 remaining.

## Known limits, verified against the live API

**Aspect columns are empty.** `localizedAspects` — grader, grade, certification
number, set, player, card number — exists only on Browse's *item detail*
endpoints, and those return `403 Insufficient permissions` without eBay's **Buy
API access grant**, which is a separate application from the account-deletion
compliance step. Search itself works fine. The collector detects the 403, logs a
warning, and stores every field search does return rather than failing the run;
the step summary says explicitly that empty aspect columns mean missing access,
not missing listing data. If the grant is obtained later, enrichment starts
working with no code change.

Two things soften this: `condition` comes through as `Graded` on search, so
graded-ness is captured regardless, and titles are dense with grader and grade
("... PSA 10", "... BGS 9.5") if you want to parse them.

**`getRateLimits` does not resolve.** The Developer Analytics endpoint returns
404 on every documented host. The client tries each candidate and, finding none,
logs a warning and proceeds without the quota guard. Actual usage is roughly 5
calls per run against a 5,000/day ceiling, so the headroom is large — but the
guard is currently advisory only.

**`buyingOptions:{FIXED_PRICE}` is inclusive.** eBay returns listings where
fixed price is *one of* the options, so ~4% of rows also carry `AUCTION`
(Buy It Now on an auction). Filter on `buyingOptions` in SQL if you want pure
fixed-price.

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
| `category_ids` | `["261328"]` | Verified live as `Trading Card Singles` (leaf) |
| `price_min` / `price_max` | `20` / `400` | USD band |
| `buying_options` | `["FIXED_PRICE"]` | Excludes auctions |
| `sort` | `newlyListed` | |
| `limit` / `max_pages` | `200` / `6` | Up to 1,200 listings per run |
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

## Browsable listings page

Every run regenerates `docs/index.html` — a single self-contained page with all
collected listings, searchable, filterable and sortable in the browser. No build
step, no external assets, no network calls; it works opened straight from disk.

```bash
python -m ebay_scanner.build_page          # writes docs/index.html
open docs/index.html                       # macOS
```

Because this repository is public, the same file can be served by **GitHub
Pages**: Settings → Pages → Source *Deploy from a branch*, branch = default,
folder = `/docs`. The page then updates itself on every scheduled run.

The page embeds the most recent 5,000 listings by default (`--limit`). Anything
beyond that stays queryable through the SQLite loader.

### A caveat on grader and grade

Those two columns are **parsed from the listing title**, not read from eBay's
item aspects, and the page says so. `item_summary/search` does not return
aspects, and the bulk `getItems` call that does requires a Buy API grant this
application has not been given — so `detailFetched` is `false` and every aspect
column in the stored JSONL is empty.

Titles carry a recognisable grader and grade about **82%** of the time, which is
enough to filter on. The parse is display-only and is deliberately never written
back into the JSONL, so the stored data stays purely API-derived. If the Buy API
grant comes through, real aspects take precedence automatically.

## Listing re-check (the sold-proxy)

The collector only ever writes *new* listings, so nothing revisited them and
disappearance — the only available proxy for a sale — was unmeasurable. The
re-check pass fills that in, running hourly on its own cron.

```
schedule:
  - cron: "*/15 * * * *"   # collection sweep
  - cron: "7 * * * *"      # re-check, offset off the hour
```

Each run sweeps whole hours of listing-creation time via the `itemStartDate`
window search — 200 live listings per call, against one per listing for
`getItem`. Anything stored but absent from its window is recorded as gone in
`data/lifecycle.jsonl` with price, seller, condition and hours-listed.

Three correctness rules, each learned from a failure in the first live runs:

- **Windows must be closed and aged.** A window whose end time is in the future
  makes eBay silently drop the date filter; one sweep came back with 3,053,675
  matches — the whole band — which would have marked the entire dataset sold.
- **Incomplete sweeps record nothing.** If a window cannot be paged in full the
  run warns and skips it, rather than reporting unseen listings as sold.
- **Windows rotate least-recently-swept first.** Ordering by window start would
  re-sweep the same oldest eight every run and never reach the other ~160.
  `data/recheck_state.json` tracks the last sweep per window; a 7-day backlog is
  fully covered within 24 hourly runs.

### Cost

About 9 pages per hour-window, 8 windows per run, ~72 calls hourly — roughly
1,700/day, on top of ~770/day for collection. Comfortably inside the 5,000/day
Browse limit.

### On `getItem`

Only the **bulk** `getItems` endpoint is 403 for this application. Single-item
`getItem` and `getItemByLegacyId` both return 200 and carry the full
`localizedAspects` block — Professional Grader, Grade, Set, Player/Athlete, Card
Number, Season and Certification Number. Enrichment is therefore possible at one
call per listing, which does not fit the budget for ~29k new listings a day but
does support a sampled subset. Not yet wired up.

## Pipeline: matching, references, scanning

### Slices, and why they exist

Matching on the open catalogue does not work. Sports card singles are extreme
long tail — 27k listings spread across ~14k distinct cards, median bucket size
1 — so copies of the same card almost never co-occur and there is nothing to
compare against.

The fix is to pin `Set`, `Professional Grader` and `Grade` in the aspect filter.
All three are then known from the query itself, no enrichment call needed, and
the corpus concentrates on cards that actually repeat. **Slices are chosen by volume, not by taste.** 3,360 distinct Set values exist
in the category; `mode: probe-sets` reads eBay's Set distribution under each
grade tier and writes `data/set_volumes.json` ranked by live listing count. Any set carrying at least `slice_min_listings` (500) earns a slice, crossed with
`slice_grades` — **649 sets x 5 tiers = 3,245 slices** covering PSA 10/9, BGS
9.5, SGC 10 and CGC 10.

A floor rather than a top-N cut, because capping by rank excluded whole brands
that are perfectly tradeable, just lower-volume than modern Prizm: Fleer, Upper
Deck, Stadium Club, Score, Leaf, Allen & Ginter, National Treasures and
Contenders were all absent at a 60-set cap.

Two traps this avoids. Hand-picking missed the highest-volume set entirely
(`2024 Bowman`, 41,642 listings). And basketball sets are named across two
years, so `2023-24 Panini Prizm` is a *different* Set value from `2023 Panini
Prizm` — picking by hand silently dropped one of them.

One pass over the grid is roughly 9,000 calls against a 5,000/day ceiling, so
backfill runs on its own cron every four hours, rotating
`backfill_slices_per_run` slices at a time and resuming from
`data/backfill_state.json`. A full pass takes several days; after that ordinary
collection keeps the slices current.

### Call budget

| Job | Cadence | Calls/day |
|---|---|---|
| collect | every 15 min | ~770 |
| re-check | hourly | ~1,080 |
| enrich | hourly | ~620 |
| backfill | every 4h | ~2,400 |
| **total** | | **~4,870 / 5,000** |

Enrichment was cut back deliberately. Slices supply set, grader and grade from
the query itself, so `getItem` is now only buying certification numbers and
player names — worth less than the comps backfill creates with the same calls.

`python -m ebay_scanner.backfill` walks `itemStartDate` windows backwards to
pull the slice's **standing inventory**, not just newly-listed items — 200
listings per call, which is what makes depth affordable.

### Bucket key

```
year | set | card_number | parallel | grader | grade
```

Normalization, all covered by tests:

- **Grader** — aliases map to a code, Beckett to BGS. Qualifiers (OC/ST/MK)
  are recorded but excluded from the key; an OC copy is still the same card.
- **Grade** — numeric only, so "PSA 10 GEM MT" and "PSA 10" are one bucket
  while BGS 9.5 and BGS 9 stay separate.
- **Set** — year split into its own field, sport suffix dropped, so "1987
  Fleer" and "1987 Fleer Basketball" collapse.
- **Card number** — "#" and leading zeros stripped; alphanumeric forms like
  `#BCP50` and `#SS-TL` are kept.
- **Parallel** — matched on a flattened form so "Red White & Blue" and "Red
  White and Blue" are one value. Absence means `base`.

**Parallel is in the key deliberately.** Without it a base Prizm and a Gold /10
of the same card number share a bucket, producing $20–$400 spreads and
worthless references. Adding it cut wide buckets from 174/238 to under a
quarter.

**Player is stored but not required in the key.** Within a fixed set the card
number already determines the player, and requiring a title-parsed player
split the same card across several buckets.

Every record carries `match_method` — `slice`, `aspects` or `title` — so the
share of the dataset resting on the parser is measurable rather than assumed.

### Reference values

Per bucket: drop asks older than 45 days, require 5+ remaining, and take the
**median of the five lowest** as the reference. Buckets under the minimum are
suppressed entirely rather than guessed. `data/references.jsonl` holds the
current values with the full percentile spread; `data/reference_snapshots.jsonl`
appends one row per bucket per day so drift stays visible.

**A listing never prices itself.** Before a candidate is judged, its bucket is
recomputed with that listing removed, so it cannot vote on the value it is
being compared against. Leaving it in drags the reference down and understates
the discount. The 5-comp minimum then means five *other* listings, so a bucket
holding exactly the candidate plus four peers is suppressed rather than flagged
off a comp set of four.

### BIN scanner

Flags a listing only when every condition holds: price within $10–$800, bucket
has a valid reference, price at or under 70% of it, and the listing is under 24
hours old. Collection and alerting now share the $10–$800 band.

**Alerts rank by dollars saved, not discount percent.** With a $10 floor a 30%
discount on a $12 card saves $4 and would otherwise displace $180 off a $600
card under the 20/day cap. Each flag stores the reference, comp count, discount and the bucket's
full distribution at flag time, so false positives can be reviewed after the
fact.

**Delivery is Telegram** (free, and unlike ntfy's iOS app there is nothing to
buy). Set repo secrets `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`. `NTFY_TOPIC`
still works as a fallback if both Telegram secrets are absent. With no channel
configured the run records flags and logs a warning rather than failing.

Alerts are capped at 20/day. Exceeding the cap is treated as a signal that the
reference logic is wrong, so the run logs a warning and sends nothing rather
than spamming.

### Episodes (capture only)

Chains are keyed on certification number where present. Where absent the
fallback requires seller **and** bucket **and** image hash to agree, with
consecutive prices within 15%. Seller plus bucket alone is not enough — dealers
hold several copies of the same card in the same grade, and merging those would
manufacture relist chains that never happened. Nothing is surfaced yet.
