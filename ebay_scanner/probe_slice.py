"""Find a dense, enrichable slice: exact aspect values and live volumes."""
import json
from . import auth, config, query
from .client import EbayClient, browse_remaining


def total(client, cfg, aspect_extra=None, q=None, label=""):
    params = query.search_params(cfg, 0)
    base = params.get("aspect_filter", f"categoryId:{cfg['category_ids'][0]}")
    if aspect_extra:
        params["aspect_filter"] = base + "," + aspect_extra
    if q:
        params["q"] = q
    params["limit"] = 1
    resp = client.get(f"{config.API_HOST}/buy/browse/v1/item_summary/search",
                      params=params, allow_status=(400,))
    if resp.status_code != 200:
        print(f"[slice] {label:52} REJECTED {resp.text[:110]}")
        return None
    n = resp.json().get("total")
    print(f"[slice] {label:52} {n:>9,}")
    return n


def main():
    cfg = config.load()
    cid, secret = config.credentials()
    token, _ = auth.get_token(cid, secret)
    client = EbayClient(token, cfg["marketplace_id"])
    rem, lim, _ = browse_remaining(client.rate_limits())
    print(f"[slice] quota {rem}/{lim}\n")

    print("########## ASPECT VALUES AVAILABLE ##########")
    params = query.search_params(cfg, 0)
    params["limit"] = 1
    params["fieldgroups"] = "ASPECT_REFINEMENTS"
    body = client.search(params)
    for dist in (body.get("refinement", {}).get("aspectDistributions") or []):
        name = dist.get("localizedAspectName")
        if name in ("Professional Grader", "Grade", "Set", "Sport", "Season"):
            vals = [(v.get("localizedAspectValue"), v.get("matchCount"))
                    for v in (dist.get("aspectValueDistributions") or [])[:8]]
            print(f"[slice] {name}:")
            for v, c in vals:
                print(f"[slice]     {c:>9,}  {v}")

    print("\n########## CANDIDATE SLICE VOLUMES ##########")
    total(client, cfg, label="baseline: category + $20-400 + graded")
    psa10 = "Professional Grader:{Professional Sports Authenticator (PSA)},Grade:{10}"
    total(client, cfg, aspect_extra=psa10, label="PSA 10")
    for term in ["Prizm", "Panini Prizm", "Topps Chrome", "Bowman Chrome",
                 "Donruss Optic", "Select"]:
        total(client, cfg, aspect_extra=psa10, q=term, label=f"PSA 10 + q={term!r}")
    print(f"\n[slice] calls used: {client.call_count}")


if __name__ == "__main__":
    main()
