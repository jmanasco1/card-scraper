"""
Sign in to eBay once, by hand, into the profile the scan reuses.

eBay gates sold-listing searches behind a sign-in when the browser looks new,
and then refuses the sign-in itself if the browser also looks automated — which
presents as a login page that loops however carefully you type. Signing in
manually, in the same profile the scan will later use, breaks that loop: the
session cookie is already there when the scan starts, so eBay never asks.

Run this once (or whenever eBay signs you out):

    python -m scraper.login
"""

import sys

from playwright.sync_api import sync_playwright

from .gemrate import PROFILE_DIR, launch_persistent


def main():
    print("Opening a browser with the scan's saved profile.\n")
    print("  1. Sign in to eBay in the window that opens.")
    print("  2. Complete any verification it asks for.")
    print("  3. Come back here and press Enter.\n")
    print(f"Profile is stored in {PROFILE_DIR} and is not committed to git.\n")

    with sync_playwright() as pw:
        ctx = launch_persistent(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://www.ebay.com/signin/", wait_until="domcontentloaded",
                      timeout=60000)
        except Exception as e:
            print(f"Could not open eBay: {e}")

        input("Press Enter once you are signed in... ")

        # Confirm rather than assume: a session cookie is the thing that has to
        # survive, so check for one instead of trusting that the click worked.
        signed_in = False
        try:
            page.goto("https://www.ebay.com", wait_until="domcontentloaded", timeout=45000)
            cookies = ctx.cookies("https://www.ebay.com")
            names = {c["name"] for c in cookies}
            signed_in = bool(names & {"ebay", "s", "nonsession", "SSRT", "dp1"})
            body = page.evaluate("document.body ? document.body.innerText : ''")[:4000].lower()
            if "sign in" in body and "sign out" not in body:
                signed_in = signed_in and "hi " in body
        except Exception:
            pass

        if signed_in:
            print("\nLooks signed in. The scan will reuse this session.")
            print("Now run:  ./run.sh --limit 10 --debug")
        else:
            print("\nCould not confirm a signed-in session.")
            print("Re-run this and make sure you are fully signed in before pressing Enter.")

        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
