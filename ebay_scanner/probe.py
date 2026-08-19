"""Live-endpoint probe. Dumps real response shapes so nothing is assumed from docs.

Run this before trusting the collector: it answers, against production eBay,
(1) what category 261328 actually is, (2) whether item_summary/search returns
itemCreationDate and localizedAspects, (3) which aspect names graded cards
really use, (4) whether bulk getItems works, (5) the getRateLimits shape.
"""
import json

from . import auth, config, fields, query
from .client import EbayClient, browse_remaining


def _dump(label, obj, limit=4000):
    text = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
    if len(text) > limit:
        text = text[:limit] + f"\n... [truncated, {len(text)} chars total]"
    print(f"\n===== {label} =====\n{text}")


def main():
    cfg = config.load()
    cid, secret = config.credentials()
    token, _ = auth.get_token(cid, secret)
    client = EbayClient(token, cfg["marketplace_id"])

    print("\n########## 1. RATE LIMITS ##########")
    limits = client.rate_limits()
    _dump("getRateLimits raw", limits, limit=6000)
    remaining, limit, reset = browse_remaining(limits)
    print(f"[probe] parsed Browse remaining={remaining} limit={limit} reset={reset}")

    print("\n########## 2. TAXONOMY ##########")
    tree_id = client.default_category_tree_id()
    print(f"[probe] default category tree id = {tree_id}")
    for category_id in ["261328"]:
        resp = client.category_subtree(tree_id, category_id)
        print(f"[probe] subtree({category_id}) -> HTTP {resp.status_code}")
        if resp.status_code == 200:
            node = resp.json().get("categorySubtreeNode", {})
            print(f"[probe]   name = {node.get('category', {}).get('categoryName')!r}")
            print(f"[probe]   leaf = {node.get('leafCategoryTreeNode')}")
            children = node.get("childCategoryTreeNodes") or []
            print(f"[probe]   children = {len(children)}")
            for child in children[:15]:
                cat = child.get("category", {})
                print(f"[probe]     {cat.get('categoryId')} {cat.get('categoryName')!r}")
        else:
            print(resp.text[:800])

    print("\n########## 3. SEARCH (no aspect filter) ##########")
    plain = dict(query.search_params(cfg, 0))
    plain.pop("aspect_filter", None)
    plain["limit"] = 3
    plain["fieldgroups"] = "ASPECT_REFINEMENTS,MATCHING_ITEMS"
    print(f"[probe] params = {json.dumps(plain)}")
    body = client.search(plain)
    print(f"[probe] top-level keys = {sorted(body.keys())}")
    print(f"[probe] total = {body.get('total')}")

    items = body.get("itemSummaries") or []
    if items:
        print(f"[probe] item_summary keys = {sorted(items[0].keys())}")
        print(f"[probe] has itemCreationDate = {'itemCreationDate' in items[0]}")
        print(f"[probe] has localizedAspects = {'localizedAspects' in items[0]}")
        _dump("first item_summary", items[0])
    else:
        print("[probe] NO ITEMS RETURNED — check filter/category")

    refinement = body.get("refinement") or {}
    print(f"[probe] refinement keys = {sorted(refinement.keys())}")
    for dist in (refinement.get("aspectDistributions") or [])[:25]:
        values = [v.get("localizedAspectValue") for v in (dist.get("aspectValueDistributions") or [])[:6]]
        print(f"[probe] ASPECT {dist.get('localizedAspectName')!r} -> {values}")

    print("\n########## 4. SEARCH (with Graded aspect filter) ##########")
    graded = dict(query.search_params(cfg, 0))
    graded["limit"] = 3
    print(f"[probe] params = {json.dumps(graded)}")
    try:
        graded_body = client.search(graded)
        print(f"[probe] graded total = {graded_body.get('total')}")
        print(f"[probe] graded returned = {len(graded_body.get('itemSummaries') or [])}")
    except SystemExit as exc:
        print(f"[probe] GRADED FILTER REJECTED: {exc}")
        graded_body = None

    print("\n########## 5. BULK getItems ##########")
    sample_ids = [i["itemId"] for i in items[:3]]
    if sample_ids:
        detail_body = client.get_items(sample_ids)
        print(f"[probe] getItems top-level keys = {sorted(detail_body.keys())}")
        detail_items = detail_body.get("items") or []
        print(f"[probe] returned {len(detail_items)} of {len(sample_ids)} requested")
        if detail_body.get("warnings"):
            _dump("getItems warnings", detail_body["warnings"], limit=1500)
        if detail_items:
            first = detail_items[0]
            print(f"[probe] item detail keys = {sorted(first.keys())}")
            print(f"[probe] has localizedAspects = {'localizedAspects' in first}")
            _dump("localizedAspects", first.get("localizedAspects"), limit=3000)
            record = fields.build_record(items[0], first)
            _dump("BUILT RECORD", record, limit=3000)

    print(f"\n[probe] total API calls used: {client.call_count}")


if __name__ == "__main__":
    main()
