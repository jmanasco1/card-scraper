"""GitHub Step Summary rendering."""
import os
import sys


def _out():
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    return open(path, "a") if path else sys.stdout


def _fmt(value, dash="—"):
    return dash if value is None else value


def write_summary(**kw):
    fh = _out()
    try:
        if kw.get("aborted"):
            fh.write("## eBay scan — ABORTED (quota guard)\n\n")
            fh.write(
                f"Browse quota remaining **{kw.get('remaining')}** of "
                f"{_fmt(kw.get('daily_limit'))}, below the abort threshold of "
                f"{kw.get('threshold')}. No search was performed.\n\n"
                f"- Calls used this run: {kw.get('calls_used')}\n"
                f"- Total listings stored: {kw.get('total_stored')}\n"
            )
            return

        new_count = kw.get("new_count", 0)
        fh.write("## eBay graded-card scan\n\n")
        fh.write("| Metric | Value |\n|---|---|\n")
        fh.write(f"| New listings found | **{new_count}** |\n")
        fh.write(f"| Total stored (all time) | {kw.get('total_stored')} |\n")
        fh.write(f"| Matches reported by eBay | {_fmt(kw.get('total_matches'))} |\n")
        fh.write(f"| API calls used this run | {kw.get('calls_used')} |\n")
        fh.write(f"| Quota remaining (at start) | {_fmt(kw.get('remaining'))} |\n")
        fh.write(f"| Quota remaining (estimated now) | {_fmt(kw.get('estimated_remaining'))} |\n")
        fh.write(f"| Daily limit | {_fmt(kw.get('daily_limit'))} |\n")
        fh.write(f"| Token | {'newly minted' if kw.get('token_minted') else 'from cache'} |\n")
        if kw.get("partition"):
            fh.write(f"| Partition written | `data/{kw['partition']}` |\n")

        categories = kw.get("categories") or {}
        if categories.get("categories"):
            fh.write("\n**Categories searched** (verified via Taxonomy API): ")
            fh.write(", ".join(
                f"`{c['categoryId']}` {c['categoryName']}"
                for c in categories["categories"]
            ))
            fh.write("\n")

        records = kw.get("new_records") or []
        if records:
            cheapest = sorted(
                [r for r in records if r.get("price") is not None],
                key=lambda r: r["price"],
            )[:10]
            fh.write("\n### 10 lowest-priced new listings\n\n")
            fh.write("| Price | Grader | Grade | Title |\n|---|---|---|---|\n")
            for r in cheapest:
                title = (r.get("title") or "").replace("|", "\\|")[:90]
                url = r.get("itemWebUrl")
                linked = f"[{title}]({url})" if url else title
                fh.write(
                    f"| ${r['price']:.2f} | {_fmt(r.get('grader'))} | "
                    f"{_fmt(r.get('grade'))} | {linked} |\n"
                )

            # Field-coverage counts: the point of keeping the raw aspects blob.
            fh.write("\n### Field coverage in this batch\n\n")
            fh.write("| Field | Present | % |\n|---|---|---|\n")
            for column in ["grader", "grade", "cert_number", "season",
                           "set_name", "player", "card_number"]:
                present = sum(1 for r in records if r.get(column))
                pct = (present / len(records) * 100) if records else 0
                fh.write(f"| {column} | {present}/{len(records)} | {pct:.0f}% |\n")
        else:
            fh.write("\n_No new listings this run — nothing committed._\n")
    finally:
        if fh is not sys.stdout:
            fh.close()
