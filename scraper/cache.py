"""
On-disk cache for eBay price lookups.

Pricing a card means two eBay searches, each of which can throw a captcha you
have to solve by hand. That makes a full run expensive in human attention, so
losing the results because you tweaked a filter and re-ran would be painful.
Prices are cached by card identity and reused until they go stale.
"""

import json
import time
from pathlib import Path

CACHE_PATH = Path(__file__).resolve().parent.parent / "results" / ".price_cache.json"

_FIELDS = ("p10_median", "p10_sales", "p10_spread", "p10_low", "p10_high",
           "p9_median", "p9_sales", "p9_spread", "p9_low", "p9_high")

# Bumped whenever a change to comp matching invalidates prices collected under
# the old rules. Entries from an older version are ignored rather than reused,
# so a fix to what counts as a comp actually takes effect instead of being
# masked by cached results gathered before it.
VERSION = 4


def card_key(m):
    return "|".join(str(m.get(k, "")) for k in ("year", "set", "card", "number", "parallel"))


def load():
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def save(cache):
    CACHE_PATH.parent.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=1))


def usable(entry):
    """A cache entry is only worth reusing if it actually holds prices.

    Failed lookups must never satisfy a later run: caching "no prices found"
    would make a captcha or a bad search freeze into a result, and every re-run
    for the length of the TTL would replay that failure without touching eBay
    again. Retrying is cheap; a silently poisoned cache is not.
    """
    return bool(entry.get("p10_median")) and bool(entry.get("p9_median"))


def get(cache, m, ttl_days):
    """Copy cached prices onto `m`. Returns True on a fresh, usable hit."""
    entry = cache.get(card_key(m))
    if not entry:
        return False
    if time.time() - entry.get("ts", 0) > ttl_days * 86400:
        return False
    if entry.get("v") != VERSION:
        return False
    if not usable(entry):
        return False
    for f in _FIELDS:
        m[f] = entry.get(f)
    m["p10_sales"] = m.get("p10_sales") or 0
    m["p9_sales"] = m.get("p9_sales") or 0
    return True


def put(cache, m):
    """Store a lookup, but only when it produced prices at both grades."""
    entry = {f: m.get(f) for f in _FIELDS}
    if not usable(entry):
        return False
    entry["ts"] = time.time()
    entry["v"] = VERSION
    cache[card_key(m)] = entry
    return True
