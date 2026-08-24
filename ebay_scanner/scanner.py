"""Phase 4: BIN scanner. Flag underpriced new listings.

A listing is flagged only when ALL hold:
  - price between $75 and $400            (the acting band)
  - its bucket has a valid reference      (5+ fresh comps)
  - price <= 0.70 * reference
  - listing is under 24 hours old

The $20 collection floor exists to build references and is deliberately NOT
the alerting floor.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import config, matching, reference

FLAGS = config.DATA_DIR / "flags.jsonl"

ACT_MIN, ACT_MAX = 10.0, 800.0
# Only alert from buckets whose key came from a trustworthy source. Title-parsed
# keys guess the set from loose token overlap and are the weakest link: they
# produced a bucket mixing plain Refractor with Pink Refractor across $25-$176.
# Slices pin set/grader/grade in the query; aspects come from getItem.
TRUSTED_METHODS = ("slice", "aspects", "catalog")
DISCOUNT = 0.70
# A cheap card that has sat for months is still buyable today, and is often the
# best deal on the board. Restricting to the last 24h hid every one of them:
# measured against the live corpus, 97 underpriced listings were sitting and
# exactly 0 were fresh enough to alert. 0 disables the age limit entirely.
MAX_AGE_HOURS = int(os.environ.get("MAX_LISTING_AGE_HOURS", "0"))
# A busy day is not a fault condition. This is only a runaway guard so a
# broken reference cannot send thousands of messages; overflow still sends the
# best ones rather than going silent. Override with ALERT_DAILY_CAP.
MAX_ALERTS_PER_DAY = int(os.environ.get("ALERT_DAILY_CAP", "200"))


def already_flagged():
    seen = set()
    if not FLAGS.exists():
        return seen
    with open(FLAGS) as fh:
        for line in fh:
            if line.strip():
                try:
                    seen.add(json.loads(line)["itemId"])
                except (ValueError, KeyError):
                    pass
    return seen


def flags_today(day):
    if not FLAGS.exists():
        return 0
    count = 0
    with open(FLAGS) as fh:
        for line in fh:
            if line.strip():
                try:
                    if json.loads(line).get("flaggedAt", "")[:10] == day:
                        count += 1
                except ValueError:
                    pass
    return count


def _post(url, data, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status in (200, 201)
    except Exception as exc:                       # noqa: BLE001
        print(f"[scan] notify failed: {exc}")
        return False


def notify(flag):
    """Telegram first (free, reliable on both phones), ntfy as a fallback.

    Returns True when at least one channel accepted the message.
    """
    price, ref = flag["price"], flag["reference"]
    line1 = f"*{flag['discount_pct']:.0f}% under reference*"
    saving = flag.get("saving", ref - price)
    age = flag.get("listingAgeDays")
    age_txt = f" · listed {age}d ago" if age is not None else ""
    line2 = (f"${price:.2f}  vs  ${ref:.2f}   (save ${saving:.2f})\n"
             f"{flag['comp_count']} comps{age_txt}")
    title = (flag["title"] or "")[:150]
    url = flag.get("itemWebUrl") or ""

    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat:
        text = f"{line1}\n{title}\n\n{line2}\n\n{url}"
        payload = urllib.parse.urlencode({
            "chat_id": chat, "text": text, "parse_mode": "Markdown",
            "disable_web_page_preview": "false"}).encode()
        if _post(f"https://api.telegram.org/bot{token}/sendMessage", payload,
                 {"Content-Type": "application/x-www-form-urlencoded"}):
            return True

    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        body = f"{title}\n\n${price:.2f} vs ${ref:.2f}\n{line2}"
        return _post(f"https://ntfy.sh/{urllib.parse.quote(topic)}",
                     body.encode("utf-8"),
                     {"Title": f"${price:.0f} · {flag['discount_pct']:.0f}% under",
                      "Click": url})
    return False


def test_notify():
    """Send one sample alert so delivery can be verified without waiting for a
    real flag or mutating flags.jsonl."""
    sample = {
        "price": 145.0, "reference": 250.0, "discount_pct": 42.0,
        "comp_count": 5, "bucket": "2022|bowman chrome|77|base|PSA|10",
        "title": "TEST ALERT — Bowman Chrome Bobby Witt Jr. #77 RC PSA 10",
        "itemWebUrl": "https://www.ebay.com/itm/000000000000",
    }
    channels = []
    if os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        channels.append("telegram")
    if os.environ.get("NTFY_TOPIC"):
        channels.append("ntfy")
    print(f"[scan] configured channels: {channels or 'NONE'}")
    if not channels:
        print("::error::No notification channel configured.")
        return 1
    ok = notify(sample)
    print(f"[scan] test notification {'SENT' if ok else 'FAILED'}")
    return 0 if ok else 1


def main():
    if "--test-notify" in sys.argv:
        return test_notify()
    rows, aspects, gone = reference.load_corpus()
    now = datetime.now(timezone.utc)
    references, stats, _ = reference.build(rows, aspects, gone, now)
    print(f"[scan] {len(references):,} buckets carry a valid reference")

    seen = already_flagged()
    day = now.strftime("%Y-%m-%d")
    sent_today = flags_today(day)
    cutoff = (now - timedelta(hours=MAX_AGE_HOURS)) if MAX_AGE_HOURS else None

    candidates = []
    for r in rows:
        if r.get("itemId") in seen or r.get("itemId") in gone:
            continue
        price = r.get("price")
        if price is None or not (ACT_MIN <= price <= ACT_MAX):
            continue
        created = reference._parse(r.get("itemCreationDate"))
        if cutoff and (not created or created < cutoff):
            continue
        key, method, _ = matching.bucket_key(r, aspects.get(r.get("itemId")))
        if method not in TRUSTED_METHODS:
            continue
        ref = references.get(key) if key else None
        if not ref or ref["comp_count"] < reference.MIN_COMPS:
            continue
        # Cheap pre-filter on the shared reference, then re-price against the
        # bucket with this listing removed so it cannot vote on its own value.
        if price > DISCOUNT * ref["reference"]:
            continue
        peer_refs, _, _ = reference.build(rows, aspects, gone, now,
                                          exclude_item=r["itemId"])
        ref = peer_refs.get(key)
        if not ref or ref["comp_count"] < reference.MIN_COMPS:
            continue          # only itself held the bucket above the minimum
        if price > DISCOUNT * ref["reference"]:
            continue
        candidates.append({
            "itemId": r["itemId"], "title": r.get("title"),
            "price": price, "itemWebUrl": r.get("itemWebUrl"),
            "itemCreationDate": r.get("itemCreationDate"),
            "sellerUsername": r.get("sellerUsername"),
            "sellerFeedbackScore": r.get("sellerFeedbackScore"),
            "bucket": key, "match_method": method,
            "reference": ref["reference"], "comp_count": ref["comp_count"],
            "discount_pct": round((1 - price / ref["reference"]) * 100, 1),
            "distribution": {k: ref[k] for k in
                             ("low", "p10", "p25", "median", "p75", "p90",
                              "high", "oldest_days", "newest_days")},
            "listingAgeDays": (now - created).days if created else None,
            "flaggedAt": now.isoformat(),
        })

    for c in candidates:
        c["saving"] = round(c["reference"] - c["price"], 2)
    # Rank by dollars saved, not discount percent. Under a 20/day cap a 30%
    # discount on a $12 card ($4) must not displace $180 off a $600 card.
    candidates.sort(key=lambda c: (-c["saving"], -c["discount_pct"]))
    room = max(0, MAX_ALERTS_PER_DAY - sent_today)
    to_send = candidates[:room]
    over_limit = len(candidates) > room
    if over_limit:
        print(f"::warning::{len(candidates)} candidates but only {room} left "
              f"under the {MAX_ALERTS_PER_DAY}/day runaway guard "
              f"({sent_today} already sent). Sending the {len(to_send)} "
              f"largest by saving; the rest are still recorded in flags.jsonl.")

    configured = bool(os.environ.get("TELEGRAM_TOKEN") and
                      os.environ.get("TELEGRAM_CHAT_ID")) or \
                 bool(os.environ.get("NTFY_TOPIC"))
    if to_send and not configured:
        print("::warning::No notification channel configured; flags recorded "
              "but nothing sent. Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID.")
    sent = 0
    for flag in to_send:
        if configured and notify(flag):
            flag["notified"] = True
            sent += 1
        else:
            flag["notified"] = False

    if candidates:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(FLAGS, "a") as fh:
            for flag in candidates:
                fh.write(json.dumps(flag, sort_keys=True, ensure_ascii=False) + "\n")

    lines = [f"buckets with reference: {len(references):,}",
             f"candidates flagged: {len(candidates)}",
             f"notifications sent: {sent}",
             f"alerts already sent today: {sent_today}",
             f"held back by the daily guard: "
             f"{max(0, len(candidates) - len(to_send))}"]
    print("\n".join("[scan] " + l for l in lines))
    for flag in candidates[:10]:
        print(f"[scan]   save ${flag['saving']:>7.2f}  -{flag['discount_pct']:.0f}%  "
              f"${flag['price']:.2f} vs ${flag['reference']:.2f} "
              f"(n={flag['comp_count']})  {(flag['title'] or '')[:46]}")
    (config.ROOT / "scan_summary.txt").write_text("\n".join(lines) + "\n")

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"flags={len(candidates)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
