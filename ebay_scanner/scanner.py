"""Phase 4: BIN scanner. Flag underpriced new listings.

A listing is flagged only when ALL hold:
  - price between $75 and $400            (the acting band)
  - its bucket has a valid reference      (5+ fresh comps)
  - price <= 0.70 * reference
  - listing is under 24 hours old

The $20 collection floor exists to build references and is deliberately NOT
the alerting floor.
"""
import collections
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import auth, config, matching, reference, verify
from .client import EbayClient, browse_remaining

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

# Live verification costs two calls per candidate (one search, one getItem)
# against a 5,000/day ceiling, so it is budgeted. Candidates past the budget are
# recorded but never sent: an unverified alert is what the field reports were
# complaining about.
# 15 candidates costs 30 calls a run. The collection cron fires 96 times a day,
# so a permanently saturated budget would want ~2,900 calls against a 5,000/day
# ceiling that collection, re-check, enrichment and backfill already draw on.
# That ceiling is enforced by the quota floor below rather than by this number:
# saturation only happens while a backlog is draining, and the floor makes the
# run stand down before collection is ever squeezed.
VERIFY_MAX_CANDIDATES = int(os.environ.get("VERIFY_MAX_CANDIDATES", "15"))

# Collection is the one job that cannot be caught up later: an uncollected
# listing is gone for good, while an unverified candidate simply waits for the
# next run. So verification reads the live quota first and stands down below
# this floor, the same contract enrichment already follows.
VERIFY_MIN_QUOTA = int(os.environ.get("VERIFY_MIN_QUOTA", "1200"))
CALLS_PER_VERIFY = 2


def already_flagged():
    """Item ids already alerted on, so the same listing is not sent twice.

    Only a notified flag suppresses. A candidate dropped by live verification -
    because a cheaper copy was listed, or comps were too thin - must stay
    eligible: those conditions change when the cheaper copy sells, and
    blacklisting the listing would lose the deal. A survivor whose notification
    failed stays eligible too, so a channel outage delays an alert rather than
    swallowing it.
    """
    seen = set()
    if not FLAGS.exists():
        return seen
    with open(FLAGS) as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("notified") and row.get("itemId"):
                seen.add(row["itemId"])
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


def verify_candidates(candidates):
    """Drop every candidate the live market does not back.

    The corpus is a 45-day accumulation with no liveness guarantee, so a bucket
    can show 27 comps when most have sold. Three field-reported failures came
    straight from trusting it: alerts on listings that had already sold, on
    cards whose only rival was priced higher, and on cards that were not the
    cheapest live BIN. Each survivor here has been re-checked against what is
    actually on sale right now.

    A verification that errors drops the candidate rather than passing it. The
    whole point is that unverified is not good enough to alert on.
    """
    if not candidates:
        return []
    cfg = config.load()
    try:
        cid, secret = config.credentials()
        token, _ = auth.get_token(cid, secret)
        client = EbayClient(token, cfg["marketplace_id"])
        index = verify.slice_index(cfg)
    except Exception as exc:                              # noqa: BLE001
        print(f"::warning::live verification unavailable ({exc}); "
              "no alerts will be sent this run")
        for c in candidates:
            c["verification"] = "unavailable"
        return []

    budget = VERIFY_MAX_CANDIDATES
    try:
        remaining, limit, _ = browse_remaining(client.rate_limits())
    except Exception:                                     # noqa: BLE001
        remaining = limit = None
    if remaining is not None:
        print(f"[scan] quota {remaining}/{limit}")
        if remaining < VERIFY_MIN_QUOTA:
            print(f"::warning::Quota {remaining} below the verification floor "
                  f"{VERIFY_MIN_QUOTA}; no alerts this run so collection keeps "
                  f"its budget.")
            for c in candidates:
                c["verification"] = "not verified: quota floor"
            return []
        budget = max(0, min(budget,
                            (remaining - VERIFY_MIN_QUOTA) // CALLS_PER_VERIFY))
    print(f"[scan] verifying up to {budget} of {len(candidates)} candidates")

    survivors = []
    for c in candidates:
        if budget <= 0:
            c["verification"] = "not verified: budget exhausted"
            continue
        budget -= 1
        sl = verify.slice_for_bucket(index, c["bucket"])
        try:
            v = verify.check(client, cfg, sl, c["bucket"],
                             c["itemId"], c["price"])
        except Exception as exc:                          # noqa: BLE001
            c["verification"] = f"error: {exc}"
            continue
        ok, why = verify.passes(v)
        c["verification"] = why
        if v:
            c["live"] = {k: v[k] for k in
                         ("live_total", "live_comps", "live_low",
                          "live_prices", "still_listed", "is_lowest")}
            if v.get("live_reference"):
                c["live_reference"] = v["live_reference"]
        if ok:
            survivors.append(c)
    return survivors


def main():
    if "--test-notify" in sys.argv:
        return test_notify()
    rows, aspects, gone = reference.load_corpus()
    now = datetime.now(timezone.utc)
    references, stats, buckets = reference.build(rows, aspects, gone, now)
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
        peer = reference.price_bucket([e[0] for e in buckets.get(key, [])],
                                      now, exclude_item=r["itemId"])
        if not peer:
            continue          # only itself held the bucket above the minimum
        if price > DISCOUNT * peer["reference"]:
            continue
        # The slice pins Grade in the query, so eBay returns whatever the
        # seller typed into that aspect - and sellers get it wrong. Two alerts
        # went out for PSA 9 cards sitting in PSA 10 buckets, priced against
        # the PSA 10 market: a $101 Yamal against a $650 "reference" when PSA 9
        # copies sell around $100. The error becomes the saving, so these
        # mispriced cards sort straight to the top of the ranking.
        #
        # Alert only when the title independently corroborates the grade. A
        # title that contradicts the aspect is evidence the aspect is wrong; a
        # title that states no grade leaves nothing to corroborate it with.
        parsed = matching.parse_title(r.get("title"))
        bucket_grader, bucket_grade = key.split("|")[4], key.split("|")[5]
        if not parsed["grade"] or str(parsed["grade"]) != str(bucket_grade):
            continue
        if parsed["grader"] and parsed["grader"] != bucket_grader:
            continue
        ref = dict(ref, reference=peer["reference"],
                   comp_count=peer["comp_count"])
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

    survivors = verify_candidates(candidates)
    print(f"[scan] {len(survivors)}/{len(candidates)} candidates survived "
          f"live verification")

    room = max(0, MAX_ALERTS_PER_DAY - sent_today)
    to_send = survivors[:room]
    over_limit = len(survivors) > room
    if over_limit:
        print(f"::warning::{len(survivors)} verified candidates but only {room} left "
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

    # Only survivors are recorded. Dropped candidates re-qualify on every run
    # until the market changes, so writing them would append the same rows every
    # 15 minutes; their reasons go to the run log and the summary instead.
    if survivors:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(FLAGS, "a") as fh:
            for flag in survivors:
                fh.write(json.dumps(flag, sort_keys=True, ensure_ascii=False) + "\n")

    # Reasons carry the offending price ("not lowest live BIN ($42.00
    # exists)"), which would make every entry unique, so count the category.
    reasons = collections.Counter(
        c.get("verification", "unknown").split(" (")[0]
        for c in candidates if c.get("verification") != "verified")
    lines = [f"buckets with reference: {len(references):,}",
             f"candidates flagged: {len(candidates)}",
             f"survived live verification: {len(survivors)}",
             f"notifications sent: {sent}",
             f"alerts already sent today: {sent_today}",
             f"held back by the daily guard: "
             f"{max(0, len(survivors) - len(to_send))}"]
    lines += [f"dropped, {why}: {n}" for why, n in reasons.most_common(8)]
    print("\n".join("[scan] " + l for l in lines))
    for flag in survivors[:10]:
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
