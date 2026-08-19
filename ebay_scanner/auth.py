"""Client-credentials OAuth. Application token, cached to disk between runs."""
import base64
import json
import time

import requests

from . import config

TOKEN_URL = f"{config.API_HOST}/identity/v1/oauth2/token"
SCOPE = "https://api.ebay.com/oauth/api_scope"

# Refresh a little early so a token can't expire mid-run.
EXPIRY_MARGIN_SECONDS = 300


def _read_cache():
    try:
        with open(config.TOKEN_CACHE) as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        return None
    if cached.get("expires_at", 0) - EXPIRY_MARGIN_SECONDS <= time.time():
        return None
    return cached.get("access_token")


def _write_cache(token, expires_in):
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.TOKEN_CACHE.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump({"access_token": token, "expires_at": time.time() + expires_in}, fh)
    tmp.replace(config.TOKEN_CACHE)
    config.TOKEN_CACHE.chmod(0o600)


def get_token(client_id, client_secret, force=False):
    """Return an application access token, reusing the cached one when valid."""
    if not force:
        cached = _read_cache()
        if cached:
            print("[auth] using cached application token")
            return cached, False

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials", "scope": SCOPE},
        timeout=30,
    )
    if resp.status_code != 200:
        raise SystemExit(
            f"[auth] token request failed: HTTP {resp.status_code} {resp.text[:500]}"
        )
    payload = resp.json()
    token = payload["access_token"]
    expires_in = int(payload.get("expires_in", 7200))
    _write_cache(token, expires_in)
    print(f"[auth] minted new application token, expires_in={expires_in}s")
    return token, True
