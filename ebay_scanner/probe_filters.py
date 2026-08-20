"""Measure the effect of the graded filters and map the category's neighbourhood.

Answers two questions with live data rather than assumption:
  1. How much does each "graded" filter actually exclude, and is the exclusion
     biased toward sellers who fill in item aspects?
  2. What sits around category 261328 in the tree — parents and siblings.
"""
import json
from collections import Counter

from . import auth, config, query
from .client import EbayClient


def total_for(client, cfg, extra_filter=None, aspect=False, label=""):
    params = query.search_params(cfg, 0)
    if not aspect:
        params.pop("aspect_filter", None)
    if extra_filter:
        params["filter"] = params["filter"] + "," + extra_filter
    params["limit"] = 50
    body = client.search(params)
    total = body.get("total")
    items = body.get("itemSummaries") or []
    print(f"[filters] {label:44} total={total:>10,}  sampled={len(items)}")
    return total, items


def main():
    cfg = config.load()
    cid, secret = config.credentials()
    token, _ = auth.get_token(cid, secret)
    client = EbayClient(token, cfg["marketplace_id"])

    print("\n########## FILTER COMPARISON ##########")
    base_total, base_items = total_for(client, cfg, label="A: category+price+fixedprice (no graded filter)")
    asp_total, asp_items = total_for(client, cfg, aspect=True, label="B: + aspect_filter Graded:{Yes}")
    cond_total, cond_items = total_for(client, cfg, extra_filter="conditionIds:{2750}",
                                       label="C: + conditionIds:{2750}")
    cond_asp = None
    try:
        cond_asp, _ = total_for(client, cfg, extra_filter="conditionIds:{2750}", aspect=True,
                                label="D: + conditionIds:{2750} AND aspect Graded")
    except SystemExit as exc:
        print(f"[filters] D rejected: {exc}")

    if base_total:
        for name, val in [("B aspect", asp_total), ("C condition", cond_total), ("D both", cond_asp)]:
            if val is not None:
                print(f"[filters] {name:14} keeps {val/base_total*100:5.1f}% of A")

    print("\n########## CONDITION MIX IN UNFILTERED SAMPLE ##########")
    conds = Counter((i.get("condition"), i.get("conditionId")) for i in base_items)
    for (name, cid_), n in conds.most_common():
        print(f"[cond] {str(name):24} id={cid_:<6} {n:3}/{len(base_items)}")

    print("\n########## CATEGORY ANCESTRY (from a live item) ##########")
    if base_items:
        sample = base_items[0]
        print(f"[cat] leafCategoryIds = {sample.get('leafCategoryIds')}")
        cats = sample.get("categories")
        if cats:
            for c in cats:
                print(f"[cat]   {c.get('categoryId'):>8}  {c.get('categoryName')}")
        else:
            print("[cat] no 'categories' array on item_summary")

    print("\n########## SIBLINGS ##########")
    tree_id = client.default_category_tree_id()
    # Walk each ancestor found above and list its immediate children.
    parents = []
    if base_items and base_items[0].get("categories"):
        parents = [c["categoryId"] for c in base_items[0]["categories"]
                   if c["categoryId"] != "261328"]
    for pid in parents:
        resp = client.category_subtree(tree_id, pid)
        if resp.status_code != 200:
            print(f"[sib] {pid}: HTTP {resp.status_code}")
            continue
        node = resp.json().get("categorySubtreeNode", {})
        name = node.get("category", {}).get("categoryName")
        kids = node.get("childCategoryTreeNodes") or []
        print(f"\n[sib] parent {pid} = {name!r} — {len(kids)} children:")
        for k in kids:
            kc = k.get("category", {})
            print(f"[sib]    {kc.get('categoryId'):>8}  {kc.get('categoryName')}"
                  f"{'   <-- currently scanned' if kc.get('categoryId')=='261328' else ''}")

    print(f"\n[filters] calls used: {client.call_count}")


if __name__ == "__main__":
    main()
