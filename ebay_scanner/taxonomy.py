"""Verify category IDs against the live Taxonomy API instead of trusting a guess."""
import json
import time

from . import config


def _cache_fresh(cfg):
    try:
        with open(config.CATEGORY_CACHE) as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        return None
    age_days = (time.time() - cached.get("verifiedAtEpoch", 0)) / 86400
    if age_days > cfg.get("category_cache_days", 7):
        return None
    if cached.get("categoryIds") != cfg["category_ids"]:
        return None
    return cached


def verify(client, cfg, force=False):
    """Confirm each configured category exists and record its real name.

    Cached to data/categories.json so this costs no calls on most runs.
    """
    if not force:
        cached = _cache_fresh(cfg)
        if cached:
            print("[taxonomy] using cached category verification")
            return cached

    tree_id = client.default_category_tree_id()
    print(f"[taxonomy] default category tree for {cfg['marketplace_id']}: {tree_id}")

    verified = []
    for category_id in cfg["category_ids"]:
        resp = client.category_subtree(tree_id, category_id)
        if resp.status_code != 200:
            raise SystemExit(
                f"[taxonomy] category {category_id} did not resolve: "
                f"HTTP {resp.status_code} {resp.text[:400]}"
            )
        node = resp.json().get("categorySubtreeNode", {})
        category = node.get("category", {})
        children = [
            child.get("category", {}).get("categoryName")
            for child in node.get("childCategoryTreeNodes", []) or []
        ]
        entry = {
            "categoryId": category.get("categoryId"),
            "categoryName": category.get("categoryName"),
            "leafCategory": node.get("leafCategoryTreeNode", False),
            "childCount": len(children),
            "sampleChildren": children[:10],
        }
        print(
            f"[taxonomy] {entry['categoryId']} = {entry['categoryName']!r} "
            f"(leaf={entry['leafCategory']}, children={entry['childCount']})"
        )
        verified.append(entry)

    result = {
        "categoryTreeId": tree_id,
        "marketplaceId": cfg["marketplace_id"],
        "categoryIds": cfg["category_ids"],
        "categories": verified,
        "verifiedAtEpoch": int(time.time()),
    }
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(config.CATEGORY_CACHE, "w") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return result
