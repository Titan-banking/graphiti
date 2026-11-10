"""
Copyright 2024, Zep Software, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Render the output of compare_arcadedb_falkordb.py as one self-contained HTML page.

    uv run python benchmarks/make_html_report.py benchmarks/results/2026-11-09
"""

import html
import json
import math
import re
import sys
from pathlib import Path

COLUMNS = [
    ('falkordb', 'FalkorDB', '#e5484d'),
    ('arcadedb_exact', 'ArcadeDB exact', '#8e8ea0'),
    ('arcadedb_ann', 'ArcadeDB ANN', '#3e63dd'),
    ('arcadedb_ann_scalar', 'ArcadeDB ANN scalar', '#30a46c'),
]

# (metric key, title, unit, higher_is_better, extractor kind)
CHARTS = [
    ('bulk_write.nodes_per_s', 'Bulk write: nodes', 'rows/s', True),
    ('bulk_write.edges_per_s', 'Bulk write: edges', 'rows/s', True),
    ('save_node_c1', 'Single node save, 1 writer', 'ms', False),
    ('save_edge_c1', 'Single edge save, 1 writer', 'ms', False),
    ('save_node_c8', 'Single node save, 8 writers', 'ms', False),
    ('save_edge_c8', 'Single edge save, 8 writers', 'ms', False),
    ('node_fulltext', 'Node fulltext search', 'ms', False),
    ('edge_fulltext', 'Edge fulltext search', 'ms', False),
    ('node_similarity', 'Node similarity search', 'ms', False),
    ('edge_similarity', 'Edge similarity search (exact on both)', 'ms', False),
    ('node_bfs', 'Node BFS, depth 2', 'ms', False),
    ('hybrid_search', 'Hybrid search (RRF)', 'ms', False),
    ('disk_mb', 'On-disk size after load', 'MB', False),
]


def get(res: dict, key: str):
    cur = res
    for part in key.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def value_and_p95(v):
    if isinstance(v, dict):
        if 'p50' in v:
            return v['p50'], v.get('p95')
        return None, None
    return v, None


def fmt(v: float) -> str:
    if v >= 1000:
        return f'{v:,.0f}'
    if v >= 100:
        return f'{v:.0f}'
    return f'{v:.1f}'


def chart_svg(results: dict, sizes: list[str], key: str, unit: str) -> str:
    cells = []
    for s in sizes:
        for ckey, _, _ in COLUMNS:
            v, p95 = value_and_p95(get(results.get(ckey, {}).get(s, {}), key))
            cells.append((s, ckey, v, p95))
    vals = [c[2] for c in cells if c[2] and c[2] > 0] + [c[3] for c in cells if c[3]]
    if not vals:
        return '<p class="muted">no data</p>'
    lo = 10 ** math.floor(math.log10(min(vals) * 0.9))
    hi = 10 ** math.ceil(math.log10(max(vals) * 1.05))
    if hi == lo:
        hi = lo * 10
    w, h, pl, pr, pt, pb = 560, 250, 52, 10, 14, 34
    cw, ch = w - pl - pr, h - pt - pb

    def y(v: float) -> float:
        return pt + ch - (math.log10(v) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)) * ch

    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{html.escape(key)}">']
    d = lo
    while d <= hi * 1.0001:
        out.append(f'<line class="grid" x1="{pl}" x2="{w - pr}" y1="{y(d):.1f}" y2="{y(d):.1f}"/>')
        out.append(
            f'<text class="tick" x="{pl - 6}" y="{y(d) + 3:.1f}" text-anchor="end">{fmt(d)}</text>'
        )
        d *= 10
    group_w = cw / len(sizes)
    bar_w = group_w * 0.8 / len(COLUMNS)
    for gi, s in enumerate(sizes):
        gx = pl + gi * group_w + group_w * 0.1
        out.append(
            f'<text class="tick" x="{pl + gi * group_w + group_w / 2:.1f}" y="{h - 12}" '
            f'text-anchor="middle">N={int(s):,}</text>'
        )
        for ci, (ckey, label, color) in enumerate(COLUMNS):
            _, _, v, p95 = next(c for c in cells if c[0] == s and c[1] == ckey)
            x = gx + ci * bar_w
            if not v or v <= 0:
                out.append(
                    f'<text class="tick" x="{x + bar_w / 2:.1f}" y="{pt + ch - 4}" text-anchor="middle">n/a</text>'
                )
                continue
            top = y(v)
            tip = f'{label}, N={s}: {fmt(v)} {unit}' + (f' (p95 {fmt(p95)})' if p95 else '')
            out.append(
                f'<rect x="{x:.1f}" y="{top:.1f}" width="{bar_w - 1.5:.1f}" height="{pt + ch - top:.1f}" '
                f'fill="{color}" rx="2"><title>{html.escape(tip)}</title></rect>'
            )
            if p95:
                out.append(
                    f'<line class="p95" x1="{x + (bar_w - 1.5) / 2:.1f}" x2="{x + (bar_w - 1.5) / 2:.1f}" '
                    f'y1="{top:.1f}" y2="{y(p95):.1f}"/>'
                )
            out.append(
                f'<text class="val" x="{x + (bar_w - 1.5) / 2:.1f}" y="{(y(p95) if p95 else top) - 3:.1f}" '
                f'text-anchor="middle">{fmt(v)}</text>'
            )
    out.append('</svg>')
    return ''.join(out)


def md_inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    return text


def md_section(md: str, heading: str) -> list[str]:
    m = re.search(rf'^## {re.escape(heading)}\n(.*?)(?=^## |\Z)', md, re.S | re.M)
    return m.group(1).strip().split('\n') if m else []


def render_blocks(lines: list[str]) -> str:
    out, in_list = [], False
    for line in lines:
        if line.startswith('- '):
            if not in_list:
                out.append('<ul>')
                in_list = True
            out.append(f'<li>{md_inline(line[2:])}</li>')
        else:
            if in_list:
                out.append('</ul>')
                in_list = False
            if line.strip() and not line.startswith('|'):
                out.append(f'<p>{md_inline(line)}</p>')
    if in_list:
        out.append('</ul>')
    return '\n'.join(out)


def headline(results: dict, size: str, key: str):
    f = value_and_p95(get(results['falkordb'].get(size, {}), key))[0]
    a = value_and_p95(get(results['arcadedb_ann'].get(size, {}), key))[0]
    return f, a


def cards(results: dict, size: str) -> str:
    items = [
        ('Node fulltext', 'node_fulltext', False),
        ('Node similarity (ANN)', 'node_similarity', False),
        ('Edge similarity', 'edge_similarity', False),
        ('Hybrid search', 'hybrid_search', False),
        ('Bulk write edges', 'bulk_write.edges_per_s', True),
        ('Edge save, 1 writer', 'save_edge_c1', False),
    ]
    out = []
    for title, key, higher in items:
        f, a = headline(results, size, key)
        if not f or not a:
            continue
        ratio = (a / f) if higher else (f / a)  # >1: ArcadeDB better
        factor = ratio if ratio > 1 else 1 / ratio
        tie = factor < 1.15
        winner = 'Tie' if tie else ('ArcadeDB' if ratio > 1 else 'FalkorDB')
        cls = 'tie' if tie else ('arc' if ratio > 1 else 'falk')
        unit = 'rows/s' if higher else 'ms'
        out.append(
            f'<div class="card {cls}"><div class="ct">{title}</div>'
            f'<div class="cv">{"about equal" if tie else f"{winner} {factor:.1f}x"}</div>'
            f'<div class="cs">FalkorDB {fmt(f)} {unit} vs ArcadeDB ANN {fmt(a)} {unit}</div></div>'
        )
    return '\n'.join(out)


def table(results: dict, sizes: list[str]) -> str:
    head = (
        '<tr><th>metric</th><th>N</th>' + ''.join(f'<th>{n}</th>' for _, n, _ in COLUMNS) + '</tr>'
    )
    rows = []
    for key, title, unit, higher in CHARTS:
        for s in sizes:
            vs = [value_and_p95(get(results.get(c, {}).get(s, {}), key)) for c, _, _ in COLUMNS]
            nums = [v[0] for v in vs if v[0]]
            best = (max if higher else min)(nums) if nums else None
            cells = []
            for v, p in vs:
                if not v:
                    cells.append('<td class="muted">n/a</td>')
                    continue
                cls = ' class="best"' if v == best else ''
                txt = fmt(v) + (f' <span class="muted">/ {fmt(p)}</span>' if p else '')
                cells.append(f'<td{cls}>{txt}</td>')
            rows.append(
                f'<tr><td>{title} <span class="muted">({unit})</span></td><td>{int(s):,}</td>{"".join(cells)}</tr>'
            )
    for s in sizes:
        cells = []
        for c, _, _ in COLUMNS:
            v = results.get(c, {}).get(s, {}).get('ann_recall_at_10')
            cells.append('<td class="muted">n/a</td>' if v is None else f'<td>{v:.3f}</td>')
        rows.append(f'<tr><td>ANN recall@10</td><td>{int(s):,}</td>{"".join(cells)}</tr>')
    for s in sizes:
        cells = []
        for c, _, _ in COLUMNS:
            v = results.get(c, {}).get(s, {}).get('peak_mem_mib')
            cells.append('<td class="muted">n/a</td>' if v is None else f'<td>{v:,.0f} MiB</td>')
        rows.append(f'<tr><td>Container memory, peak</td><td>{int(s):,}</td>{"".join(cells)}</tr>')
    for s in sizes:
        cells = []
        for c, _, _ in COLUMNS:
            cc = results.get(c, {}).get(s, {}).get('counts')
            ok = cc and cc['nodes'] == int(s) and cc['edges'] == 2 * int(s)
            cells.append(
                '<td class="muted">n/a</td>'
                if not cc
                else ('<td>ok</td>' if ok else '<td class="bad">mismatch</td>')
            )
        rows.append(
            f'<tr><td>Rows after load = N nodes / 2N edges</td><td>{int(s):,}</td>{"".join(cells)}</tr>'
        )
    return f'<table>{head}{"".join(rows)}</table>'


def p50_p95(v) -> str:
    return f'{fmt(v["p50"])} / {fmt(v["p95"])}' if isinstance(v, dict) and 'p50' in v else 'n/a'


def engine_hybrid(results: dict, sizes: list[str]) -> str:
    rows = []
    for s in sizes:
        h = results.get('arcadedb_ann', {}).get(s, {}).get('engine_hybrid', {})
        py, en = h.get('python_rrf'), h.get('engine_fused')
        rows.append(f'<tr><td>{int(s):,}</td><td>{p50_p95(py)}</td><td>{p50_p95(en)}</td></tr>')
    return (
        '<table><tr><th>N</th><th>Graphiti Python RRF (p50 / p95 ms)</th>'
        '<th>Engine <code>/vector/{db}/hybrid</code> (p50 / p95 ms)</th></tr>'
        + ''.join(rows)
        + '</table>'
    )


CSS = """
:root{--bg:#fff;--fg:#1c2024;--muted:#6b7280;--card:#f6f7f9;--border:#e3e5e8;--grid:#e8eaed;--code:#eef0f3}
@media (prefers-color-scheme:dark){:root{--bg:#111316;--fg:#e8eaed;--muted:#9aa1ab;--card:#1a1d21;--border:#2a2e34;--grid:#2a2e34;--code:#23272d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:32px 20px 80px}h1{margin:0 0 4px;font-size:28px}h2{margin:44px 0 12px;font-size:20px;border-bottom:1px solid var(--border);padding-bottom:6px}
.muted{color:var(--muted)}code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:.9em}
.legend{display:flex;flex-wrap:wrap;gap:16px;margin:14px 0}.legend span{display:inline-flex;align-items:center;gap:6px}.legend i{width:12px;height:12px;border-radius:3px;display:inline-block}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--border);border-left:4px solid;border-radius:8px;padding:12px 14px}.card.arc{border-left-color:#3e63dd}.card.falk{border-left-color:#e5484d}.card.tie{border-left-color:#8e8ea0}
.ct{color:var(--muted);font-size:13px}.cv{font-size:20px;font-weight:650}.cs{font-size:12px;color:var(--muted)}
.charts{display:grid;grid-template-columns:repeat(auto-fill,minmax(520px,1fr));gap:18px}
.chart{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px}.chart h3{margin:0 0 2px;font-size:15px}.chart .sub{font-size:12px;color:var(--muted);margin-bottom:4px}
svg{width:100%;height:auto}.grid{stroke:var(--grid);stroke-width:1}.tick{fill:var(--muted);font-size:10px}.val{fill:var(--fg);font-size:8px}.p95{stroke:var(--fg);stroke-width:1;opacity:.55}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:5px 8px;border-bottom:1px solid var(--border);text-align:right}th:first-child,td:first-child{text-align:left}th{position:sticky;top:0;background:var(--bg)}
td.best{font-weight:700;background:rgba(48,164,108,.14)}td.bad{color:#e5484d;font-weight:700}.scroll{overflow-x:auto}
ul{padding-left:20px}li{margin:6px 0}
"""


def main() -> None:
    base = Path(sys.argv[1] if len(sys.argv) > 1 else 'benchmarks/results/2026-11-09')
    results = json.loads(base.with_suffix('.json').read_text())
    md = base.with_suffix('.md').read_text()
    title = md.split('\n', 1)[0].lstrip('# ').strip()
    sizes = sorted({s for v in results.values() for s in v}, key=int)
    top = sizes[-1]

    legend = ''.join(f'<span><i style="background:{c}"></i>{name}</span>' for _, name, c in COLUMNS)
    chart_html = ''.join(
        f'<div class="chart"><h3>{t}</h3><div class="sub">{u}, log scale, '
        f'{"higher is better" if hi else "lower is better"}'
        f'{"; whisker = p95" if not hi and k != "disk_mb" else ""}</div>{chart_svg(results, sizes, k, u)}</div>'
        for k, t, u, hi in CHARTS
    )
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>
<h1>{html.escape(title)}</h1>
<p class="muted">Graphiti's own drivers and search functions, same data on both engines, one run per cell.
ArcadeDB 26.12.1-SNAPSHOT against FalkorDB, 1024-dimension embeddings, 4 CPU / 5 GB per container.</p>
<div class="legend">{legend}</div>

<h2>At a glance: N={int(top):,} (ArcadeDB ANN vs FalkorDB)</h2>
<div class="cards">{cards(results, top)}</div>
<p class="muted">Ratios under about 2x are within this benchmark's run-to-run noise; see the caveats below.</p>

<h2>Charts</h2>
<div class="charts">{chart_html}</div>

<h2>Findings</h2>
{render_blocks(md_section(md, 'Findings'))}

<h2>All numbers</h2>
<p class="muted">Latency cells are p50 <span class="muted">/ p95</span> in ms. Green marks the best value in each row.</p>
<div class="scroll">{table(results, sizes)}</div>

<h2>ArcadeDB engine-fused search vs Graphiti Python RRF</h2>
<p class="muted">Informational; ungrouped queries. The engine endpoint is HTTP only, so the Bolt driver can't use it.</p>
{engine_hybrid(results, sizes)}

<h2>Setup</h2>
{render_blocks(md_section(md, 'Setup'))}
</main></body></html>"""
    out = base.with_suffix('.html')
    out.write_text(page)
    print(f'wrote {out} ({len(page):,} bytes)')


if __name__ == '__main__':
    main()
