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

Run only the edge-similarity leg against an already-loaded ArcadeDB database, so a profiler
attributes the samples to that query and nothing else.

    uv run python benchmarks/profile_arcadedb_edge.py --variant arcadedb_ann --calls 40
"""

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from benchmarks.compare_arcadedb_falkordb import (  # noqa: E402
    GROUPS,
    MIN_SCORE,
    Engine,
    qvec,
)
from graphiti_core.search.search_filters import SearchFilters  # noqa: E402
from graphiti_core.search.search_utils import edge_similarity_search  # noqa: E402

DIM = 1024
SEED = 42


def pct(values: list[float], q: float) -> float:
    if not values:
        return float('nan')
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[index]


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--variant', default='arcadedb_ann')
    p.add_argument('--n', type=int, default=50000)
    p.add_argument('--calls', type=int, default=60)
    p.add_argument('--dim', type=int, default=DIM)
    p.add_argument('--out', default='/tmp/arcadedb-edge-leg.json')
    args = p.parse_args()

    engine = Engine(args.variant, args.dim)
    await engine.open()
    out: dict[str, object] = {'variant': args.variant, 'n': args.n, 'calls': args.calls}
    try:
        driver = engine.driver
        filt = SearchFilters()
        rng = random.Random(SEED)
        edge_pick = [rng.randrange(2 * args.n) for _ in range(args.calls)]

        edge_lat: list[float] = []
        for idx in edge_pick:
            group = GROUPS[idx % len(GROUPS)]
            t = time.perf_counter()
            await edge_similarity_search(
                driver,
                qvec(1, idx, args.dim),
                None,
                None,
                filt,
                [group],
                limit=10,
                min_score=MIN_SCORE,
            )
            edge_lat.append((time.perf_counter() - t) * 1000)
        out['edge_similarity_ms'] = {
            'p50': pct(edge_lat, 0.5),
            'p95': pct(edge_lat, 0.95),
            'mean': statistics.fmean(edge_lat),
        }
        print(f'edge similarity p50 {out["edge_similarity_ms"]["p50"]:.1f} ms', flush=True)
    finally:
        await engine.close()

    Path(args.out).write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == '__main__':
    asyncio.run(main())
