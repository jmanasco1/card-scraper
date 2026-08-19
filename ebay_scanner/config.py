"""Runtime configuration, loaded from config.json next to this module."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / ".cache"
TOKEN_CACHE = CACHE_DIR / "ebay_token.json"
CATEGORY_CACHE = DATA_DIR / "categories.json"

# eBay hosts. Analytics lives on apiz, everything else on api.
API_HOST = "https://api.ebay.com"
APIZ_HOST = "https://apiz.ebay.com"


def load():
    with open(Path(__file__).resolve().parent / "config.json") as fh:
        cfg = json.load(fh)
    # Env overrides so the workflow can tweak a run without a commit.
    if os.environ.get("SCANNER_MAX_PAGES"):
        cfg["max_pages"] = int(os.environ["SCANNER_MAX_PAGES"])
    return cfg


def credentials():
    cid = os.environ.get("EBAY_CLIENT_ID", "").strip()
    secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        raise SystemExit(
            "Missing credentials. Set repository secrets EBAY_CLIENT_ID "
            "(eBay 'App ID (Client ID)') and EBAY_CLIENT_SECRET "
            "(eBay 'Cert ID (Client Secret)') from a PRODUCTION keyset."
        )
    return cid, secret
