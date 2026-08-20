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

# eBay's token endpoint throttles by answering 401 invalid_client rather than
# 429. Observed live: a mint failed at 19:56 and the identical request
# succeeded at 19:59. Retry before treating it as a credential problem.
TOKEN_ATTEMPTS = 4
TOKEN_BACKOFF_SECONDS = [5, 20, 60]


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
    resp = None
    for attempt in range(TOKEN_ATTEMPTS):
        resp = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": SCOPE},
            timeout=30,
        )
        if resp.status_code == 200:
            if attempt:
                print(f"[auth] token minted on attempt {attempt + 1} "
                      f"(earlier attempts were throttled)")
            break
        transient = resp.status_code in (429, 500, 502, 503, 504) or (
            resp.status_code == 401 and "invalid_client" in resp.text)
        if not transient or attempt == TOKEN_ATTEMPTS - 1:
            break
        delay = TOKEN_BACKOFF_SECONDS[min(attempt, len(TOKEN_BACKOFF_SECONDS) - 1)]
        print(f"[auth] token request returned HTTP {resp.status_code}; "
              f"retrying in {delay}s (attempt {attempt + 1}/{TOKEN_ATTEMPTS})")
        time.sleep(delay)

    if resp.status_code != 200:
        detail = resp.text[:500]
        message = [f"[auth] token request failed after {TOKEN_ATTEMPTS} attempts: "
                   f"HTTP {resp.status_code} {detail}"]
        if resp.status_code in (400, 401) and "invalid_client" in detail:
            keyset_env = config.keyset_environment(client_id)
            message.append(
                "\neBay rejected the credential pair itself. In order of likelihood:\n"
                "  1. The Client ID and Client Secret come from DIFFERENT keysets. "
                "They must be the App ID and Cert ID of the SAME application.\n"
                "  2. EBAY_CLIENT_SECRET holds the wrong field. It must be the "
                "'Cert ID (Client Secret)' — not the Dev ID, not the Ru Name, and "
                "not the App ID again.\n"
                "  3. The keyset is for a different environment than "
                f"{config.ENV} ({config.API_HOST}).\n"
                "  4. The keyset has been regenerated or disabled in the eBay "
                "developer console, invalidating the stored secret.\n"
                f"\nDetected keyset environment from the App ID: "
                f"{keyset_env or 'UNRECOGNIZED'}. Target: {config.ENV}.\n"
                "Re-copy both values from the same row of "
                "https://developer.ebay.com/my/keys and update both secrets."
            )
        raise SystemExit("\n".join(message))
    payload = resp.json()
    token = payload["access_token"]
    expires_in = int(payload.get("expires_in", 7200))
    _write_cache(token, expires_in)
    print(f"[auth] minted new application token, expires_in={expires_in}s")
    return token, True
