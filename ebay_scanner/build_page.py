"""Render the collected listings as a single self-contained HTML page.

    python -m ebay_scanner.build_page [--limit N] [--out docs/index.html]

Reads every JSONL partition, trims each record to the fields worth displaying,
and embeds them in one page with client-side search, filtering and sorting.
No external assets, so it works opened from disk or served by GitHub Pages.
"""
import argparse
import glob
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config

# Grader/grade are not available from item_summary/search, and the bulk
# getItems call that carries localizedAspects needs a Buy API grant this
# application does not hold. Titles carry the same information ~82% of the
# time, so it is parsed here FOR DISPLAY ONLY and never written to the JSONL,
# which stays purely API-derived.
GRADERS = "PSA|BGS|SGC|CGC|CSG|HGA|TAG|ISA|GMA|BCCG|BVG|KSA|PGI"
GRADE_RE = re.compile(
    rf"\b({GRADERS})\s*\.?\s*"
    r"(10|9\.5|9|8\.5|8|7\.5|7|6\.5|6|5\.5|5|4\.5|4|3\.5|3|2\.5|2|1\.5|1)\b",
    re.I,
)


def parse_grade(title):
    match = GRADE_RE.search(title or "")
    if not match:
        return None, None
    return match.group(1).upper(), match.group(2)


def load_aspects():
    """Newest successful enrichment per itemId, if any."""
    out = {}
    path = config.DATA_DIR / "aspects.jsonl"
    if not path.exists():
        return out
    with open(path) as fh:
        for line in fh:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("status") == "ok" and rec.get("itemId"):
                out[rec["itemId"]] = rec
    return out


def load_records():
    records = []
    for path in sorted(glob.glob(str(config.DATA_DIR / "*.jsonl"))):
        if path.endswith(("aspects.jsonl", "lifecycle.jsonl")):
            continue
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records


def trim(record):
    grader, grade = parse_grade(record.get("title"))
    return {
        "t": record.get("title"),
        "p": record.get("price"),
        "u": record.get("itemWebUrl"),
        "i": record.get("imageUrl"),
        "d": record.get("itemCreationDate"),
        "s": record.get("sellerUsername"),
        "fs": record.get("sellerFeedbackScore"),
        "fp": record.get("sellerFeedbackPercentage"),
        "c": record.get("condition"),
        # Prefer a real API aspect when one exists; fall back to the title.
        "g": record.get("grader") or grader,
        "gr": record.get("grade") or grade,
        "src": "api" if record.get("grader") else ("title" if grader else None),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5000,
                        help="most recent N listings to embed (default 5000)")
    parser.add_argument("--out", default=str(config.ROOT / "docs" / "index.html"))
    args = parser.parse_args()

    records = load_records()
    enriched = load_aspects()
    for r in records:
        extra = enriched.get(r.get("itemId"))
        if extra:
            for key in ("grader", "grade", "cert_number", "season",
                        "set_name", "player", "card_number"):
                if extra.get(key):
                    r[key] = extra[key]
    total = len(records)
    records.sort(key=lambda r: (r.get("itemCreationDate") or ""), reverse=True)
    shown = [trim(r) for r in records[:args.limit]]

    prices = [r["p"] for r in shown if r.get("p") is not None]
    dates = [r["itemCreationDate"] for r in records if r.get("itemCreationDate")]
    graded = sum(1 for r in shown if r.get("g"))

    stats = {
        "total": total,
        "shown": len(shown),
        "truncated": total > len(shown),
        "median": round(sorted(prices)[len(prices) // 2], 2) if prices else None,
        "low": min(prices) if prices else None,
        "high": max(prices) if prices else None,
        "gradedPct": round(graded / len(shown) * 100) if shown else 0,
        "oldest": min(dates)[:10] if dates else None,
        "newest": max(dates)[:10] if dates else None,
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

    payload = json.dumps(shown, separators=(",", ":"), ensure_ascii=False)
    payload = payload.replace("</", "<\\/")  # never break out of the script tag

    html = TEMPLATE.replace("/*__DATA__*/null", payload)
    html = html.replace("/*__STATS__*/null", json.dumps(stats))

    out = Path(args.out)
    if not out.is_absolute():
        out = config.ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    size_kb = out.stat().st_size / 1024
    print(f"[page] wrote {out} — {len(shown)} of {total} listings, {size_kb:.0f} KB")


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Graded Card Listings</title>
<style>
:root{
  --bg:#f6f7f9; --panel:#fff; --ink:#12151a; --muted:#5b6470; --line:#e2e6ec;
  --accent:#2b6cb0; --chip:#eef2f7; --shadow:0 1px 3px rgba(0,0,0,.07);
}
@media (prefers-color-scheme:dark){
  :root{ --bg:#0f1216; --panel:#171b21; --ink:#e8ecf1; --muted:#98a2b0; --line:#262c35;
         --accent:#6aa9e9; --chip:#212831; --shadow:0 1px 3px rgba(0,0,0,.4); }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{padding:22px 20px 14px;max-width:1400px;margin:0 auto}
h1{margin:0 0 4px;font-size:21px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px}
.stats{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 0}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:8px 12px;box-shadow:var(--shadow)}
.stat b{display:block;font-size:17px;letter-spacing:-.01em}
.stat span{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.controls{position:sticky;top:0;z-index:5;background:var(--bg);
  border-bottom:1px solid var(--line);padding:12px 20px}
.controls .inner{max-width:1400px;margin:0 auto;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
input,select{background:var(--panel);color:var(--ink);border:1px solid var(--line);
  border-radius:7px;padding:8px 10px;font:inherit;font-size:14px}
input:focus,select:focus{outline:2px solid var(--accent);outline-offset:-1px}
#q{flex:1;min-width:220px}
.num{width:92px}
.count{color:var(--muted);font-size:13px;margin-left:auto;white-space:nowrap}
main{max-width:1400px;margin:0 auto;padding:0 20px 40px}
.wrap{overflow-x:auto;background:var(--panel);border:1px solid var(--line);
  border-radius:10px;box-shadow:var(--shadow)}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:9px 11px;text-align:left;border-bottom:1px solid var(--line);vertical-align:middle}
th{position:sticky;top:0;background:var(--panel);font-size:11px;text-transform:uppercase;
  letter-spacing:.05em;color:var(--muted);cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--ink)}
th.active{color:var(--accent)}
tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--chip)}
img{width:48px;height:48px;object-fit:cover;border-radius:5px;background:var(--chip);display:block}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.price{font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap}
.chip{display:inline-block;background:var(--chip);border-radius:5px;padding:2px 7px;
  font-size:12px;font-weight:600;white-space:nowrap}
.title{max-width:520px}
.muted{color:var(--muted);font-size:12px}
.tnum{font-variant-numeric:tabular-nums;white-space:nowrap}
.more{display:block;width:100%;margin:14px 0 0;padding:11px;background:var(--panel);
  border:1px solid var(--line);border-radius:8px;color:var(--ink);font:inherit;cursor:pointer}
.more:hover{background:var(--chip)}
.empty{padding:44px;text-align:center;color:var(--muted)}
.note{margin:16px 0 0;padding:11px 13px;background:var(--panel);border:1px solid var(--line);
  border-left:3px solid var(--accent);border-radius:7px;font-size:13px;color:var(--muted)}
</style>
</head>
<body>
<header>
  <h1>Graded Card Listings</h1>
  <div class="sub" id="sub"></div>
  <div class="stats" id="stats"></div>
</header>

<div class="controls"><div class="inner">
  <input id="q" type="search" placeholder="Search title, player, set, seller…" autocomplete="off">
  <select id="grader"></select>
  <select id="grade"></select>
  <input id="min" class="num" type="number" placeholder="Min $" min="0">
  <input id="max" class="num" type="number" placeholder="Max $" min="0">
  <span class="count" id="count"></span>
</div></div>

<main>
  <div class="wrap">
    <table>
      <thead><tr>
        <th style="width:56px"></th>
        <th data-k="t">Title</th>
        <th data-k="p" style="width:96px">Price</th>
        <th data-k="g" style="width:86px">Grader</th>
        <th data-k="gr" style="width:76px">Grade</th>
        <th data-k="s" style="width:150px">Seller</th>
        <th data-k="fs" style="width:110px">Feedback</th>
        <th data-k="d" style="width:130px">Listed</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
    <div class="empty" id="empty" hidden>No listings match those filters.</div>
  </div>
  <button class="more" id="more" hidden></button>
  <div class="note" id="note"></div>
</main>

<script>
const DATA  = /*__DATA__*/null;
const STATS = /*__STATS__*/null;
const PAGE  = 200;

let sortKey = 'd', sortDir = -1, shown = PAGE, view = DATA;

const $ = id => document.getElementById(id);
const esc = s => (s??'').toString().replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function fmtDate(d){
  if(!d) return '';
  const dt = new Date(d);
  if(isNaN(dt)) return '';
  return dt.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
}

function buildHeader(){
  $('sub').textContent =
    `${STATS.oldest === STATS.newest ? STATS.oldest : STATS.oldest+' – '+STATS.newest}`
    + ` · built ${STATS.built}`;
  const cells = [
    ['Listings', STATS.total.toLocaleString()],
    ['Median', STATS.median != null ? '$'+STATS.median : '—'],
    ['Range', STATS.low != null ? `$${STATS.low}–$${STATS.high}` : '—'],
    ['Grade known', STATS.gradedPct + '%'],
  ];
  $('stats').innerHTML = cells.map(([k,v]) =>
    `<div class="stat"><b>${esc(v)}</b><span>${esc(k)}</span></div>`).join('');

  $('note').innerHTML =
    'Grader and grade are parsed from listing titles, not from eBay item aspects — '
    + 'the Browse API returns aspects only through a bulk item call this application '
    + 'is not yet granted. They are shown here for convenience and are '
    + '<strong>not</strong> stored in the JSONL, which stays purely API-derived. '
    + (STATS.truncated
        ? `Showing the ${STATS.shown.toLocaleString()} most recent of ${STATS.total.toLocaleString()} stored listings.`
        : 'All stored listings are shown.');
}

function buildFilters(){
  const graders = [...new Set(DATA.map(r=>r.g).filter(Boolean))].sort();
  $('grader').innerHTML = '<option value="">All graders</option>'
    + graders.map(g=>`<option>${esc(g)}</option>`).join('');
  const grades = [...new Set(DATA.map(r=>r.gr).filter(Boolean))]
    .sort((a,b)=>parseFloat(b)-parseFloat(a));
  $('grade').innerHTML = '<option value="">All grades</option>'
    + grades.map(g=>`<option>${esc(g)}</option>`).join('');
}

function apply(){
  const q = $('q').value.trim().toLowerCase();
  const g = $('grader').value, gr = $('grade').value;
  const lo = parseFloat($('min').value), hi = parseFloat($('max').value);

  view = DATA.filter(r=>{
    if(q && !((r.t||'')+' '+(r.s||'')).toLowerCase().includes(q)) return false;
    if(g  && r.g  !== g ) return false;
    if(gr && r.gr !== gr) return false;
    if(!isNaN(lo) && !(r.p >= lo)) return false;
    if(!isNaN(hi) && !(r.p <= hi)) return false;
    return true;
  });

  view.sort((a,b)=>{
    let x = a[sortKey], y = b[sortKey];
    if(sortKey === 'p' || sortKey === 'fs'){ x = x??-Infinity; y = y??-Infinity; }
    else { x = (x??'').toString().toLowerCase(); y = (y??'').toString().toLowerCase(); }
    return x < y ? -sortDir : x > y ? sortDir : 0;
  });

  shown = PAGE;
  render();
}

function render(){
  const slice = view.slice(0, shown);
  $('rows').innerHTML = slice.map(r=>`<tr>
    <td>${r.i ? `<img loading="lazy" src="${esc(r.i)}" alt="">` : '<div style="width:48px"></div>'}</td>
    <td class="title"><a href="${esc(r.u)}" target="_blank" rel="noopener">${esc(r.t)}</a></td>
    <td class="price">$${r.p != null ? r.p.toFixed(2) : '—'}</td>
    <td>${r.g ? `<span class="chip">${esc(r.g)}</span>` : '<span class="muted">—</span>'}</td>
    <td>${r.gr ? `<span class="chip">${esc(r.gr)}</span>` : '<span class="muted">—</span>'}</td>
    <td class="muted">${esc(r.s)}</td>
    <td class="muted tnum">${r.fs != null ? r.fs.toLocaleString() : '—'}${r.fp ? ` · ${esc(r.fp)}%` : ''}</td>
    <td class="muted tnum">${fmtDate(r.d)}</td>
  </tr>`).join('');

  $('count').textContent = `${view.length.toLocaleString()} of ${DATA.length.toLocaleString()}`;
  $('empty').hidden = view.length > 0;
  const rest = view.length - slice.length;
  $('more').hidden = rest <= 0;
  $('more').textContent = rest > 0 ? `Show ${Math.min(rest, PAGE)} more (${rest.toLocaleString()} remaining)` : '';

  document.querySelectorAll('th[data-k]').forEach(th=>
    th.classList.toggle('active', th.dataset.k === sortKey));
}

document.querySelectorAll('th[data-k]').forEach(th=>{
  th.addEventListener('click', ()=>{
    const k = th.dataset.k;
    if(k === sortKey) sortDir = -sortDir;
    else { sortKey = k; sortDir = (k === 'p' || k === 'fs' || k === 'd') ? -1 : 1; }
    apply();
  });
});
['q','grader','grade','min','max'].forEach(id=>{
  $(id).addEventListener('input', apply);
});
$('more').addEventListener('click', ()=>{ shown += PAGE; render(); });

buildHeader();
buildFilters();
apply();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
