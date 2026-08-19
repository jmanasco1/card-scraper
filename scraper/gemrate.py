"""
GemRate site access layer.

GemRate rebuilt their site: the old per-set checklist + sale-comp pages are
gone. The current data lives on the "Top Cards" report
(/top-cards?grader=...&category=...), which embeds the full dataset in the page
as a JavaScript variable:

    var RowData = JSON.parse('[{"card_number": "26", "category": "Basketball",
        "gems": 8711, "grader": "psa", "name": "Michael Jordan",
        "parallel": "Base", "set_name": "Fleer", "total": 70968,
        "year": "1990", "trend_links": {...}}, ...]')

So the strategy is now single-stage:
  1. Load the Top Cards page for the sport/category with Playwright (a real
     browser is required — Cloudflare blocks non-browser and datacenter IPs).
  2. Pull the RowData JSON straight out of the HTML — far more reliable than
     scraping the rendered ag-Grid table.

Note: GemRate no longer publishes recent sale prices (those moved to their
CardLadder integration), so this layer returns population + gem-rate data only.
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

BASE = "https://www.gemrate.com"

# Cloudflare sits in front of gemrate.com and serves a JS challenge
# ("Just a moment...") to anything that looks automated. Headless mode is the
# strongest bot signal, so CI runs headful under xvfb (GEMRATE_HEADFUL=1) and
# we prefer real Google Chrome over Playwright's bundled Chromium. NOTE:
# Cloudflare blocks datacenter IPs (e.g. GitHub-hosted runners) outright; set
# GEMRATE_PROXY to a residential/mobile proxy to scrape from CI. A normal home
# connection clears the challenge on its own.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--start-maximized",
]
CHALLENGE_TITLES = ("just a moment", "attention required", "verifying you are human")

# Injected before any page script runs, to erase the most common
# automation fingerprints Cloudflare looks for.
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
    window.navigator.permissions.query = (p) => (
        p && p.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : origQuery(p)
    );
}
"""


def default_headless():
    return os.environ.get("GEMRATE_HEADFUL", "") != "1"


def proxy_config():
    """Parse GEMRATE_PROXY (e.g. http://user:pass@host:port) into Playwright's
    proxy dict, or None if unset. Required to scrape from a datacenter IP."""
    from urllib.parse import urlparse

    raw = os.environ.get("GEMRATE_PROXY", "").strip()
    if not raw:
        return None
    p = urlparse(raw)
    server = f"{p.scheme}://{p.hostname}"
    if p.port:
        server += f":{p.port}"
    cfg = {"server": server}
    if p.username:
        cfg["username"] = p.username
    if p.password:
        cfg["password"] = p.password
    return cfg


def launch_browser(pw, headless):
    """Prefer real Google Chrome (best Cloudflare pass rate); fall back to
    the msedge channel, then bundled Chromium. Honors GEMRATE_PROXY."""
    proxy = proxy_config()
    kwargs = {"headless": headless, "args": LAUNCH_ARGS}
    if proxy:
        kwargs["proxy"] = proxy
    for channel in ("chrome", "msedge"):
        try:
            return pw.chromium.launch(channel=channel, **kwargs)
        except Exception:
            continue
    return pw.chromium.launch(**kwargs)


def new_stealth_context(browser):
    ctx = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
    )
    ctx.add_init_script(STEALTH_JS)
    return ctx


def wait_for_challenge(page, timeout_s=60):
    """Wait until the page title is no longer a Cloudflare challenge.
    Returns True if clear, False if the challenge never resolved."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            title = page.title().lower()
        except Exception:
            title = ""
        if title and not any(t in title for t in CHALLENGE_TITLES):
            return True
        time.sleep(2)
    return False


@dataclass
class CardRow:
    set_name: str
    card_name: str
    card_number: str
    url: str
    total_pop: int
    gem_pop: int
    gem_rate_pct: float
    year: str = ""
    parallel: str = ""


# ---------------------------------------------------------------------- #
#  RowData extraction — the dataset embedded in the Top Cards page
# ---------------------------------------------------------------------- #
_ROWDATA_RE = re.compile(r"var\s+RowData\s*=\s*JSON\.parse\(\s*'")


def extract_rowdata(html):
    """Pull the `var RowData = JSON.parse('...')` array out of page HTML.
    Returns a list of dicts, or [] if the marker isn't present."""
    m = _ROWDATA_RE.search(html)
    if not m:
        return []
    i = m.end()
    out = []
    while i < len(html):
        c = html[i]
        if c == "\\":
            out.append(html[i : i + 2])
            i += 2
            continue
        if c == "'":
            break
        out.append(c)
        i += 1
    raw = "".join(out)
    # The captured text is a JS single-quoted string literal (\uXXXX, \', \\,
    # ...). Decoding the escapes yields the JSON document itself.
    try:
        js = raw.encode("utf-8").decode("unicode_escape")
        return json.loads(js)
    except Exception:
        # Fallback: handle only the escapes JSON itself understands.
        try:
            return json.loads(raw.replace("\\'", "'"))
        except Exception:
            return []


def card_from_row(item):
    """Build a CardRow from one RowData entry, computing gem rate."""
    total = _to_int(item.get("total"))
    gems = _to_int(item.get("gems"))
    name = item.get("name")
    if not name or not total:
        return None
    gem_rate = round(100.0 * gems / total, 2) if gems is not None else 0.0
    links = item.get("trend_links") or {}
    grader = str(item.get("grader") or "psa").lower()
    link = links.get(grader) or links.get("psa") or ""
    url = f"{BASE}{link}" if link.startswith("/") else (link or BASE)
    return CardRow(
        set_name=str(item.get("set_name") or ""),
        card_name=str(name),
        card_number=str(item.get("card_number") or ""),
        url=url,
        total_pop=total,
        gem_pop=gems or 0,
        gem_rate_pct=gem_rate,
        year=str(item.get("year") or ""),
        parallel=str(item.get("parallel") or ""),
    )


class GemRateClient:
    def __init__(self, delay_seconds=1.5, headless=None, debug=False):
        self.delay = delay_seconds
        self.debug = debug
        if headless is None:
            headless = default_headless()
        self._pw = sync_playwright().start()
        self._browser = launch_browser(self._pw, headless)
        self._ctx = new_stealth_context(self._browser)
        self.page = self._ctx.new_page()

    def close(self):
        self._ctx.close()
        self._browser.close()
        self._pw.stop()

    def _goto(self, url):
        resp = self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        cleared = wait_for_challenge(self.page)
        try:
            self.page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        time.sleep(self.delay)
        if self.debug:
            status = resp.status if resp else "?"
            note = "" if cleared else " | CHALLENGE NOT CLEARED"
            print(f"  [debug] GET {url} -> {status} | title: {self.page.title()!r}{note}")
        return cleared

    def fetch_report(self, category, grader="psa", **params):
        """Load the Top Cards report for a category (plus any extra query
        params) and return [CardRow].

        Extra params are passed straight through to the URL. Which ones the
        site actually honors is discovered at runtime by `walk_universe` —
        see the note there.
        """
        query = {"grader": grader, "category": category}
        query.update({k: v for k, v in params.items() if v not in (None, "")})
        url = f"{BASE}/top-cards?" + urlencode(query)
        self._goto(url)
        rows = extract_rowdata(self.page.content())
        if self.debug:
            print(f"  [debug] RowData rows extracted: {len(rows)} from {url}")
            if not rows:
                print("  [debug] no RowData found — page structure may have changed")
        return [row for item in rows if (row := card_from_row(item))]

    def fetch_top_cards(self, category, grader="psa"):
        """The unfiltered Top Cards report — the ~100 most-graded cards of all
        time. Kept for the `--universe top` mode; note that ranking this pool
        is a fame contest, not a value screen."""
        return self.fetch_report(category, grader)

    def walk_universe(self, category, grader="psa", years=(), debug=False):
        """Build the widest card pool the site will give us.

        The Top Cards report returns roughly the 100 most-graded cards for
        whatever slice you ask for. Asking only for a category therefore yields
        the all-time blue chips and nothing else — which is exactly why the old
        scan could only ever return famous vintage.

        Requesting the report per *year* should slice the same report into
        per-year top-100s, reaching cards that the all-time list buries. We
        can't confirm from here whether the site honors a `year` param (its
        endpoints are undocumented and Cloudflare blocks datacenter IPs, so
        this is unverifiable outside a real local run), so instead of trusting
        it we measure it: fetch the baseline, then fetch one year and check
        whether the rows actually differ. If the param is ignored we say so and
        stop, rather than burning forty page loads on identical responses.

        Returns (cards, report) where report describes what worked.
        """
        seen = {}
        report = {"year_param_honored": None, "slices": []}

        def absorb(label, rows):
            new_count = 0
            for c in rows:
                key = (c.year, c.set_name, c.card_name, c.card_number, c.parallel)
                if key not in seen:
                    seen[key] = c
                    new_count += 1
            report["slices"].append({"slice": label, "rows": len(rows), "new": new_count})
            if debug or new_count:
                print(f"  {label}: {len(rows)} rows, {new_count} new (pool now {len(seen)})")
            return new_count

        base = self.fetch_report(category, grader)
        base_keys = {(c.year, c.set_name, c.card_name, c.card_number, c.parallel) for c in base}
        absorb("all-time", base)

        if not years:
            return list(seen.values()), report

        # Probe one year before committing to the full sweep.
        probe_year = years[0]
        probe = self.fetch_report(category, grader, year=probe_year)
        probe_keys = {(c.year, c.set_name, c.card_name, c.card_number, c.parallel) for c in probe}

        if not probe or probe_keys == base_keys:
            report["year_param_honored"] = False
            print(
                f"\n  !! /top-cards ignored ?year={probe_year} (returned the same "
                f"{len(probe)} rows as the unfiltered report).\n"
                "     The universe is capped at the all-time top cards, so results will\n"
                "     still skew to blue chips. Run `python -m scraper.recon` to dump the\n"
                "     site's real navigation and report back what set/year URLs exist."
            )
            return list(seen.values()), report

        report["year_param_honored"] = True
        absorb(f"year={probe_year}", probe)
        for y in years[1:]:
            absorb(f"year={y}", self.fetch_report(category, grader, year=y))

        return list(seen.values()), report


# ---------------------------------------------------------------------- #
#  parsing helpers
# ---------------------------------------------------------------------- #
def _to_int(v):
    if v is None:
        return None
    try:
        return int(float(str(v).replace(",", "").replace("%", "").strip()))
    except ValueError:
        return None
