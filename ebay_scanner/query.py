"""Build the Browse API search parameters from config."""


def build_filter(cfg):
    parts = [
        f"price:[{cfg['price_min']}..{cfg['price_max']}]",
        f"priceCurrency:{cfg['currency']}",
    ]
    if cfg.get("buying_options"):
        parts.append("buyingOptions:{%s}" % "|".join(cfg["buying_options"]))
    return ",".join(parts)


def build_aspect_filter(cfg, category_id):
    """aspect_filter must lead with categoryId or eBay rejects it."""
    spec = cfg.get("aspect_filter") or {}
    if not spec:
        return None
    parts = [f"categoryId:{category_id}"]
    for name, values in spec.items():
        parts.append("%s:{%s}" % (name, "|".join(values)))
    return ",".join(parts)


def search_params(cfg, offset):
    category_id = cfg["category_ids"][0]
    params = {
        "category_ids": ",".join(cfg["category_ids"]),
        "filter": build_filter(cfg),
        "sort": cfg["sort"],
        "limit": cfg["limit"],
        "offset": offset,
    }
    aspect = build_aspect_filter(cfg, category_id)
    if aspect:
        params["aspect_filter"] = aspect
    return params
