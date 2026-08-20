"""Thin eBay API client. Counts every HTTP call so the run can report quota use."""
import time

import requests

from . import config

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 4


class EbayApiError(RuntimeError):
    """A non-retryable API failure, carrying the status so callers can react.

    Some eBay endpoints are gated behind access grants the application may not
    hold; those come back 403 and should degrade the run, not kill it.
    """

    def __init__(self, status, url, body):
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} {url}\n{body[:600]}")


class EbayClient:
    def __init__(self, token, marketplace_id):
        self.token = token
        self.marketplace_id = marketplace_id
        self.call_count = 0
        self.session = requests.Session()

    def _headers(self, extra=None):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace_id,
            "Accept": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    def get(self, url, params=None, headers=None, allow_status=()):
        """GET with backoff on transient failures. Counts against the daily quota."""
        delay = 2
        last = None
        for attempt in range(MAX_RETRIES):
            self.call_count += 1
            resp = self.session.get(
                url, params=params, headers=self._headers(headers), timeout=45
            )
            last = resp
            if resp.status_code == 200 or resp.status_code in allow_status:
                return resp
            if resp.status_code not in RETRY_STATUSES:
                break
            print(
                f"[client] HTTP {resp.status_code} on {url} "
                f"(attempt {attempt + 1}/{MAX_RETRIES}), retrying in {delay}s"
            )
            time.sleep(delay)
            delay *= 2
        raise EbayApiError(last.status_code, url, last.text)

    # --- Taxonomy -------------------------------------------------------
    def default_category_tree_id(self):
        url = f"{config.API_HOST}/commerce/taxonomy/v1/get_default_category_tree_id"
        resp = self.get(url, params={"marketplace_id": self.marketplace_id})
        return resp.json()["categoryTreeId"]

    def category_subtree(self, tree_id, category_id):
        url = f"{config.API_HOST}/commerce/taxonomy/v1/category_tree/{tree_id}/get_category_subtree"
        resp = self.get(url, params={"category_id": category_id}, allow_status=(400, 404))
        return resp

    # --- Browse ---------------------------------------------------------
    def search(self, params):
        url = f"{config.API_HOST}/buy/browse/v1/item_summary/search"
        # contextualLocation keeps results consistent with a US buyer's view.
        headers = {"X-EBAY-C-ENDUSERCTX": "contextualLocation=country=US"}
        return self.get(url, params=params, headers=headers).json()

    def get_items(self, item_ids):
        """Bulk item detail (max 20 ids). This is where localizedAspects live."""
        url = f"{config.API_HOST}/buy/browse/v1/item"
        resp = self.get(
            url, params={"item_ids": ",".join(item_ids)}, allow_status=(207,)
        )
        return resp.json()

    # --- Developer Analytics --------------------------------------------
    def rate_limit_candidates(self):
        """Host/param combinations to try for getRateLimits.

        The documented host did not resolve against the live API, so this walks
        the plausible variants rather than hardcoding one and failing blind.
        """
        path = "/developer/analytics/v1_beta/rate_limit"
        for host in (config.API_HOST, config.APIZ_HOST):
            yield f"{host}{path}", None
            yield f"{host}{path}", {"api_context": "buy", "api_name": "Browse"}

    def rate_limits(self):
        for url, params in self.rate_limit_candidates():
            resp = self.get(url, params=params,
                            allow_status=(400, 401, 403, 404))
            if resp.status_code == 200:
                print(f"[client] rate limits from {url} params={params}")
                return resp.json()
            print(f"[client] rate_limit {url} params={params} -> "
                  f"HTTP {resp.status_code}")
        print("::warning::getRateLimits did not resolve on any known host; "
              "continuing without a quota guard.")
        return None


def browse_remaining(payload):
    """Pull the Browse API's remaining daily call count out of a getRateLimits body.

    Returns (remaining, limit, reset) or (None, None, None) when the shape does
    not contain a Browse entry — verified against the live response, not docs.
    """
    if not payload:
        return None, None, None
    best = None
    for group in payload.get("rateLimits", []):
        if (group.get("apiName") or "").lower() != "browse":
            continue
        for resource in group.get("resources", []):
            for rate in resource.get("rates", []):
                remaining = rate.get("remaining")
                if remaining is None:
                    continue
                if best is None or remaining < best[0]:
                    best = (remaining, rate.get("limit"), rate.get("reset"))
    return best if best else (None, None, None)
