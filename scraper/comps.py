"""
Sold-price comps from eBay, at two grades.

The value model needs a PSA 10 price *and* a PSA 9 price for the same card,
because the signal is the ratio between them. That makes comp hygiene matter a
lot more than it did when prices were only decoration: a ratio built from a
mismatched denominator is worse than no ratio at all, since it invents a
discount that isn't there.

So this module is strict about what counts as a comp:

  * the listing title must carry the exact grade searched for (a PSA 9 that
    sneaks into the PSA 10 set would deflate the gap and fake a bargain),
  * it must match the card's year and number when we know them,
  * lots, reprints, customs, breaks and digital cards are dropped outright,
  * prices are trimmed of outliers before the median is taken.

eBay throws captchas at automation, so this reuses the visible browser session
and pauses for a human when it hits one.
"""

import re
import statistics
import time
from datetime import date
from urllib.parse import quote_plus

EBAY_SOLD = (
    "https://www.ebay.com/sch/i.html?_nkw={q}&_sacat=0"
    "&LH_Sold=1&LH_Complete=1&_ipg=60&_sop=13"  # _sop=13 = most recently ended first
)

_PRICE_RE = re.compile(r"\$([\d,]+(?:\.\d{2})?)")
# eBay stamps every sold listing with "Sold  Mar 15, 2025".
_DATE_RE = re.compile(r"sold\s+([a-z]{3})\s+(\d{1,2}),?\s+(\d{4})", re.I)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}


def parse_sold_date(text):
    """Date of sale from a listing's text, or None."""
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    mon = _MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    try:
        return date(int(m.group(3)), mon, int(m.group(2)))
    except ValueError:
        return None

# Anything in a title that means the price isn't for one raw single card.
_JUNK = (
    "lot of", "card lot", "reprint", "rp)", "custom", "digital", "topps now digital",
    "break", "random", "you pick", "choose", "read description", "damaged",
    "authentic altered", "psa auth", "lot ", " lots", "repack", "proxy", "aceo",
    "sticker only", "not psa", "no psa", "coin", "sealed box", "wax pack",
)

# Set-name words too generic to prove a listing is the right set.
_SET_STOPWORDS = {"the", "of", "and", "series", "set", "cards", "card", "base",
                  "update", "basketball", "baseball", "football", "hockey"}

# Words that mark a card as a parallel, insert or variation rather than the
# base card. A base PSA 9 Cooper Flagg trades around $46 while its Refractor
# trades around $370, so counting both as one card inflates every median it
# touches — the single biggest source of wrong prices in this scraper.
#
# Colours are included because most parallels are named by colour, but colour
# words also appear in player names (Draymond Green) and set names (Gold
# Label), so any token that occurs in the card's own name or set is dropped
# from this list before it is applied.
_PARALLEL_WORDS = {
    "refractor", "xfractor", "x-fractor", "superfractor", "fractor",
    "holo", "holofoil", "foilboard", "shimmer", "wave", "raywave", "ray",
    "cracked", "mojo", "sepia", "atomic", "negative", "disco", "hyper",
    "laser", "scope", "velocity", "camo", "tiger", "snakeskin", "choice",
    "downtown", "die-cut", "diecut", "variation", "ssp", "sp", "prizm",
    "autograph", "auto", "patch", "relic", "jersey", "memorabilia", "signed",
    "gold", "silver", "bronze", "platinum", "pink", "green", "blue", "red",
    "purple", "orange", "black", "yellow", "teal", "aqua", "copper", "ruby",
    "sapphire", "emerald", "rainbow", "speckle", "mosaic", "optic", "ice",
}

# A serial number ("/25", "/199") only ever appears on a limited parallel.
_SERIAL_RE = re.compile(r"/\s?\d{1,4}\b")

# Grades that must NOT appear when we're searching for a specific grade.
_ALL_GRADES = ("psa 10", "psa 9", "psa 8", "psa 7", "psa 6", "psa 5",
               "bgs 10", "bgs 9.5", "bgs 9", "sgc 10", "sgc 9.5", "sgc 9")


def _norm(text):
    """Lowercase, collapse whitespace, and normalize grade spellings so
    'PSA10', 'PSA-10' and 'PSA 10' all compare equal."""
    t = " " + re.sub(r"\s+", " ", (text or "").lower()).strip() + " "
    t = re.sub(r"\b(psa|bgs|sgc|cgc)\s*[-–]?\s*(10|9\.5|9|8|7|6|5)\b", r"\1 \2", t)
    return t


def build_query(metrics, grade=10):
    """eBay search string for one card at one grade."""
    parts = [metrics.get("year", ""), metrics.get("set", ""), metrics.get("card", "")]
    parallel = metrics.get("parallel", "")
    if parallel and parallel.lower() != "base":
        parts.append(parallel)
    num = str(metrics.get("number", "") or "").strip()
    if num:
        parts.append(f"#{num}")
    parts.append(f"PSA {grade}")
    return " ".join(str(p) for p in parts if p).strip()


def build_ebay_url(metrics, grade=10):
    """The eBay 'sold listings' URL — handed to the user in the output so they
    can eyeball the same comps the scorer used."""
    return EBAY_SOLD.format(q=quote_plus(build_query(metrics, grade)))


def title_matches(title, metrics, grade, require_number=True):
    """True when the listing prices the card and grade we asked for."""
    return reject_reason(title, metrics, grade, require_number) is None


def reject_reason(title, metrics, grade, require_number=True):
    """Why this listing isn't a comp, or None if it is.

    Returning the reason rather than a bare boolean is what makes a bad run
    diagnosable: when every listing is discarded, the question is always which
    rule did the discarding, and guessing at that from the outside has proven
    expensive.

    `require_number` is relaxed on a second pass. Plenty of honest listings omit
    the card number ("1986 Fleer Michael Jordan Rookie PSA 10"), and on a
    single-card search the year plus the set in the query already pin it down
    well enough that demanding "#57" in the title throws away most of the real
    comps.
    """
    t = _norm(title)

    for j in _JUNK:
        if j in t:
            return f"junk term {j!r}"

    want = f"psa {grade}"
    if want not in t:
        return f"no {want!r} in title"
    # Reject titles carrying a different grade too ("PSA 9 and PSA 10 lot",
    # "upgrade from PSA 9"). Exactly one grade token may appear.
    present = {g for g in _ALL_GRADES if g in t}
    # "psa 10" contains no other token, but "psa 9" is a substring of nothing
    # here since we normalized spacing; guard the 9/9.5 overlap explicitly.
    if want == "psa 9" and "psa 9.5" in t:
        return "is a PSA 9.5"
    if present != {want}:
        return f"carries other grades {sorted(present - {want})}"

    # --- the player has to actually be on the card -------------------------
    #
    # eBay's search is fuzzy: asking for one card returns plenty of other cards
    # from the same set and era. Without this check any of them counted, which
    # is how a $2 common was priced at $70 and how a PSA 10 came back cheaper
    # than its own PSA 9 — they were simply different cards.
    name_tokens = [w for w in re.findall(r"[a-z]+", (metrics.get("card") or "").lower())
                   if len(w) > 1]
    if name_tokens:
        surname = name_tokens[-1]
        if surname not in t:
            return f"surname {surname!r} absent"
        # A surname alone is too weak (Jordan, Johnson, Smith recur constantly),
        # so for a normal "First Last" name demand another token too.
        if len(name_tokens) > 1 and not any(w in t for w in name_tokens[:-1]):
            return f"only surname matched, missing {name_tokens[:-1]}"

    # --- base cards and parallels are different cards -----------------------
    parallel = (metrics.get("parallel") or "").strip().lower()
    own_words = set(re.findall(r"[a-z]+", (metrics.get("card") or "").lower()))
    own_words |= set(re.findall(r"[a-z]+", (metrics.get("set") or "").lower()))
    if parallel and parallel != "base":
        # We want a specific parallel: it has to be named.
        wanted = [w for w in re.findall(r"[a-z]+", parallel) if len(w) > 2]
        if wanted and not all(w in t for w in wanted):
            return f"not the {parallel!r} parallel"
    else:
        # We want the base card: anything naming a parallel is a different card.
        suspects = _PARALLEL_WORDS - own_words
        hit = next((w for w in suspects if re.search(rf"\b{re.escape(w)}\b", t)), None)
        if hit:
            return f"parallel/insert {hit!r}, not the base card"
        if _SERIAL_RE.search(t):
            return "serial-numbered parallel"

    # --- and it has to be from the right set -------------------------------
    set_tokens = [w for w in re.findall(r"[a-z]+", (metrics.get("set") or "").lower())
                  if len(w) > 2 and w not in _SET_STOPWORDS]
    if set_tokens and not any(w in t for w in set_tokens):
        return f"set tokens {set_tokens} absent"

    year = str(metrics.get("year", "") or "").strip()
    if year and year.isdigit():
        # Match the season either as '1997' or as '1997-98'.
        short = year[2:]
        nxt = str(int(year) + 1)[2:]
        has_year = (re.search(rf"\b{year}\b", t) or f"{year}-{nxt}" in t
                    or f"{short}-{nxt}" in t)
        # Sellers often omit the year when the set and number already identify
        # the card. Only insist on it when a *different* year is stated, which
        # would mean a different card.
        other_year = re.search(r"\b(19[5-9]\d|20[0-4]\d)\b", t)
        if not has_year and other_year:
            return f"year {other_year.group(1)}, wanted {year}"

    num = str(metrics.get("number", "") or "").strip()
    if require_number and num:
        # eBay sellers write the number as "#165", "No. 165" or just "165".
        # Requiring the hash discarded plenty of correct comps; by this point
        # the player, set and year already match, so a bare number is safe.
        if not re.search(rf"(#\s*{re.escape(num)}\b|\bno\.?\s*{re.escape(num)}\b"
                         rf"|\b{re.escape(num)}\b)", t):
            return f"card number {num} absent"
    elif num:
        # Relaxed pass: a *different* explicit number is still disqualifying,
        # we just no longer insist one be present.
        m = re.search(r"#\s*(\w+)\b", t)
        if m and m.group(1).lower() != num.lower():
            return f"different card number #{m.group(1)}"

    return None


def _looks_like_captcha(page):
    try:
        blob = ((page.title() or "") + " " + (page.url or "")).lower()
    except Exception:
        return False
    return any(s in blob for s in
               ("captcha", "pardon our interruption", "security measure", "robot", "splashui"))


def _wait_out_captcha(page, query, interactive, debug):
    """Returns True if the page is usable, False if we should skip this card."""
    if not _looks_like_captcha(page):
        return True
    if not interactive:
        if debug:
            print(f"    [debug] eBay captcha for {query!r} — skipping")
        return False
    print(f"    !! eBay captcha — solve it in the browser window ({query})")
    deadline = time.time() + 180
    while time.time() < deadline and _looks_like_captcha(page):
        time.sleep(3)
    if _looks_like_captcha(page):
        print("    !! still blocked, skipping this card")
        return False
    print("    ok, continuing")
    return True


# eBay pads a thin result set rather than returning nothing: once the real
# matches run out it appends a band of loosely-related listings under a heading
# like "Results matching fewer words". Those are different cards — usually more
# expensive ones, since popular cards dominate the fallback — and counting them
# is why an obscure common priced like a star's rookie while a heavily-traded
# card priced correctly. Everything from that heading onwards is discarded.
_PADDING_MARKERS = (
    "results matching fewer words",
    "matching fewer words",
    "shop on ebay",
    "related searches",
    "you may also like",
    "similar items",
    "explore related",
)

_SCRAPE_JS = """
(sels) => {
  const [itemSel, titleSel, priceSel, markers] = sels;
  const nodes = Array.from(document.querySelectorAll(itemSel + ', h2, h3'));
  const out = [];
  for (const n of nodes) {
    const tag = n.tagName.toLowerCase();
    if (tag === 'h2' || tag === 'h3') {
      const txt = (n.innerText || '').trim().toLowerCase();
      if (txt && markers.some(m => txt.includes(m))) break;   // padding starts here
      continue;
    }
    if (!n.matches(itemSel)) continue;
    const blob = (n.innerText || '').toLowerCase();
    if (blob.includes('sponsored')) continue;
    const t = n.querySelector(titleSel);
    const p = n.querySelector(priceSel);
    const title = t ? t.innerText.trim() : '';
    if (!title || /shop on ebay/i.test(title)) continue;
    out.push({
      title: title,
      price: p ? p.innerText.trim() : '',
      text: n.innerText.trim()
    });
  }
  return out;
}
"""


def _scrape_listings(page):
    """Pull (title, price-text, full-text) for each genuine result row.

    Walks the results in document order and stops at eBay's padding heading, so
    only listings that actually matched the search are returned. eBay serves a
    few different markups depending on the A/B bucket, so several selector sets
    are tried.
    """
    for item_sel, title_sel, price_sel in (
        ("li.s-item", ".s-item__title", ".s-item__price"),
        ("li.s-card", ".s-card__title", ".s-card__price"),
        ("[data-testid='item-card']", "[role='heading']", ".s-card__price"),
    ):
        try:
            items = page.evaluate(
                _SCRAPE_JS, [item_sel, title_sel, price_sel, list(_PADDING_MARKERS)]
            )
        except Exception:
            continue
        if items:
            return items
    return []


def _trimmed(prices, trim=0.15):
    """Drop prices that can't plausibly be the same card as the rest.

    Percentile trimming alone assumes contamination is small and symmetric.
    It isn't: a handful of wrong-card listings survive the title filter and
    they skew high, because expensive cards are the ones that get listed with
    lots of extra words. So outliers are cut by distance from the median in
    units of median absolute deviation, which doesn't care how many are on one
    side, and only then are the tails trimmed.
    """
    if len(prices) < 4:
        return sorted(prices)
    s = sorted(prices)
    med = statistics.median(s)
    devs = [abs(p - med) for p in s]
    mad = statistics.median(devs)
    if mad > 0:
        kept = [p for p in s if abs(p - med) <= 4.0 * mad]
    else:
        # Every price identical bar a few: keep those within 25% of the median.
        kept = [p for p in s if abs(p - med) <= 0.25 * med] or s
    if len(kept) >= 3:
        # MAD has already removed what doesn't belong; trimming the tails on
        # top of it would just discard real sales and narrow the range the
        # report shows for sanity-checking.
        return kept
    if len(s) < 5:
        return s
    k = max(1, int(len(s) * trim))
    return s[k:-k] or s


def fetch_grade_comps(page, metrics, grade, delay=1.5, debug=False, interactive=True, stats=None):
    """Price one card at one grade.

    Returns {'median','sales','spread','low','high'} or None if there weren't
    enough clean matching sales to be worth a number.
    """
    if stats is None:
        stats = {}
    query = build_query(metrics, grade)
    url = EBAY_SOLD.format(q=quote_plus(query))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        stats["nav_failed"] = stats.get("nav_failed", 0) + 1
        if debug:
            print(f"    [debug] eBay goto failed for {query!r}: {e}")
        return None

    time.sleep(delay)
    if _looks_like_captcha(page):
        stats["captcha"] = stats.get("captcha", 0) + 1
    if not _wait_out_captcha(page, query, interactive, debug):
        stats["captcha_blocked"] = stats.get("captcha_blocked", 0) + 1
        return None

    items = _scrape_listings(page)

    # Save one raw results page. The padding cutoff depends on eBay's markup,
    # which changes and cannot be verified without seeing it.
    if not stats.get("saved_html"):
        try:
            from pathlib import Path
            out = Path(__file__).resolve().parent.parent / "results" / "ebay_page_sample.html"
            out.parent.mkdir(exist_ok=True)
            out.write_text(page.content())
            stats["saved_html"] = True
        except Exception:
            pass

    audit = stats.setdefault("audit", [])
    entry = {
        "query": query,
        "grade": grade,
        "listings": len(items),
        "titles": [it.get("title", "")[:110] for it in items[:25]],
        "rejects": [],
        "kept": [],
    }
    audit.append(entry)

    def harvest(strict):
        """Returns [(price, sold_date_or_None)] for the listings that match."""
        out = []
        for it in items:
            if not title_matches(it.get("title", ""), metrics, grade, require_number=strict):
                continue
            pm = _PRICE_RE.search(it.get("price", "")) or _PRICE_RE.search(it.get("text", ""))
            if not pm:
                continue
            try:
                price = float(pm.group(1).replace(",", ""))
            except ValueError:
                continue
            out.append((price, parse_sold_date(it.get("text", ""))))
        return out

    sales = harvest(strict=True)
    matching = "strict"
    if len(sales) < 3:
        relaxed = harvest(strict=False)
        if len(relaxed) > len(sales):
            sales, matching = relaxed, "relaxed"
    prices = [p for p, _ in sales]

    entry["matching"] = matching
    for it in items[:25]:
        why = reject_reason(it.get("title", ""), metrics, grade,
                            require_number=(matching == "strict"))
        line = f"{it.get('price','?')[:14]:>14}  {it.get('title','')[:96]}"
        if why:
            entry["rejects"].append(f"{line}   << {why}")
        else:
            entry["kept"].append(line)
    entry["prices"] = sorted(prices)
    entry["dated"] = sum(1 for _, d in sales if d)

    stats.setdefault("listings_seen", 0)
    stats["listings_seen"] += len(items)
    if items and not prices:
        stats["all_filtered"] = stats.get("all_filtered", 0) + 1
    if not items:
        stats["no_listings"] = stats.get("no_listings", 0) + 1

    if debug:
        print(f"    [debug] PSA {grade}: {len(items)} listings on page, "
              f"{len(prices)} usable ({matching} matching) — {query!r}")
        if items and not prices:
            for it in items[:3]:
                print(f"        rejected: {it.get('title','')[:88]!r}")

    if not prices:
        return None

    core = _trimmed(prices)
    keep = list(core)
    # The search is sorted by most-recently-ended first (_sop=13 in the URL),
    # so position in the results *is* time order. Relying on parsed dates
    # instead made the whole signal hostage to one regex against markup that
    # may not carry a date at all; ordering is structural and always present.
    ordered = []
    for price, _d in sales:                     # newest first, as returned
        if price in keep:
            keep.remove(price)
            ordered.append(price)
    median = statistics.median(core)
    # Dispersion relative to the median: high values mean the "comps" are
    # probably a mix of different cards, so the model discounts them.
    spread = 0.0
    if median > 0 and len(core) > 1:
        spread = (max(core) - min(core)) / median
    return {
        "median": round(median, 2),
        "sales": len(prices),
        "spread": round(spread, 2),
        "low": round(min(core), 2),
        "high": round(max(core), 2),
        "trend": _trend(ordered),
        "dates_seen": sum(1 for _, d in sales if d),
    }


def _trend(ordered):
    """How this card's own price has moved, newest sales versus earlier ones.

    `ordered` is newest-first, which is how eBay returns the search. This is a
    within-card comparison, which is the point: estimating what a card *should*
    cost from other cards means modelling why one card's PSA 10 commands 3x its
    9 while another's commands 15x, and that variation is driven by things no
    available field captures. A card measured against its own recent history
    assumes none of it — player, set, era and collector base are all held fixed
    because it is the same card.
    """
    if len(ordered) < 6:
        return None
    cut = max(2, len(ordered) // 3)
    recent = ordered[:cut]           # newest
    older = ordered[cut:]            # everything before
    if not older or not recent:
        return None
    old_med, new_med = statistics.median(older), statistics.median(recent)
    if old_med <= 0:
        return None
    return {
        "older_median": round(old_med, 2),
        "recent_median": round(new_med, 2),
        "recent_n": len(recent),
        "older_n": len(older),
        "change_pct": round(100.0 * (new_med - old_med) / old_med, 1),
    }


def price_card(page, metrics, delay=1.5, debug=False, interactive=True, stats=None):
    """Price a card at PSA 10 and PSA 9 and write the fields the value model
    reads. Returns True if both grades produced a price.

    `stats` accumulates why lookups fail across the whole run, so a scan that
    scores nothing can say which stage broke instead of just coming up empty.
    """
    if stats is None:
        stats = {}
    ten = fetch_grade_comps(page, metrics, 10, delay, debug, interactive, stats)
    nine = fetch_grade_comps(page, metrics, 9, delay, debug, interactive, stats)
    stats["attempted"] = stats.get("attempted", 0) + 1

    for prefix, res in (("p10", ten), ("p9", nine)):
        metrics[f"{prefix}_median"] = res["median"] if res else None
        metrics[f"{prefix}_sales"] = res["sales"] if res else 0
        metrics[f"{prefix}_spread"] = res["spread"] if res else None
        # Carried into the report so a nonsense comp set is visible without
        # opening eBay: a $2 card showing a $2-$720 range is obviously wrong.
        metrics[f"{prefix}_low"] = res["low"] if res else None
        metrics[f"{prefix}_high"] = res["high"] if res else None
        metrics[f"{prefix}_trend"] = res.get("trend") if res else None
    return bool(ten and nine)
