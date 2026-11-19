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

Render two runs of compare_arcadedb_falkordb.py against ArcadeDB as one HTML page: an older
ArcadeDB build and a newer one, side by side, with the old-to-new change of each metric.

    uv run python benchmarks/make_html_compare.py \
        benchmarks/results/x86-arcadedb benchmarks/results/x86-arcadedb-snapshot-2026-11-19 \
        benchmarks/results/x86-arcadedb-snapshot-2026-11-19-compare.html

The ``exact`` columns use no vector index, so what differs between the two runs there is the
engine (and the host). The ``ANN`` columns use the vector indexes Graphiti's driver creates, and
the driver changed between the runs as well, so a change there is the engine and the driver.
"""

import html
import json
import re
import sys
from pathlib import Path

import make_html_report as report
from make_html_report import CSS, chart_svg, fmt, get, value_and_p95

# Within one engine build, identical queries in different columns differ by up to about this
# factor at 50k, so a smaller change is not reported as better or worse.
NOISE = 1.25

OLD_COLOR, NEW_COLOR = '#8e8ea0', '#3e63dd'
COLUMNS = [
    ('old_exact', 'old build, exact', '#b9b9c6'),
    ('old_ann', 'old build, ANN', '#9aa7d9'),
    ('new_exact', 'new build, exact', '#5b5b73'),
    ('new_ann', 'new build, ANN', '#3e63dd'),
    ('new_ann_scalar', 'new build, ANN scalar', '#30a46c'),
]
CHARTS = [
    (k, 'Edge similarity search (exact scan; the edge index in the ANN columns)' if k == 'edge_similarity' else t, u, hi)
    for k, t, u, hi in report.CHARTS
    if k != 'disk_mb'
]
# (card title, metric, higher is better, old column, new column, what differs between the two)
CARDS = [
    ('Node BFS, depth 2', 'node_bfs', False, 'old_exact', 'new_exact', 'engine'),
    ('Edge similarity (exact scan)', 'edge_similarity', False, 'old_exact', 'new_exact', 'engine'),
    ('Hybrid search (exact)', 'hybrid_search', False, 'old_exact', 'new_exact', 'engine'),
    ('Node fulltext', 'node_fulltext', False, 'old_exact', 'new_exact', 'engine'),
    ('Bulk write edges', 'bulk_write.edges_per_s', True, 'old_exact', 'new_exact', 'engine'),
    ('Edge similarity (ANN)', 'edge_similarity', False, 'old_ann', 'new_ann', 'engine + driver'),
    ('Hybrid search (ANN)', 'hybrid_search', False, 'old_ann', 'new_ann', 'engine + driver'),
    ('Node similarity (ANN)', 'node_similarity', False, 'old_ann', 'new_ann', 'engine + driver'),
]


def setup_value(md: str, label: str) -> str:
    m = re.search(rf'^- {label}: (.+)$', md, re.M)
    return m.group(1).strip() if m else 'not recorded'


def build_of(md: str) -> str:
    m = re.search(r'build ([0-9a-f]{8})', setup_value(md, 'ArcadeDB'))
    return m.group(1) if m else '?'


def card(title: str, key: str, higher: bool, old: float | None, new: float | None, differs: str) -> str:
    if not old or not new:
        return ''
    ratio = (new / old) if higher else (old / new)  # > 1: the new build is better
    factor = ratio if ratio >= 1 else 1 / ratio
    if factor < NOISE:
        cls, verdict = 'tie', f'unchanged ({factor:.2f}x)'
    elif ratio > 1:
        cls, verdict = 'arc', f'{factor:.1f}x better'
    else:
        cls, verdict = 'falk', f'{factor:.1f}x worse'
    unit = 'rows/s' if higher else 'ms'
    return (
        f'<div class="card {cls}"><div class="ct">{html.escape(title)} '
        f'<span class="muted">({differs})</span></div><div class="cv">{verdict}</div>'
        f'<div class="cs">{fmt(old)} to {fmt(new)} {unit}</div></div>'
    )


def table(results: dict, sizes: list[str]) -> str:
    head = '<tr><th>metric</th><th>N</th>' + ''.join(f'<th>{n}</th>' for _, n, _ in COLUMNS) + '</tr>'
    rows = []
    for key, title, unit, _higher in CHARTS:
        for s in sizes:
            cells = []
            for col, _, _ in COLUMNS:
                v, p = value_and_p95(get(results.get(col, {}).get(s, {}), key))
                cells.append(
                    '<td class="muted">n/a</td>' if not v else
                    f'<td>{fmt(v)}' + (f' <span class="muted">/ {fmt(p)}</span>' if p else '') + '</td>'
                )
            rows.append(f'<tr><td>{html.escape(title)} <span class="muted">({unit})</span></td><td>{int(s):,}</td>{"".join(cells)}</tr>')
    for s in sizes:
        cells = []
        for col, _, _ in COLUMNS:
            v = results.get(col, {}).get(s, {}).get('ann_recall_at_10')
            cells.append('<td class="muted">n/a</td>' if v is None else f'<td>{v:.3f}</td>')
        rows.append(f'<tr><td>ANN recall@10</td><td>{int(s):,}</td>{"".join(cells)}</tr>')
    return f'<table>{head}{"".join(rows)}</table>'


def main() -> None:
    old_base, new_base, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    old, new = (json.loads(b.with_suffix('.json').read_text()) for b in (old_base, new_base))
    old_md, new_md = (b.with_suffix('.md').read_text() for b in (old_base, new_base))
    results = {
        'old_exact': old.get('arcadedb_exact', {}),
        'old_ann': old.get('arcadedb_ann', {}),
        'new_exact': new.get('arcadedb_exact', {}),
        'new_ann': new.get('arcadedb_ann', {}),
        'new_ann_scalar': new.get('arcadedb_ann_scalar', {}),
    }
    sizes = sorted({s for v in results.values() for s in v}, key=int)
    top = sizes[-1]
    old_build, new_build = build_of(old_md), build_of(new_md)
    report.COLUMNS = COLUMNS  # chart_svg draws the columns of the module it lives in

    cards = ''.join(
        card(t, k, hi, value_and_p95(get(results[o].get(top, {}), k))[0], value_and_p95(get(results[n].get(top, {}), k))[0], d)
        for t, k, hi, o, n, d in CARDS
    )
    charts = ''.join(
        f'<div class="chart"><h3>{html.escape(t)}</h3><div class="sub">{u}, log scale, '
        f'{"higher is better" if hi else "lower is better"}{"; whisker = p95" if not hi else ""}</div>'
        f'{chart_svg(results, sizes, k, u)}</div>'
        for k, t, u, hi in CHARTS
    )
    legend = ''.join(f'<span><i style="background:{c}"></i>{n}</span>' for _, n, c in COLUMNS)
    title = f'ArcadeDB build {old_build} vs build {new_build}: the Graphiti benchmark'
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>
<h1>{html.escape(title)}</h1>
<p class="muted">The same benchmark (<code>compare_arcadedb_falkordb.py</code>: Graphiti's own driver and search functions, 1024-dimension
embeddings, 3 groups, 4 CPU / 5 GB, 512 MB page cache) against two ArcadeDB builds, one run per cell. Old: build {old_build}
({html.escape(setup_value(old_md, 'ArcadeDB'))}). New: the <code>latest</code> snapshot, build {new_build}.</p>
<div class="legend">{legend}</div>

<h2>At a glance: N={int(top):,}, old to new</h2>
<div class="cards">{cards}</div>
<p class="muted">Cards in <b>engine</b> read the <i>exact</i> columns, which use no vector index and so differ only by the ArcadeDB build (and
the host). Cards in <b>engine + driver</b> read the <i>ANN</i> columns: Graphiti's driver changed between the runs too (a vector index over
the edges, <code>group_id = :group_id</code> for one group, <code>group_id</code> declared <code>STRING</code>), so a change there is not
the engine's alone. A change under {NOISE}x is shown as unchanged: identical queries in different columns of one run differ by about that
much, and by more for bulk writes at small N.</p>

<h2>What these runs do not tell you</h2>
<ul>
<li>The new build ({new_build}) was created at 21:56 on 2026-11-19, before #12559 (the wide-record read) merged at 22:05: it does not contain it, nor any of the open PRs from this session (#12572, #12573, #12574, #12581).</li>
<li>The node-similarity ANN column got slower (see the table at N=50,000). The cause is not established; the edge index now shares the 512 MB page cache with the node index, which is a hypothesis, not a measurement.</li>
<li>One run per cell. The 512 MB page cache is the compose file's; at 2 GB the same ANN hybrid search measured 190 ms, not 366 ms.</li>
</ul>

<h2>Charts</h2>
<div class="charts">{charts}</div>

<h2>All numbers</h2>
<p class="muted">Latency cells are p50 <span class="muted">/ p95</span> in ms; bulk writes are rows/s.</p>
<div class="scroll">{table(results, sizes)}</div>

<h2>Setup of each run</h2>
<table>
<tr><th>run</th><th>ArcadeDB</th><th>image</th><th>Graphiti</th></tr>
<tr><td>old</td><td>{html.escape(setup_value(old_md, 'ArcadeDB'))}</td><td><code>{html.escape(setup_value(old_md, 'ArcadeDB').split('image ')[-1] if 'image' in setup_value(old_md, 'ArcadeDB') else 'see md')}</code></td><td>{html.escape(setup_value(old_md, 'this branch'))}</td></tr>
<tr><td>new</td><td>{html.escape(setup_value(new_md, 'ArcadeDB'))}</td><td><code>{html.escape(setup_value(new_md, 'ArcadeDB').split('image ')[-1] if 'image' in setup_value(new_md, 'ArcadeDB') else 'see md')}</code></td><td>{html.escape(setup_value(new_md, 'this branch'))}</td></tr>
</table>
<p class="muted">Raw data: <code>{old_base.name}.json</code> / <code>.md</code> and <code>{new_base.name}.json</code> / <code>.md</code>.</p>
</main></body></html>"""
    out.write_text(page)
    print(f'wrote {out} ({len(page):,} bytes)')


if __name__ == '__main__':
    main()
