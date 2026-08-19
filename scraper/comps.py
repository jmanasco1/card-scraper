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
from urllib.parse import quote_plus

EBAY_SOLD = (
    "https://www.ebay.com/sch/i.html?_nkw={q}&_sacat=0"
    "&LH_Sold=1&LH_Complete=1&_ipg=60&_sop=13"  # _sop=13 = most recently ended first
)

_PRICE_RE = re.compile(r"\$([\d,]+(?:\.\d{2})?)")

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
    """Does this listing actually price the card and grade we asked for?

    `require_number` is relaxed on a second pass. Plenty of honest listings omit
    the card number ("1986 Fleer Michael Jordan Rookie PSA 10"), and on a
    single-card search the year plus the set in the query already pin it down
    well enough that demanding "#57" in the title throws away most of the real
    comps.
    """
    t = _norm(title)

    if any(j in t for j in _JUNK):
        return False

    want = f"psa {grade}"
    if want not in t:
        return False
    # Reject titles carrying a different grade too ("PSA 9 and PSA 10 lot",
    # "upgrade from PSA 9"). Exactly one grade token may appear.
    present = {g for g in _ALL_GRADES if g in t}
    # "psa 10" contains no other token, but "psa 9" is a substring of nothing
    # here since we normalized spacing; guard the 9/9.5 overlap explicitly.
    if want == "psa 9" and "psa 9.5" in t:
        return False
    if present != {want}:
        return False

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
            return False
        # A surname alone is too weak (Jordan, Johnson, Smith recur constantly),
        # so for a normal "First Last" name demand another token too.
        if len(name_tokens) > 1 and not any(w in t for w in name_tokens[:-1]):
            return False

    # --- and it has to be from the right set -------------------------------
    set_tokens = [w for w in re.findall(r"[a-z]+", (metrics.get("set") or "").lower())
                  if len(w) > 2 and w not in _SET_STOPWORDS]
    if set_tokens and not any(w in t for w in set_tokens):
        return False

    year = str(metrics.get("year", "") or "").strip()
    if year and year.isdigit():
        # Match the season either as '1997' or as '1997-98'.
        short = year[2:]
        nxt = str(int(year) + 1)[2:]
        if not re.search(rf"\b{year}\b", t) and f"{year}-{nxt}" not in t and f"{short}-{nxt}" not in t:
            return False

    num = str(metrics.get("number", "") or "").strip()
    if require_number and num:
        if not re.search(rf"(#\s*{re.escape(num)}\b|\bno\.?\s*{re.escape(num)}\b)", t):
            return False
    elif num:
        # Relaxed pass: a *different* explicit number is still disqualifying,
        # we just no longer insist one be present.
        m = re.search(r"#\s*(\w+)\b", t)
        if m and m.group(1).lower() != num.lower():
            return False

    return True


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


def _scrape_listings(page):
    """Pull (title, price-text, full-text) for each result row. eBay serves two
    different card markups depending on the A/B bucket, so try both."""
    for selector, title_sel, price_sel in (
        ("li.s-item", ".s-item__title", ".s-item__price"),
        ("li.s-card", ".s-card__title", ".s-card__price"),
        ("[data-testid='item-card']", "[role='heading']", ".s-card__price"),
    ):
        try:
            items = page.eval_on_selector_all(
                selector,
                """(els, sels) => els.map(e => {
                    const t = e.querySelector(sels[0]);
                    const p = e.querySelector(sels[1]);
                    return {
                        title: t ? t.innerText.trim() : '',
                        price: p ? p.innerText.trim() : '',
                        text: e.innerText.trim()
                    };
                })""",
                [title_sel, price_sel],
            )
        except Exception:
            continue
        if items:
            return items
    return []


def _trimmed(prices, trim=0.15):
    """Drop the extreme tails before averaging. eBay sold data always carries a
    few nonsense prices (best-offer artifacts, mislabeled slabs)."""
    if len(prices) < 5:
        return sorted(prices)
    s = sorted(prices)
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

    def harvest(strict):
        out = []
        for it in items:
            if not title_matches(it.get("title", ""), metrics, grade, require_number=strict):
                continue
            pm = _PRICE_RE.search(it.get("price", "")) or _PRICE_RE.search(it.get("text", ""))
            if not pm:
                continue
            try:
                out.append(float(pm.group(1).replace(",", "")))
            except ValueError:
                continue
        return out

    prices = harvest(strict=True)
    matching = "strict"
    if len(prices) < 3:
        relaxed = harvest(strict=False)
        if len(relaxed) > len(prices):
            prices, matching = relaxed, "relaxed"

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
    return bool(ten and nine)
