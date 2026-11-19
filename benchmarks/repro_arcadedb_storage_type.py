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

A/B of the two ways ArcadeDB can hold a Graphiti edge embedding, with the same data and the same
query: an undeclared LIST (what Graphiti's driver produces today, because it writes the edge with
``SET e = $edge_data``) and a declared ARRAY_OF_FLOATS.

Both variants are loaded through Bolt, so the write path is identical apart from the declared
property type, and the read is the same edge-similarity query Graphiti sends.

    uv run python benchmarks/repro_arcadedb_storage_type.py --n 50000
"""

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from benchmarks.compare_arcadedb_falkordb import (  # noqa: E402
    GROUPS,
    MIN_SCORE,
    Engine,
    load,
    qvec,
)
from graphiti_core.search.search_filters import SearchFilters  # noqa: E402
from graphiti_core.search.search_utils import edge_similarity_search  # noqa: E402

DIM = 1024
SEED = 42


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[index]


async def time_edge_similarity(engine: Engine, n: int, calls: int) -> dict[str, float]:
    driver = engine.driver
    filt = SearchFilters()
    rng = random.Random(SEED)
    latencies: list[float] = []
    for _ in range(calls):
        idx = rng.randrange(2 * n)
        group = GROUPS[idx % len(GROUPS)]
        t = time.perf_counter()
        await edge_similarity_search(
            driver,
            qvec(1, idx, engine.dim),
            None,
            None,
            filt,
            [group],
            limit=10,
            min_score=MIN_SCORE,
        )
        latencies.append((time.perf_counter() - t) * 1000)
    return {
        'p50': pct(latencies, 0.5),
        'p95': pct(latencies, 0.95),
        'mean': statistics.fmean(latencies),
    }


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--n', type=int, default=50000)
    p.add_argument('--calls', type=int, default=30)
    p.add_argument('--dim', type=int, default=DIM)
    p.add_argument('--out', default='/tmp/arcadedb-storage-type-ab.json')
    args = p.parse_args()

    results: dict[str, Any] = {}
    for variant in ('arcadedb_ann', 'arcadedb_exact'):
        engine = Engine(variant, args.dim)
        await engine.open()
        try:
            t0 = time.perf_counter()
            await load(engine, args.n, 8)
            load_s = time.perf_counter() - t0
            stats = await time_edge_similarity(engine, args.n, args.calls)
            results[variant] = {'load_s': load_s, 'edge_similarity_ms': stats}
            print(
                f'{variant}: load {load_s:.0f} s, edge similarity p50 {stats["p50"]:.1f} ms',
                flush=True,
            )
        finally:
            await engine.close()

    Path(args.out).write_text(json.dumps(results, indent=1))
    print(json.dumps(results, indent=1))


if __name__ == '__main__':
    asyncio.run(main())
