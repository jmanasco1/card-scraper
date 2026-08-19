"""
HTML report — the human-facing output.

The CSV is for spreadsheets and the markdown is for GitHub, but neither is
something you want to read on a Saturday morning while deciding what to bid on.
This writes a self-contained page that opens in a browser: sortable, colour-
coded by how much to trust each row, with both eBay searches one click away.

No external assets — everything is inlined, so the file works offline and can
be mailed to someone as-is.
"""

import html
import json
from datetime import date


def _bar(pct, cap=60.0):
    """Width for the discount bar, clamped so one huge outlier doesn't flatten
    every other row into invisibility."""
    return max(0.0, min(100.0, abs(pct) / cap * 100.0))


def _move(r, prefix):
    """A grade's own price history: what it was, what it is now, and the move.
    Only observed sales — nothing here is predicted."""
    t = r.get(f"{prefix}_trend")
    if not t:
        return "—"
    chg = t["change_pct"]
    cls = "down" if chg < 0 else "up"
    return (f"${t['older_median']:,.0f} → ${t['recent_median']:,.0f}"
            f"<span class='n'>{t['older_n']}+{t['recent_n']} sales</span>"
            f"<span class='chg {cls}'>{chg:+.0f}%</span>")


def _range(r, prefix):
    """Low-high of the comps behind a median. A range that spans orders of
    magnitude is the clearest sign the search matched more than one card."""
    lo, hi = r.get(f"{prefix}_low"), r.get(f"{prefix}_high")
    if not lo or not hi:
        return ""
    return f"${lo:,.0f}–${hi:,.0f}"


def _conf_label(c):
    if c >= 0.75:
        return "solid", "Backed by deep comps on both grades"
    if c >= 0.4:
        return "fair", "Enough comps to be suggestive, not conclusive"
    return "thin", "Few comps — treat this discount as unproven"


def render(sport, ranked, model, stats, uni_report, sample=False, problem=None,
           findings=0):
    stamp = date.today().isoformat()
    rows = []

    for i, r in enumerate(ranked, 1):
        conf = r.get("confidence") or 0.0
        cls, conf_hint = _conf_label(conf)
        disc = r.get("discount_pct") or 0.0
        sign = "cheap" if disc > 0 else "rich"
        name = html.escape(f"{r['card']}")
        num = html.escape(str(r.get("number") or ""))
        setname = html.escape(f"{r.get('year','')} {r.get('set','')}".strip())
        par = r.get("parallel") or ""
        if par and par.lower() != "base":
            setname += f" · {html.escape(par)}"

        rows.append(f"""
      <tr data-score="{r.get('score') or 0}" data-disc="{disc}" data-conf="{conf}"
          data-pop="{r.get('total_pop') or 0}" data-gem="{r.get('gem_rate_pct') or 0}"
          data-p10="{r.get('p10_median') or 0}" data-fair="{r.get('headroom_usd') or 0}">
        <td class="rank">{'★' if r.get('is_finding') else i}</td>
        <td class="card">
          <div class="cname">{name} <span class="num">#{num}</span></div>
          <div class="cset">{setname}</div>
        </td>
        <td class="num-col">{r.get('total_pop', 0):,}</td>
        <td class="num-col">{r.get('gem_rate_pct', 0)}%</td>

        <td class="num-col buy">${(r.get('raw_median') or 0):,.0f}<span class="n">{r.get('raw_sales', 0)}</span></td>
        <td class="num-col">${(r.get('ev_cost') or 0):,.0f}<span class="n">incl. grading</span></td>
        <td class="num-col">${(r.get('grade_ev') or 0):,.0f}<span class="n">after fees</span></td>
        <td class="num-col disloc">${(r.get('grade_edge') or 0):+,.0f}<span class="n">{(r.get('grade_roi_pct') or 0):+.0f}% ROI</span></td>
        <td class="num-col">{(r.get('breakeven_gem_pct') or 0):.0f}%<span class="n">vs {r.get('gem_rate_pct')}% actual</span></td>
        <td class="disc {sign}">
          <div class="dval">{disc:+.0f}%</div>
          <div class="dbar"><span style="width:{_bar(disc):.1f}%"></span></div>
        </td>
        <td><span class="pill {cls}" title="{html.escape(conf_hint)}">{cls}</span></td>
        <td class="score">{(r.get('score') or 0):+.1f}</td>
        <td class="links">
          <a href="{html.escape(r.get('ebay_url_10', '#'))}" target="_blank" rel="noopener">PSA 10</a>
          <a href="{html.escape(r.get('ebay_url_9', '#'))}" target="_blank" rel="noopener">PSA 9</a>
        </td>
      </tr>""")

    r2 = model.get("r_squared", 0.0)
    weak = r2 < 0.25
    slope = model.get("slope")

    warn = ""
    if sample:
        warn += """
    <div class="sample">
      <strong>SAMPLE DATA — NOT REAL FINDINGS.</strong> The card names and
      populations are real, but every price on this page is invented to
      demonstrate the layout. Do not buy anything based on it.
    </div>"""
    n_fit = model.get("n_fit") or model.get("n") or 0
    if model.get("kind") == "flat" or n_fit < 30:
        warn += f"""
    <div class="warn">
      <strong>Not enough cards to model anything.</strong> The curve was fitted
      on {n_fit} card{'' if n_fit == 1 else 's'}, and it estimates three
      parameters. At that size it fits noise and any dollar figure it produces
      is arbitrary. Run <code>./run.sh --limit 60</code> or more — the whole
      method depends on comparing a card against a decent pool of its peers.
    </div>"""
    if weak:
        warn += """
    <div class="warn">
      <strong>Weak fit.</strong> The cards priced this run don't price scarcity
      consistently, so these discounts are noisy. Price more cards
      (<code>./run.sh --limit 80</code>) before acting on them.
    </div>"""
    if uni_report.get("year_param_honored") is False:
        warn += """
    <div class="warn">
      <strong>Universe capped.</strong> GemRate ignored the per-year request, so
      this pool is only the all-time most-graded cards — the famous ones, where
      mispricing is least likely. The long tail wasn't reached.
    </div>"""
    if ranked and not findings:
        warn += """
    <div class="warn">
      <strong>No dislocations, but the prices below are real.</strong> None of
      these cards had its PSA 10 fall far enough against its own PSA 9 to count
      as a finding. Every row is still an actual sold price with an actual
      sales count — sort by <em>10 vs its 9</em> to see which came closest.
    </div>"""
    if not ranked:
        detail = html.escape(problem) if problem else (
            "No card got usable sold prices at both PSA 9 and PSA 10, so nothing "
            "could be compared.")
        warn += f"""
    <div class="warn">
      <strong>Nothing scoreable.</strong> {detail}
    </div>"""

    meta = ""
    if stats:
        meta = (f"{stats.get('priced', 0)} cards priced at both grades · "
                f"median year {stats.get('median_year')} · "
                f"median population {stats.get('median_pop', 0):,} · "
                f"typical 10/9 ratio {stats.get('median_gap')}×")

    fit = f"R² {r2:.2f}"
    if slope is not None:
        fit += f" · slope {slope:.2f}"
    fit += f" · fitted on {model.get('n_fit', model.get('n', 0))} cards"

    return TEMPLATE.format(
        sport=html.escape(sport.title()),
        stamp=stamp,
        meta=html.escape(meta),
        fit=html.escape(fit),
        warn=warn,
        rows="".join(rows),
        count=len(ranked),
        empty=("" if ranked else
               '<div class="empty">No cards could be scored this run — '
               'see the note above.</div>'),
        json_blob=html.escape(json.dumps({"date": stamp, "sport": sport, "sample": sample})),
    )


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{sport} value scan — {stamp}</title>
<style>
  :root {{
    --bg:#f7f7f5; --panel:#fff; --ink:#1b1b19; --muted:#6b6b66;
    --line:#e3e3de; --cheap:#1a7f5a; --cheapbg:#dff0e8;
    --rich:#a33; --accent:#2f6f4f; --warn:#8a6d1f; --warnbg:#fdf6e0;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg:#16171a; --panel:#1e1f23; --ink:#e9e9e6; --muted:#9a9a94;
      --line:#2e3036; --cheap:#5fd0a0; --cheapbg:#1d3a30;
      --rich:#e2867f; --accent:#7fd6aa; --warn:#e0c574; --warnbg:#3a3320;
    }}
  }}
  :root[data-theme="dark"] {{
    --bg:#16171a; --panel:#1e1f23; --ink:#e9e9e6; --muted:#9a9a94;
    --line:#2e3036; --cheap:#5fd0a0; --cheapbg:#1d3a30;
    --rich:#e2867f; --accent:#7fd6aa; --warn:#e0c574; --warnbg:#3a3320;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    padding:32px 20px 80px;
  }}
  .wrap {{ max-width:1180px; margin:0 auto; }}
  h1 {{ font-size:26px; margin:0 0 4px; letter-spacing:-.01em; }}
  .sub {{ color:var(--muted); font-size:13.5px; margin-bottom:2px; }}
  .fit {{ color:var(--muted); font-size:12.5px; font-variant-numeric:tabular-nums; }}
  .lede {{
    background:var(--panel); border:1px solid var(--line); border-radius:10px;
    padding:14px 16px; margin:20px 0; font-size:14px; color:var(--muted);
  }}
  .lede b {{ color:var(--ink); }}
  .warn {{
    background:var(--warnbg); border:1px solid var(--warn); border-radius:10px;
    padding:12px 14px; margin:14px 0; font-size:13.5px;
  }}
  .tablewrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:10px; background:var(--panel); }}
  table {{ border-collapse:collapse; width:100%; min-width:940px; }}
  th, td {{ padding:10px 12px; text-align:left; border-bottom:1px solid var(--line); vertical-align:top; }}
  th {{
    font-size:11.5px; text-transform:uppercase; letter-spacing:.05em;
    color:var(--muted); font-weight:600; cursor:pointer; user-select:none;
    white-space:nowrap; position:sticky; top:0; background:var(--panel);
  }}
  th:hover {{ color:var(--ink); }}
  th.sorted::after {{ content:" ▾"; }}
  tr:last-child td {{ border-bottom:none; }}
  tbody tr:hover {{ background:rgba(127,127,127,.06); }}
  .rank {{ color:var(--muted); font-variant-numeric:tabular-nums; width:34px; }}
  .cname {{ font-weight:600; }}
  .num {{ color:var(--muted); font-weight:400; }}
  .cset {{ color:var(--muted); font-size:12.5px; margin-top:1px; }}
  .num-col {{ font-variant-numeric:tabular-nums; white-space:nowrap; }}
  .num-col .n {{ display:block; color:var(--muted); font-size:11.5px; }}
  .num-col .rng {{ display:block; color:var(--muted); font-size:11px; opacity:.8; }}
  .num-col .n::before {{ content:"n="; }}
  .ratio .n::before {{ content:""; }}
  .buy {{ font-weight:600; }}
  .num-col .chg {{ display:block; font-size:11.5px; font-weight:650; }}
  .num-col .chg.down {{ color:var(--cheap); }}
  .num-col .chg.up {{ color:var(--muted); }}
  .disloc {{ font-weight:700; color:var(--cheap); }}
  .sample {{
    background:#5b1d1d; color:#ffdede; border:1px solid #a33; border-radius:10px;
    padding:12px 14px; margin:14px 0; font-size:13.5px; letter-spacing:.01em;
  }}
  .disc {{ min-width:104px; }}
  .dval {{ font-variant-numeric:tabular-nums; font-weight:650; }}
  .disc.cheap .dval {{ color:var(--cheap); }}
  .disc.rich .dval {{ color:var(--rich); }}
  .dbar {{ height:4px; background:var(--line); border-radius:2px; margin-top:5px; overflow:hidden; }}
  .dbar span {{ display:block; height:100%; background:var(--cheap); }}
  .disc.rich .dbar span {{ background:var(--rich); }}
  .pill {{
    display:inline-block; padding:2px 8px; border-radius:20px; font-size:11.5px;
    font-weight:600; border:1px solid var(--line); cursor:help;
  }}
  .pill.solid {{ background:var(--cheapbg); color:var(--cheap); border-color:transparent; }}
  .pill.fair {{ color:var(--muted); }}
  .pill.thin {{ color:var(--rich); }}
  .score {{ font-variant-numeric:tabular-nums; font-weight:650; color:var(--accent); }}
  .links a {{
    display:block; color:var(--accent); text-decoration:none; font-size:12.5px;
    white-space:nowrap;
  }}
  .links a:hover {{ text-decoration:underline; }}
  footer {{ margin-top:26px; color:var(--muted); font-size:13px; }}
  footer code {{
    background:var(--panel); border:1px solid var(--line);
    padding:1px 5px; border-radius:4px; font-size:12px;
  }}
  .empty {{ padding:40px; text-align:center; color:var(--muted); }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{sport} — value scan</h1>
  <div class="sub">{stamp} · {count} cards ranked · {meta}</div>
  <div class="fit">Fitted premium curve: {fit}</div>

  <div class="lede">
    <b>Buy it raw, grade it, sell it.</b> <b>Raw costs</b> is what an ungraded
    copy actually sells for. <b>All-in</b> adds $25 to grade it. <b>Expect back</b>
    is what you get on average, after eBay fees and shipping, weighting the PSA 10
    price by how often the card actually gems and the rest by the PSA 9 price with
    a haircut. <b>Profit</b> is the difference. <b>Needs to gem</b> is the rate
    required just to break even — if that's higher than the card's real gem rate,
    it's a losing trade no matter how big the PSA 10 price looks.
  </div>
  {warn}

  <div class="tablewrap">
  <table id="t">
    <thead><tr>
      <th data-k="rank">#</th>
      <th>Card</th>
      <th data-k="pop">Pop</th>
      <th data-k="gem">Gem&nbsp;%</th>
      <th data-k="raw">Raw costs</th>
      <th data-k="cost">All-in</th>
      <th data-k="ev">Expect back</th>
      <th data-k="disc" class="sorted">Profit</th>
      <th data-k="be">Needs to gem</th>
      <th data-k="conf">Trust</th>
      <th data-k="score">Edge</th>
      <th>Verify</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
  {empty}
  </div>

  <footer>
    <p><b>A dislocation is a question, not an answer.</b> The 10 falling behind
    its own 9 can mean the 10 is cheap, or it can mean a batch of 10s hit the
    market at once, or that the recent sales were weak listings. It tells you
    where to look.</p>
    <p><b>Before you buy anything:</b> open both eBay links and check the comps
    are genuinely the same card — same year, same parallel, same number. Thin
    comps are the usual reason a discount evaporates on inspection, which is
    what the <b>Trust</b> column is warning you about.</p>
    <p>Re-run with <code>./run.sh --limit 80</code> to price more cards — a
    bigger pool fits a better curve and makes every discount more reliable.</p>
  </footer>
</div>

<script>
// Click-to-sort. Numeric columns read from the row's data-* attributes, which
// hold the raw values, so sorting never depends on how a cell is formatted.
(function () {{
  var table = document.getElementById('t');
  if (!table) return;
  var tbody = table.tBodies[0];
  var keys = {{ pop:'pop', gem:'gem', p10:'p10', fair:'fair', disc:'disc', conf:'conf', score:'score' }};
  var desc = {{}};
  table.querySelectorAll('th[data-k]').forEach(function (th, idx) {{
    th.addEventListener('click', function () {{
      var k = th.dataset.k;
      var rows = Array.prototype.slice.call(tbody.rows);
      desc[k] = !desc[k];
      var dir = desc[k] ? -1 : 1;
      rows.sort(function (a, b) {{
        var av, bv;
        if (keys[k]) {{ av = parseFloat(a.dataset[k]) || 0; bv = parseFloat(b.dataset[k]) || 0; }}
        else if (k === 'gap') {{
          av = (parseFloat(a.dataset.p10) || 0); bv = (parseFloat(b.dataset.p10) || 0);
        }} else {{
          av = parseInt(a.cells[0].textContent, 10); bv = parseInt(b.cells[0].textContent, 10);
          dir = -dir;
        }}
        return (av - bv) * dir;
      }});
      rows.forEach(function (r) {{ tbody.appendChild(r); }});
      table.querySelectorAll('th').forEach(function (h) {{ h.classList.remove('sorted'); }});
      th.classList.add('sorted');
    }});
  }});
}})();
</script>
</body>
</html>
"""
