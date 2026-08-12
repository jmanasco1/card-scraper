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
import sys

from playwright.sync_api import sync_playwright

BASE = "https://www.gemrate.com"

DEFAULT_URLS = [
    BASE,
    f"{BASE}/sets?grader=psa&category=basketball-cards",
]


def recon(urls):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
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
            try:
                resp = page.goto(url, wait_until="networkidle", timeout=60000)
            except Exception as e:
                print(f"  goto failed: {e}")
                continue
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
