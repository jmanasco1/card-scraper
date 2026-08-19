#!/usr/bin/env bash
#
# One-command local setup + scan, for macOS and Linux.
#
# The scan has to run on a home connection: gemrate.com's Cloudflare blocks
# datacenter IPs, and eBay shows captchas that a person has to click. This
# script handles the setup so that's the only thing left for you to do.
#
#   ./run.sh                      scan basketball, price 40 cards
#   ./run.sh --sport baseball     a different sport
#   ./run.sh --limit 10           quick trial run
#
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# --- Python -----------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found."
  echo "On macOS, install Apple's command line tools and try again:"
  echo "    xcode-select --install"
  exit 1
fi

PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)'; then
  echo "Python $PYV is too old; this needs 3.9 or newer."
  exit 1
fi

# --- Dependencies, in a virtualenv so nothing touches system Python ----------
if [ ! -d "$VENV" ]; then
  say "Creating a virtual environment in $VENV (one time)..."
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

say "Installing Python packages (one time, ~30s)..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# The scraper prefers real Google Chrome, because it clears Cloudflare's bot
# check far more reliably than Playwright's bundled Chromium. If Chrome is
# already on this machine there is nothing to download — Playwright drives the
# installed copy directly.
if [ -d "/Applications/Google Chrome.app" ] || command -v google-chrome >/dev/null 2>&1; then
  say "Found Google Chrome already installed — nothing to download."
else
  say "Installing a browser for Playwright to drive (one time, ~150MB)..."
  if ! playwright install chrome && ! playwright install chromium; then
    cat <<'MSG'

Could not download a browser.

That is usually a network or proxy issue rather than a problem with this
project. The simplest fix is to install Google Chrome normally, from
https://www.google.com/chrome/ — the scan will pick it up automatically, and
you can then re-run this script.

MSG
    exit 1
  fi
fi

# --- Run --------------------------------------------------------------------
# A visible browser is required twice over: headless mode is the single
# strongest signal Cloudflare's bot check looks for, and you cannot solve an
# eBay captcha in a window you can't see.
export GEMRATE_HEADFUL=1

say "Starting the scan. A Chrome window will open — leave it visible."
echo "When eBay shows a captcha, solve it in that window; the scan waits for you."
echo

if ! python -m scraper.scan "$@"; then
  cat <<'MSG'

The scan did not finish. The two usual causes:

  * "No cards found" — Cloudflare blocked the page. This has to run on a home
    internet connection; a VPN or corporate network can look like a datacenter
    and get blocked the same way. Turn the VPN off and try again.

  * A browser error — install Google Chrome from https://www.google.com/chrome/
    and re-run this script.

Re-run with --debug to see exactly where it stopped.
MSG
  exit 1
fi

say "Done. Open the summary:"
SPORT_ARG=""
for i in "$@"; do
  if [ "${PREV:-}" = "--sport" ]; then SPORT_ARG="$i"; fi
  PREV="$i"
done
SPORT="${SPORT_ARG:-basketball}"
echo "    open results/summary_${SPORT}.md"
