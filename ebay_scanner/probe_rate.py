"""Size pagination from measured listing velocity, not guesswork.

Answers: how many listings enter the band per minute, and therefore how many
pages a 15-minute sweep needs before it starts missing things.
"""
from datetime import datetime

from . import auth, config, query
from .client import EbayClient


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def measure(client, cfg, lo, hi, label):
    cfg = dict(cfg, price_min=lo, price_max=hi)
    params = query.search_params(cfg, 0)
    params["limit"] = 200
    body = client.search(params)
    total = body.get("total")
    items = body.get("itemSummaries") or []
    dates = sorted(d for d in (parse(i.get("itemCreationDate")) for i in items) if d)

    print(f"\n[band] {label}")
    print(f"[band]   total matching = {total:,}")
    print(f"[band]   page 1 returned {len(items)} items")
    if len(dates) < 2:
        print("[band]   not enough dated items to measure velocity")
        return total, None
    span_min = (dates[-1] - dates[0]).total_seconds() / 60
    rate = len(dates) / span_min if span_min > 0 else float("inf")
    print(f"[band]   newest {dates[-1]:%H:%M:%S}  oldest {dates[0]:%H:%M:%S}"
          f"  span {span_min:.1f} min")
    print(f"[band]   velocity = {rate:.1f} listings/min")
    for window in (15, 30, 60):
        need = rate * window
        pages = -(-int(need) // 200)
        print(f"[band]   {window:>3} min sweep -> ~{need:,.0f} listings"
              f" -> {pages} pages of 200")
    return total, rate


def main():
    cfg = config.load()
    cid, secret = config.credentials()
    token, _ = auth.get_token(cid, secret)
    client = EbayClient(token, cfg["marketplace_id"])

    print("########## LISTING VELOCITY BY PRICE BAND ##########")
    cur_total, cur_rate = measure(client, cfg, 75, 400, "current  price:[75..400]")
    new_total, new_rate = measure(client, cfg, 20, 400, "proposed price:[20..400]")

    print("\n########## COMPARISON ##########")
    if cur_total and new_total:
        print(f"[cmp] inventory multiplier = {new_total/cur_total:.2f}x "
              f"({cur_total:,} -> {new_total:,})")
    if cur_rate and new_rate:
        print(f"[cmp] velocity multiplier  = {new_rate/cur_rate:.2f}x "
              f"({cur_rate:.1f} -> {new_rate:.1f} listings/min)")
        need15 = new_rate * 15
        print(f"[cmp] a 15-min sweep must absorb ~{need15:,.0f} listings "
              f"= {-(-int(need15)//200)} pages")
        print(f"[cmp] 6 pages covers {1200/new_rate:.1f} min of listings")
    print(f"\n[cmp] calls used: {client.call_count}")


if __name__ == "__main__":
    main()
