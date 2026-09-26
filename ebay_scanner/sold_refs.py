"""Sold-price references.

eBay's Browse API cannot see sold listings (that needs Marketplace Insights),
and GitHub runners get captcha-walled on eBay's sold search. So sold comps are
pulled from a real browser session and committed as data/sold_refs.json:

    {"generated": "...", "buckets": {"<bucket key>": [[price, "YYYY-MM-DD", title], ...]}}

Sold rows are pre-filtered to the bucket: same year, set, card number,
parallel, grader and grade, with Best Offer sales dropped (eBay shows the
asking price on those, not the accepted one).

A bucket key carries no player, and some card numbers are shared across
sports or subsets, so comps are narrowed per listing to the sold rows whose
titles share its name tokens. Comps that disagree (wide p25-p75 spread) are
treated as no reference rather than averaged.
"""
import json
import os
import re
import statistics
from datetime import datetime, timezone

from . import config

PATH = config.DATA_DIR / "sold_refs.json"
# Asking-price references produced junk alerts, so by default only listings
# backed by real sales can alert. Set REQUIRE_SOLD_REF=0 to fall back.
REQUIRE_SOLD = os.environ.get("REQUIRE_SOLD_REF", "1") != "0"
SOLD_DISCOUNT = float(os.environ.get("SOLD_DISCOUNT", "0.85"))
MAX_AGE_DAYS = int(os.environ.get("SOLD_MAX_AGE_DAYS", "45"))
MIN_SOLD = int(os.environ.get("SOLD_MIN_COMPS", "5"))
MAX_SPREAD = float(os.environ.get("SOLD_MAX_SPREAD", "2.0"))

_STOP = set("""
psa bgs sgc cgc beckett gem mint pristine graded grade card cards rookie
base panini topps bowman donruss optic prizm select mosaic chrome update
heritage finest stadium club archives gypsy queen allen ginter draft fleer
hoops ultra sapphire now traded best chronicles contenders phoenix certified
prestige score leaf upper deck flair metal skybox instant national treasures
immaculate flawless obsidian absolute revolution kings court spectra
illusions origins zenith cosmic platinum tribute rated prospect prospects
first edition basketball baseball football soccer hockey nba nfl mlb nhl
wnba ufc the and with for new hot invest sharp centered low pop set lot
refractor parallel silver holo auto insert variation image rookies
""".split())


def load():
    try:
        data = json.loads(PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return data.get("buckets", {})


def _tokens(title):
    out = set()
    for w in re.findall(r"[a-z][a-z'.]+", (title or "").lower()):
        w = w.strip("'.")
        if len(w) >= 3 and w not in _STOP:
            out.add(w)
    return out


def _pct(vals, q):
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 2)


def reference_for(title, rows, now=None):
    """Median recent sale for this listing's card, or None if not trustworthy."""
    now = now or datetime.now(timezone.utc)
    cand = _tokens(title)
    if not cand or not rows:
        return None
    need = 2 if len(cand) >= 2 else 1
    kept = []
    for row in rows:
        try:
            price, day, sold_title = row[0], row[1], row[2]
            sold_at = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, IndexError):
            continue
        age = (now - sold_at).days
        if age > MAX_AGE_DAYS:
            continue
        if len(cand & _tokens(sold_title)) < need:
            continue
        kept.append((float(price), age, day))
    if len(kept) < MIN_SOLD:
        return None
    prices = sorted(k[0] for k in kept)
    p25, p75 = _pct(prices, 0.25), _pct(prices, 0.75)
    if p25 <= 0 or p75 / p25 > MAX_SPREAD:
        return None
    med = round(statistics.median(prices), 2)
    kept.sort(key=lambda k: k[1])
    return {
        "reference": med, "comp_count": len(kept), "source": "sold",
        "low": prices[0], "p10": _pct(prices, 0.1), "p25": p25,
        "median": med, "p75": p75, "p90": _pct(prices, 0.9),
        "high": prices[-1],
        "oldest_days": max(k[1] for k in kept),
        "newest_days": min(k[1] for k in kept),
        "sales": [[k[0], k[2]] for k in kept[:5]],
    }


def passes_live(verdict):
    """Sold-backed candidates only need to be live and the cheapest copy."""
    if not verdict:
        return False, "no live query"
    if not verdict["still_listed"]:
        return False, "listing no longer live"
    if not verdict["is_lowest"]:
        return False, f"not lowest live BIN (${verdict['live_low']:.2f} exists)"
    return True, "verified (sold comps)"
