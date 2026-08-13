"""
Recent sale comps from eBay.

GemRate dropped sale prices from its site, so the "is this moving / undervalued"
signal now comes from eBay's completed (sold) listings. For a given card we
search eBay sold PSA-10 listings, parse the recent sale prices + dates, and
compute a short-window price momentum.

Reuses the already-open Playwright page (same real-browser session that clears
bot checks). eBay throttles heavy scraping, so this runs best from a home IP
and keeps a polite delay between lookups.
"""

import re
import statistics
from datetime import datetime
from urllib.parse import quote_plus

EBAY_SOLD = (
    "https://www.ebay.com/sch/i.html?_nkw={q}&_sacat=0"
    "&LH_Sold=1&LH_Complete=1&_ipg=60&_sop=13"  # _sop=13 = most recently ended first
)

_PRICE_RE = re.compile(r"\$([\d,]+(?:\.\d{2})?)")
_DATE_RE = re.compile(r"Sold\s+([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})")


def build_query(metrics):
    """Build an eBay search string for one candidate card."""
    parts = [metrics.get("year", ""), metrics.get("set", ""), metrics.get("card", "")]
    parallel = metrics.get("parallel", "")
    if parallel and parallel.lower() != "base":
        parts.append(parallel)
    num = str(metrics.get("number", "") or "").strip()
    if num:
        parts.append(f"#{num}")
    parts.append("PSA 10")
    return " ".join(p for p in parts if p).strip()


def build_ebay_url(metrics):
    """The eBay 'sold listings' search URL for a card — open it to eyeball
    real recent PSA-10 solds without tripping eBay's scraper captcha."""
    return EBAY_SOLD.format(q=quote_plus(build_query(metrics)))


def _looks_like_captcha(page):
    try:
        t = (page.title() or "").lower()
    except Exception:
        t = ""
    return any(s in t for s in ("captcha", "pardon our interruption", "security measure", "robot"))


def fetch_comps(page, query, delay=1.5, debug=False, interactive=False):
    """Return recent sold comps [{'date': iso, 'price': float}] for a query,
    newest first, restricted to titles that look like PSA 10 sales."""
    url = EBAY_SOLD.format(q=quote_plus(query))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        if debug:
            print(f"    [debug] eBay goto failed for {query!r}: {e}")
        return []
    import time

    time.sleep(delay)

    # eBay throws a captcha wall at automation. If we're running visibly
    # (interactive), pause so a human can solve it; otherwise give up on this
    # lookup rather than hang.
    if _looks_like_captcha(page):
        if not interactive:
            if debug:
                print(f"    [debug] eBay captcha for {query!r} — skipping (use --comps to solve manually)")
            return []
        print(f"    !! eBay captcha — solve it in the Chrome window; waiting up to 120s ({query})")
        deadline = time.time() + 120
        while time.time() < deadline and _looks_like_captcha(page):
            time.sleep(3)
        if _looks_like_captcha(page):
            print("    !! still blocked, skipping this card")
            return []

    items = page.eval_on_selector_all(
        "li.s-item",
        """els => els.map(e => {
            const t = e.querySelector('.s-item__title');
            const p = e.querySelector('.s-item__price');
            return {
                title: t ? t.innerText.trim() : '',
                price: p ? p.innerText.trim() : '',
                text: e.innerText.trim()
            };
        })""",
    )

    comps = []
    for it in items:
        title = it["title"].lower()
        if "psa 10" not in title.replace("psa10", "psa 10"):
            continue
        pm = _PRICE_RE.search(it["price"]) or _PRICE_RE.search(it["text"])
        dm = _DATE_RE.search(it["text"])
        if not pm:
            continue
        price = float(pm.group(1).replace(",", ""))
        date = None
        if dm:
            try:
                date = datetime.strptime(dm.group(1), "%b %d, %Y").date().isoformat()
            except ValueError:
                date = None
        comps.append({"date": date, "price": price})

    if debug:
        print(f"    [debug] eBay {query!r}: {len(items)} listings, {len(comps)} PSA-10 comps")
    return comps


def summarize(comps, min_sales=3):
    """From a list of comps (newest first) compute momentum + levels.

    Returns dict with recent_sales, latest_comp, oldest_comp, momentum_pct,
    or None if there aren't enough priced comps to be meaningful.
    """
    priced = [c["price"] for c in comps if c.get("price")]
    if len(priced) < min_sales:
        return None
    # comps come newest-first; split into recent half vs older half
    recent = priced[: max(1, len(priced) // 2)]
    older = priced[len(priced) // 2:]
    recent_med = statistics.median(recent)
    older_med = statistics.median(older)
    momentum = 0.0
    if older_med > 0:
        momentum = 100.0 * (recent_med - older_med) / older_med
    return {
        "recent_sales": len(priced),
        "latest_comp": priced[0],
        "oldest_comp": priced[-1],
        "recent_median": round(recent_med, 2),
        "momentum_pct": round(momentum, 1),
    }
