"""Enumerate the highest-volume Sets so slices are chosen by data, not taste.

Slices currently cover eight hand-picked sets, which means alerts are blind to
everything else. eBay reports a Set aspect distribution per query; asking for it
under each grader/grade tier surfaces the sets that actually carry volume.
"""
import json
from collections import defaultdict

from . import auth, config, query
from .client import EbayClient, browse_remaining


def set_distribution(client, cfg, aspect_extra, label):
    params = query.search_params(cfg, 0)
    base = params.get("aspect_filter") or f"categoryId:{cfg['category_ids'][0]}"
    params["aspect_filter"] = base + "," + aspect_extra
    params["limit"] = 1
    params["fieldgroups"] = "ASPECT_REFINEMENTS"
    resp = client.get(f"{config.API_HOST}/buy/browse/v1/item_summary/search",
                      params=params, allow_status=(400,))
    if resp.status_code != 200:
        print(f"[sets] {label}: REJECTED {resp.text[:120]}")
        return {}
    body = resp.json()
    out = {}
    for dist in (body.get("refinement", {}).get("aspectDistributions") or []):
        if dist.get("localizedAspectName") != "Set":
            continue
        for v in (dist.get("aspectValueDistributions") or []):
            name, count = v.get("localizedAspectValue"), v.get("matchCount") or 0
            if name:
                out[name] = count
    print(f"[sets] {label}: {len(out)} sets reported, total {body.get('total'):,}")
    return out


def main():
    cfg = config.load()
    cid, secret = config.credentials()
    token, _ = auth.get_token(cid, secret)
    client = EbayClient(token, cfg["marketplace_id"])
    rem, lim, _ = browse_remaining(client.rate_limits())
    print(f"[sets] quota {rem}/{lim}\n")

    graders = cfg.get("slice_graders") or {}
    combined = defaultdict(int)
    for tier in cfg.get("slice_grades") or []:
        value = graders.get(tier["grader"])
        if not value:
            continue
        label = f"{tier['grader']} {tier['grade']}"
        extra = (f"Professional Grader:{{{value}}},Grade:{{{tier['grade']}}}")
        for name, count in set_distribution(client, cfg, extra, label).items():
            combined[name] += count

    ranked = sorted(combined.items(), key=lambda kv: -kv[1])
    print(f"\n[sets] {len(ranked)} distinct sets seen across the grade tiers")
    print("[sets] top 60 by graded listing volume:")
    for i, (name, count) in enumerate(ranked[:60], 1):
        print(f"[sets]  {i:3}. {count:>8,}  {name}")

    out = config.DATA_DIR / "set_volumes.json"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"ranked": [{"set": n, "listings": c} for n, c in ranked]},
        indent=2, sort_keys=True) + "\n")
    print(f"\n[sets] wrote {out}")
    print(f"[sets] calls used: {client.call_count}")


if __name__ == "__main__":
    main()
