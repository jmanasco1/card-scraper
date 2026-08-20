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
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import config, matching, reference

FLAGS = config.DATA_DIR / "flags.jsonl"

ACT_MIN, ACT_MAX = 75.0, 400.0
DISCOUNT = 0.70
MAX_AGE_HOURS = 24
MAX_ALERTS_PER_DAY = 20


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
    line2 = (f"${price:.2f}  vs  ${ref:.2f}\n"
             f"{flag['comp_count']} comps · {flag['bucket']}")
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


def main():
    rows, aspects, gone = reference.load_corpus()
    now = datetime.now(timezone.utc)
    references, stats, _ = reference.build(rows, aspects, gone, now)
    print(f"[scan] {len(references):,} buckets carry a valid reference")

    seen = already_flagged()
    day = now.strftime("%Y-%m-%d")
    sent_today = flags_today(day)
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)

    candidates = []
    for r in rows:
        if r.get("itemId") in seen or r.get("itemId") in gone:
            continue
        price = r.get("price")
        if price is None or not (ACT_MIN <= price <= ACT_MAX):
            continue
        created = reference._parse(r.get("itemCreationDate"))
        if not created or created < cutoff:
            continue
        key, method, _ = matching.bucket_key(r, aspects.get(r.get("itemId")))
        ref = references.get(key) if key else None
        if not ref or ref["comp_count"] < reference.MIN_COMPS:
            continue
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
            "flaggedAt": now.isoformat(),
        })

    candidates.sort(key=lambda c: -c["discount_pct"])
    room = MAX_ALERTS_PER_DAY - sent_today
    over_limit = len(candidates) > room

    if over_limit:
        print(f"::warning::{len(candidates)} candidates exceeds the "
              f"{MAX_ALERTS_PER_DAY}/day alert cap ({sent_today} already sent "
              f"today). Reference logic is probably wrong — recording them but "
              f"sending nothing.")
        to_send = []
    else:
        to_send = candidates[:max(0, room)]

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
             f"cap exceeded (suppressed): {over_limit}"]
    print("\n".join("[scan] " + l for l in lines))
    for flag in candidates[:10]:
        print(f"[scan]   -{flag['discount_pct']:.0f}%  ${flag['price']:.2f} vs "
              f"${flag['reference']:.2f} (n={flag['comp_count']})  "
              f"{(flag['title'] or '')[:58]}")
    (config.ROOT / "scan_summary.txt").write_text("\n".join(lines) + "\n")

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"flags={len(candidates)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
