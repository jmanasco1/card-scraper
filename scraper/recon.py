"""
Site recon: maps gemrate.com's real structure so the scraper can be kept in
sync with the live site. Prints page titles, nav links, and every JSON/XHR
endpoint the app calls — run it in CI (or anywhere with open egress) and read
the logs.

Usage:
    python -m scraper.recon [url ...]
    (no args: homepage + the /sets URL the scraper uses for basketball)
"""

import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

from .gemrate import launch_browser, new_stealth_context, wait_for_challenge

BASE = "https://www.gemrate.com"

# The open question this tool exists to answer: how far past the ~100 all-time
# top cards can we get? The scan probes ?year= at runtime; if that turns out to
# be ignored, run this and look for real set/year navigation in the link dump.
DEFAULT_URLS = [
    BASE,
    f"{BASE}/top-cards?grader=psa&category=basketball-cards",
    f"{BASE}/top-cards?grader=psa&category=basketball-cards&year=2019",
]


def recon(urls):
    # Recon defaults to a *visible* browser, unlike the rest of the codebase.
    # It is only ever run by hand, and headless is the clearest signal
    # Cloudflare blocks — a headless recon just sits in the challenge until it
    # times out, which reads as a hang rather than a block. Set
    # GEMRATE_HEADLESS=1 to override.
    headless = os.environ.get("GEMRATE_HEADLESS", "") == "1"
    print(f"Launching a {'headless' if headless else 'visible'} browser. "
          f"Each page can take up to a minute while Cloudflare clears.\n")
    with sync_playwright() as pw:
        browser = launch_browser(pw, headless=headless)
        ctx = new_stealth_context(browser)
        page = ctx.new_page()
        captured = []

        def on_response(response):
            try:
                ctype = response.headers.get("content-type", "")
                if "json" in ctype:
                    body = response.text()
                    captured.append((response.status, response.url, body))
            except Exception:
                pass

        page.on("response", on_response)

        for url in urls:
            captured.clear()
            print(f"\n{'=' * 70}\nRECON {url}")
            started = time.time()
            print("  loading...", flush=True)
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"  goto failed: {e}")
                continue
            print(f"  loaded in {time.time() - started:.0f}s, "
                  f"waiting for Cloudflare...", flush=True)
            cleared = wait_for_challenge(page)
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            if not cleared:
                print("  !! Cloudflare challenge did not clear — if this browser is "
                      "not visible on screen, that is why. Re-run without "
                      "GEMRATE_HEADLESS set.")
            print(f"  status: {resp.status if resp else '?'}")
            print(f"  final url: {page.url}")
            print(f"  title: {page.title()!r}")

            anchors = page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => e.getAttribute('href') + ' | ' + e.innerText.trim().slice(0, 60))",
            )
            print(f"  --- {len(anchors)} links ---")
            for a in anchors[:120]:
                print(f"    {a}")

            print(f"  --- {len(captured)} JSON responses ---")
            for status, jurl, body in captured:
                print(f"    [{status}] {jurl} ({len(body)} bytes)")
                print(f"      {body[:600]}")

            # First 1500 chars of visible text, to see what actually rendered
            text = page.evaluate("document.body ? document.body.innerText : ''")
            print("  --- body text (first 1500 chars) ---")
            print("    " + json.dumps(text[:1500]))

        browser.close()


if __name__ == "__main__":
    recon(sys.argv[1:] or DEFAULT_URLS)
