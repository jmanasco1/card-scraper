"""Flatten an eBay item into the stored record shape."""
from datetime import datetime, timezone

# Aspect names vary across listings and sellers, so each stored column is
# resolved from a list of candidates in priority order. The raw aspects blob is
# always kept alongside, which is what lets you measure how often these miss.
ASPECT_COLUMNS = {
    "grader": ["Professional Grader", "Grader", "Grading Company"],
    "grade": ["Grade", "Card Condition", "Numerical Grade"],
    "cert_number": ["Certification Number", "Cert Number", "Serial Number"],
    "season": ["Season", "Year", "Year Manufactured"],
    "set_name": ["Set", "Card Set", "Insert Set"],
    "player": ["Player/Athlete", "Player", "Athlete", "Subject"],
    "card_number": ["Card Number", "Card #"],
}


def aspects_to_dict(localized_aspects):
    """localizedAspects is a list of {type,name,value}; collapse to name -> value.

    Repeated names are joined rather than dropped, since multi-value aspects
    (multiple players on one card) are real and losing them would be silent.
    """
    out = {}
    for aspect in localized_aspects or []:
        name = aspect.get("name")
        value = aspect.get("value")
        if not name or value is None:
            continue
        if name in out:
            out[name] = f"{out[name]} | {value}"
        else:
            out[name] = value
    return out


def _pick(aspects, candidates):
    for name in candidates:
        if name in aspects:
            return aspects[name], name
    return None, None


def build_record(summary, detail=None, first_seen=None):
    """Merge a search summary with optional item detail into one flat record."""
    item = detail or {}
    price = (item.get("price") or summary.get("price") or {})
    seller = (item.get("seller") or summary.get("seller") or {})
    image = (item.get("image") or summary.get("image") or {})

    aspects = aspects_to_dict(item.get("localizedAspects"))
    record = {
        "itemId": summary.get("itemId") or item.get("itemId"),
        "legacyItemId": item.get("legacyItemId") or summary.get("legacyItemId"),
        "title": item.get("title") or summary.get("title"),
        "price": _to_float(price.get("value")),
        "currency": price.get("currency"),
        "itemWebUrl": item.get("itemWebUrl") or summary.get("itemWebUrl"),
        "itemCreationDate": item.get("itemCreationDate") or summary.get("itemCreationDate"),
        "sellerUsername": seller.get("username"),
        "sellerFeedbackScore": seller.get("feedbackScore"),
        "sellerFeedbackPercentage": seller.get("feedbackPercentage"),
        "condition": item.get("condition") or summary.get("condition"),
        "conditionId": item.get("conditionId") or summary.get("conditionId"),
        "epid": summary.get("epid") or item.get("epid"),
        "imageUrl": image.get("imageUrl"),
        "categoryId": summary.get("leafCategoryIds", [None])[0]
        if summary.get("leafCategoryIds") else item.get("categoryId"),
        "buyingOptions": summary.get("buyingOptions") or item.get("buyingOptions"),
        "firstSeenAt": first_seen or datetime.now(timezone.utc).isoformat(),
        "detailFetched": bool(detail),
    }

    for column, candidates in ASPECT_COLUMNS.items():
        value, matched = _pick(aspects, candidates)
        record[column] = value
        record[f"{column}_aspect"] = matched

    # Raw blob, kept verbatim for missingness analysis.
    record["aspects"] = aspects
    record["aspectCount"] = len(aspects)
    return record


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
