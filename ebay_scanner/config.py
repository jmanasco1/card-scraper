"""Runtime configuration, loaded from config.json next to this module."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / ".cache"
TOKEN_CACHE = CACHE_DIR / "ebay_token.json"
CATEGORY_CACHE = DATA_DIR / "categories.json"

# eBay hosts. Analytics lives on apiz, everything else on api. Sandbox is a
# fully separate deployment — production keys do not authenticate against it,
# and sandbox keys do not authenticate against production.
ENV = os.environ.get("EBAY_ENV", "production").strip().lower()
if ENV == "sandbox":
    API_HOST = "https://api.sandbox.ebay.com"
    APIZ_HOST = "https://apiz.sandbox.ebay.com"
else:
    ENV = "production"
    API_HOST = "https://api.ebay.com"
    APIZ_HOST = "https://apiz.ebay.com"


def load():
    with open(Path(__file__).resolve().parent / "config.json") as fh:
        cfg = json.load(fh)
    # Env overrides so the workflow can tweak a run without a commit.
    if os.environ.get("SCANNER_MAX_PAGES"):
        cfg["max_pages"] = int(os.environ["SCANNER_MAX_PAGES"])
    return cfg


def keyset_environment(client_id):
    """eBay App IDs embed their environment as a -PRD- or -SBX- segment.

    Returns "production", "sandbox", or None when the ID does not follow the
    usual shape (which is itself worth reporting).
    """
    segments = client_id.split("-")
    if "PRD" in segments:
        return "production"
    if "SBX" in segments:
        return "sandbox"
    return None


def credentials():
    raw_id = os.environ.get("EBAY_CLIENT_ID", "")
    raw_secret = os.environ.get("EBAY_CLIENT_SECRET", "")
    cid, secret = raw_id.strip(), raw_secret.strip()
    if not cid or not secret:
        raise SystemExit(
            "Missing credentials. Set repository secrets EBAY_CLIENT_ID "
            "(eBay 'App ID (Client ID)') and EBAY_CLIENT_SECRET "
            "(eBay 'Cert ID (Client Secret)') from a PRODUCTION keyset."
        )

    # Surface the shape of the credentials without printing them. A mismatch
    # between the keyset environment and the host is the usual cause of a 401.
    keyset_env = keyset_environment(cid)
    print(f"[config] target environment: {ENV} ({API_HOST})")
    print(f"[config] client id: {len(cid)} chars, keyset environment "
          f"{keyset_env or 'UNRECOGNIZED (does not contain -PRD- or -SBX-)'}")
    print(f"[config] client secret: {len(secret)} chars")
    if raw_id != cid or raw_secret != raw_secret.strip():
        print("[config] note: stripped surrounding whitespace from a secret")

    if keyset_env and keyset_env != ENV:
        raise SystemExit(
            f"Credential/environment mismatch: EBAY_CLIENT_ID is a "
            f"{keyset_env.upper()} keyset but the run is targeting {ENV.upper()} "
            f"({API_HOST}). eBay will reject this with 'invalid_client'.\n"
            f"Fix: use a {ENV} keyset, or set EBAY_ENV={keyset_env} to target "
            f"the {keyset_env} host instead."
        )
    return cid, secret
