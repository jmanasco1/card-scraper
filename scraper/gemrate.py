"""
GemRate site access layer.

Strategy:
  1. Load pages with Playwright (site is JavaScript-rendered).
  2. Intercept JSON/XHR responses first — Next.js apps ship structured data,
     which is far more reliable than DOM parsing.
  3. Fall back to parsing rendered tables if no usable JSON is captured.

Two-stage funnel:
  Stage 1 (cheap):  set checklist pages -> every card's pop + gem rate.
  Stage 2 (costly): individual card pages -> recent sale comps.
  Stage 2 only runs for cards that survive the Stage 1 filters, which keeps
  request volume low enough to be a polite scraper.
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

BASE = "https://www.gemrate.com"

# Cloudflare sits in front of gemrate.com and serves a JS challenge
# ("Just a moment...") to anything that looks automated. Headless mode is the
# strongest bot signal, so CI runs headful under xvfb (GEMRATE_HEADFUL=1),
# prefer real Google Chrome over Playwright's bundled Chromium, keep the
# browser's own user agent, and wait for the challenge to clear — the
# cf_clearance cookie then covers the rest of the session.
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
    proxy dict, or None if unset. Cloudflare blocks datacenter IPs outright, so
    a residential/mobile proxy is required to scrape from CI."""
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
    grade_breakdown: dict = field(default_factory=dict)
    comps: list = field(default_factory=list)  # [{"date": iso, "price": float, "grade": str}]


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
        self._captured_json = []
        self.page.on("response", self._capture_json)

    def close(self):
        self._ctx.close()
        self._browser.close()
        self._pw.stop()

    # ------------------------------------------------------------------ #
    #  network capture
    # ------------------------------------------------------------------ #
    def _capture_json(self, response):
        try:
            ctype = response.headers.get("content-type", "")
            if "json" in ctype and response.status == 200:
                self._captured_json.append(
                    {"url": response.url, "body": response.json()}
                )
        except Exception:
            pass

    def _goto(self, url):
        self._captured_json = []
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
            if not self._captured_json:
                print("  [debug] no JSON responses captured")
            for blob in self._captured_json:
                body = json.dumps(blob["body"])
                print(f"  [debug] json {blob['url']} ({len(body)} bytes): {body[:400]}")

    # ------------------------------------------------------------------ #
    #  Stage 0: enumerate sets for a category
    # ------------------------------------------------------------------ #
    def list_sets(self, grader, category):
        """Return [{'name':..., 'url':...}] for a sport category."""
        url = f"{BASE}/sets?grader={grader}&category={category}"
        self._goto(url)

        sets = []
        # Prefer intercepted JSON
        for blob in self._captured_json:
            found = _walk_for_sets(blob["body"])
            if found:
                for s in found:
                    link = s.get("url") or s.get("slug") or s.get("href")
                    name = s.get("name") or s.get("set_name") or s.get("title")
                    if link and name:
                        sets.append({"name": name, "url": urljoin(BASE, str(link))})
        if sets:
            return _dedupe(sets)

        # DOM fallback: any link that looks like a set/pop-report page
        anchors = self.page.eval_on_selector_all(
            "a[href*='set']",
            "els => els.map(e => ({href: e.getAttribute('href'), text: e.innerText.trim()}))",
        )
        for a in anchors:
            if a["href"] and a["text"] and len(a["text"]) > 3:
                sets.append({"name": a["text"], "url": urljoin(BASE, a["href"])})
        return _dedupe(sets)

    # ------------------------------------------------------------------ #
    #  Stage 1: set checklist -> all cards with pop + gem rate
    # ------------------------------------------------------------------ #
    def scrape_set(self, set_url, set_name=""):
        self._goto(set_url)
        cards = []

        # JSON first
        for blob in self._captured_json:
            for item in _walk_for_cards(blob["body"]):
                row = _card_from_json(item, set_name, set_url)
                if row:
                    cards.append(row)
        if cards:
            return cards

        # DOM fallback: parse the rendered checklist table
        rows = self.page.eval_on_selector_all(
            "table tr",
            """rows => rows.map(r => {
                const cells = Array.from(r.querySelectorAll('td,th')).map(c => c.innerText.trim());
                const link = r.querySelector('a');
                return {cells, href: link ? link.getAttribute('href') : null};
            })""",
        )
        if not rows:
            return cards
        header = [h.lower() for h in rows[0]["cells"]]
        idx = _header_index(header)
        for r in rows[1:]:
            row = _card_from_dom(r, idx, set_name, set_url)
            if row:
                cards.append(row)
        return cards

    # ------------------------------------------------------------------ #
    #  Stage 2: card page -> recent comps + grade breakdown
    # ------------------------------------------------------------------ #
    def scrape_card(self, card: CardRow):
        self._goto(card.url)

        # JSON first
        for blob in self._captured_json:
            comps = _walk_for_comps(blob["body"])
            if comps:
                card.comps = comps
            grades = _walk_for_grades(blob["body"])
            if grades:
                card.grade_breakdown = grades
        if card.comps:
            return card

        # DOM fallback: look for a sales/comps table (date + $price rows)
        text_rows = self.page.eval_on_selector_all(
            "table tr",
            """rows => rows.map(r =>
                Array.from(r.querySelectorAll('td')).map(c => c.innerText.trim())
            )""",
        )
        for cells in text_rows:
            joined = " ".join(cells)
            price = _parse_price(joined)
            date = _parse_date(joined)
            if price and date:
                grade = next((c for c in cells if re.match(r"^(PSA|BGS|SGC|CGC)?\s*\d{1,2}(\.5)?$", c)), "")
                card.comps.append({"date": date, "price": price, "grade": grade})
        return card


# ---------------------------------------------------------------------- #
#  JSON walkers — tolerant of unknown response shapes
# ---------------------------------------------------------------------- #
def _iter_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_dicts(v)


def _walk_for_sets(body):
    out = []
    for d in _iter_dicts(body):
        keys = {k.lower() for k in d.keys()}
        if ("name" in keys or "set_name" in keys or "title" in keys) and (
            "slug" in keys or "url" in keys or "href" in keys
        ):
            out.append(d)
    return out


def _walk_for_cards(body):
    out = []
    for d in _iter_dicts(body):
        keys = {k.lower() for k in d.keys()}
        has_pop = bool(keys & {"total_pop", "population", "total", "pop"})
        has_gem = bool(keys & {"gem_rate", "gemrate", "gem_pct", "gems", "gem_pop"})
        has_name = bool(keys & {"card_name", "name", "player", "subject"})
        if has_pop and has_gem and has_name:
            out.append(d)
    return out


def _walk_for_comps(body):
    comps = []
    for d in _iter_dicts(body):
        keys = {k.lower() for k in d.keys()}
        if keys & {"sale_price", "price", "sold_price"} and keys & {"sale_date", "date", "sold_date", "end_date"}:
            price = _first(d, ["sale_price", "price", "sold_price"])
            date = _first(d, ["sale_date", "date", "sold_date", "end_date"])
            grade = _first(d, ["grade", "grade_name", "psa_grade"]) or ""
            p = _parse_price(str(price))
            dt = _parse_date(str(date))
            if p and dt:
                comps.append({"date": dt, "price": p, "grade": str(grade)})
    return comps


def _walk_for_grades(body):
    for d in _iter_dicts(body):
        keys = {k.lower() for k in d.keys()}
        # dicts that look like {"10": n, "9": n, "8": n, ...}
        numeric_keys = [k for k in keys if re.match(r"^\d{1,2}(\.5)?$", k)]
        if len(numeric_keys) >= 4:
            return {k: d[k] for k in d if re.match(r"^\d{1,2}(\.5)?$", str(k))}
    return {}


def _first(d, keys):
    lower = {k.lower(): v for k, v in d.items()}
    for k in keys:
        if k in lower:
            return lower[k]
    return None


def _card_from_json(item, set_name, set_url):
    name = _first(item, ["card_name", "name", "player", "subject"])
    total = _to_int(_first(item, ["total_pop", "population", "total", "pop"]))
    gem_pop = _to_int(_first(item, ["gem_pop", "gems"]))
    gem_rate = _to_float(_first(item, ["gem_rate", "gemrate", "gem_pct"]))
    number = str(_first(item, ["card_number", "number", "num"]) or "")
    link = _first(item, ["url", "slug", "href"])
    if not name or total is None:
        return None
    if gem_rate is None and gem_pop is not None and total:
        gem_rate = round(100.0 * gem_pop / total, 2)
    if gem_pop is None and gem_rate is not None:
        gem_pop = int(total * gem_rate / 100.0)
    return CardRow(
        set_name=set_name,
        card_name=str(name),
        card_number=number,
        url=urljoin(BASE, str(link)) if link else set_url,
        total_pop=total,
        gem_pop=gem_pop or 0,
        gem_rate_pct=gem_rate if gem_rate is not None else 0.0,
    )


def _header_index(header):
    idx = {}
    for i, h in enumerate(header):
        if "card" in h or "player" in h or "name" in h:
            idx.setdefault("name", i)
        if h in ("#", "no", "num") or "number" in h:
            idx.setdefault("number", i)
        if "pop" in h and "gem" not in h:
            idx.setdefault("pop", i)
        if "gem" in h and ("%" in h or "rate" in h):
            idx.setdefault("gem_rate", i)
        elif "gem" in h:
            idx.setdefault("gem_pop", i)
    return idx


def _card_from_dom(row, idx, set_name, set_url):
    cells = row["cells"]
    if "name" not in idx or "pop" not in idx:
        return None
    try:
        name = cells[idx["name"]]
        total = _to_int(cells[idx["pop"]])
        if not name or total is None:
            return None
        gem_rate = _to_float(cells[idx["gem_rate"]]) if "gem_rate" in idx and idx["gem_rate"] < len(cells) else None
        gem_pop = _to_int(cells[idx["gem_pop"]]) if "gem_pop" in idx and idx["gem_pop"] < len(cells) else None
        if gem_rate is None and gem_pop is not None and total:
            gem_rate = round(100.0 * gem_pop / total, 2)
        if gem_pop is None and gem_rate is not None:
            gem_pop = int(total * gem_rate / 100.0)
        number = cells[idx["number"]] if "number" in idx and idx["number"] < len(cells) else ""
        return CardRow(
            set_name=set_name,
            card_name=name,
            card_number=number,
            url=urljoin(BASE, row["href"]) if row["href"] else set_url,
            total_pop=total,
            gem_pop=gem_pop or 0,
            gem_rate_pct=gem_rate if gem_rate is not None else 0.0,
        )
    except (IndexError, ValueError):
        return None


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


def _to_float(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def _parse_price(text):
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text)
    return float(m.group(1).replace(",", "")) if m else None


def _parse_date(text):
    for fmt, pattern in [
        ("%Y-%m-%d", r"\d{4}-\d{2}-\d{2}"),
        ("%m/%d/%Y", r"\d{1,2}/\d{1,2}/\d{4}"),
        ("%b %d, %Y", r"[A-Z][a-z]{2} \d{1,2}, \d{4}"),
    ]:
        m = re.search(pattern, text)
        if m:
            try:
                return datetime.strptime(m.group(0), fmt).date().isoformat()
            except ValueError:
                continue
    return None


def _dedupe(items):
    seen, out = set(), []
    for it in items:
        if it["url"] not in seen:
            seen.add(it["url"])
            out.append(it)
    return out
