"""Find a working way to ask eBay whether a stored listing is still live.

getItems is 403 for this app, so the re-check pass needs another route.
Tests each candidate against real stored itemIds before anything is built on it.
"""
import glob
import json

from . import auth, config, query
from .client import EbayClient


def sample_stored(n=5):
    rows = []
    for path in sorted(glob.glob(str(config.DATA_DIR / "*.jsonl"))):
        with open(path) as fh:
            for line in fh:
                if line.strip():
                    rows.append(json.loads(line))
    rows.sort(key=lambda r: r.get("itemCreationDate") or "", reverse=True)
    return rows[:n]


def main():
    cfg = config.load()
    cid, secret = config.credentials()
    token, _ = auth.get_token(cid, secret)
    client = EbayClient(token, cfg["marketplace_id"])

    rows = sample_stored(5)
    print(f"[recheck] testing against {len(rows)} recently stored listings")
    for r in rows[:3]:
        print(f"[recheck]   {r['itemId']}  legacy={r.get('legacyItemId')}  "
              f"created={r.get('itemCreationDate')}")

    item_id = rows[0]["itemId"]
    legacy = rows[0].get("legacyItemId")

    print("\n########## A. ITEM DETAIL ENDPOINTS ##########")
    tests = [
        ("getItem", f"{config.API_HOST}/buy/browse/v1/item/{item_id}", None),
        ("getItems (bulk)", f"{config.API_HOST}/buy/browse/v1/item",
         {"item_ids": ",".join(r["itemId"] for r in rows[:2])}),
    ]
    if legacy:
        tests.append(("getItemByLegacyId",
                      f"{config.API_HOST}/buy/browse/v1/item/get_item_by_legacy_id",
                      {"legacy_item_id": legacy}))
    for label, url, params in tests:
        resp = client.get(url, params=params, allow_status=(400, 401, 403, 404, 500))
        print(f"[recheck] {label:22} HTTP {resp.status_code}"
              f"{'  <-- USABLE' if resp.status_code == 200 else ''}")
        if resp.status_code != 200:
            print(f"[recheck]     {resp.text[:160]}")

    print("\n########## A2. DOES getItem CARRY ASPECTS? ##########")
    for r in rows[:3]:
        resp = client.get(f"{config.API_HOST}/buy/browse/v1/item/{r['itemId']}",
                          allow_status=(400, 403, 404, 410))
        if resp.status_code != 200:
            print(f"[recheck] {r['itemId']} -> HTTP {resp.status_code}")
            continue
        item = resp.json()
        aspects = item.get("localizedAspects") or []
        names = [a.get("name") for a in aspects]
        print(f"[recheck] {r['itemId']}  keys={len(item)}  aspects={len(aspects)}")
        print(f"[recheck]   names: {names[:14]}")
        for want in ["Professional Grader", "Grade", "Certification Number",
                     "Set", "Player/Athlete", "Card Number", "Season"]:
            hit = next((a.get("value") for a in aspects if a.get("name") == want), None)
            print(f"[recheck]     {want:22} = {hit!r}")
        break

    print("\n########## A3. WHAT DOES A DEAD LISTING RETURN? ##########")
    bogus = "v1|100000000000|0"
    resp = client.get(f"{config.API_HOST}/buy/browse/v1/item/{bogus}",
                      allow_status=(400, 403, 404, 410, 500))
    print(f"[recheck] bogus itemId -> HTTP {resp.status_code}  {resp.text[:200]}")

    print("\n########## B. itemStartDate WINDOW SEARCH ##########")
    # If live listings can be enumerated by creation window, absence from a
    # re-sweep of that window means the listing ended.
    created = rows[0].get("itemCreationDate")
    if not created:
        print("[recheck] no creation date on sample; skipping")
        return
    day = created[:10]
    window = f"itemStartDate:[{day}T00:00:00.000Z]"
    params = query.search_params(cfg, 0)
    params["filter"] = params["filter"] + "," + window
    params["limit"] = 200
    resp = client.get(f"{config.API_HOST}/buy/browse/v1/item_summary/search",
                      params=params, allow_status=(400, 403))
    print(f"[recheck] filter {window}  -> HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"[recheck]   {resp.text[:300]}")
        return
    body = resp.json()
    returned = {i.get("itemId") for i in body.get("itemSummaries") or []}
    print(f"[recheck]   total={body.get('total'):,}  returned={len(returned)}")
    found = sum(1 for r in rows if r["itemId"] in returned)
    print(f"[recheck]   {found}/{len(rows)} of the sampled stored ids appear "
          f"in the first page of that window")

    print(f"\n[recheck] calls used: {client.call_count}")


if __name__ == "__main__":
    main()
