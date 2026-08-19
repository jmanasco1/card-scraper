"""Thin eBay API client. Counts every HTTP call so the run can report quota use."""
import time

import requests

from . import config

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 4


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
        raise SystemExit(
            f"[client] request failed: HTTP {last.status_code} {url}\n{last.text[:1000]}"
        )

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
    def rate_limits(self):
        url = f"{config.APIZ_HOST}/developer/analytics/v1_beta/rate_limit"
        resp = self.get(url, params={"api_context": "buy", "api_name": "Browse"},
                        allow_status=(400, 403, 404))
        if resp.status_code != 200:
            print(f"[client] rate_limit unavailable: HTTP {resp.status_code} "
                  f"{resp.text[:300]}")
            return None
        return resp.json()


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
