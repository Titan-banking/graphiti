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

Profile the two slow ArcadeDB legs of the Graphiti benchmark: edge similarity (an exact scan over
every fact) and the hybrid RRF search that includes it.

    uv run python benchmarks/profile_arcadedb_slow.py --variant arcadedb_ann --n 50000 --calls 60

It loads the benchmark's own dataset (same seeds, same vectors) and then times the two legs
separately, so a JFR profile can be attributed to one of them.
"""

import argparse
import asyncio
import datetime as dt
import json
import random
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent))

from benchmarks.compare_arcadedb_falkordb import (  # noqa: E402
    GROUPS,
    MIN_SCORE,
    Engine,
    load,
    qvec,
    words,
)
from graphiti_core.cross_encoder.client import CrossEncoderClient  # noqa: E402
from graphiti_core.embedder.client import EmbedderClient  # noqa: E402
from graphiti_core.graphiti_types import GraphitiClients  # noqa: E402
from graphiti_core.llm_client import LLMClient  # noqa: E402
from graphiti_core.search.search import search  # noqa: E402
from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF  # noqa: E402
from graphiti_core.search.search_filters import SearchFilters  # noqa: E402
from graphiti_core.search.search_utils import edge_similarity_search  # noqa: E402
from graphiti_core.tracer import NoOpTracer  # noqa: E402

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
    p.add_argument('--out', default='/tmp/arcadedb-slow-legs.json')
    args = p.parse_args()

    engine = Engine(args.variant, args.dim)
    await engine.open()
    out: dict[str, object] = {'variant': args.variant, 'n': args.n, 'calls': args.calls}
    try:
        t0 = time.perf_counter()
        await load(engine, args.n, 8)
        out['load_s'] = time.perf_counter() - t0
        print(f'load done at {dt.datetime.now(dt.timezone.utc).isoformat()}', flush=True)

        driver = engine.driver
        filt = SearchFilters()
        rng = random.Random(SEED)
        edge_pick = [rng.randrange(2 * args.n) for _ in range(args.calls)]

        # 1. edge similarity: exact scan over every fact
        edge_lat: list[float] = []
        print(
            f'edge similarity starts at {dt.datetime.now(dt.timezone.utc).isoformat()}', flush=True
        )
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
        print(f'edge similarity ends at {dt.datetime.now(dt.timezone.utc).isoformat()}', flush=True)
        out['edge_similarity_ms'] = {
            'p50': pct(edge_lat, 0.5),
            'p95': pct(edge_lat, 0.95),
            'mean': statistics.fmean(edge_lat),
        }
        print(f'edge similarity p50 {out["edge_similarity_ms"]["p50"]:.1f} ms', flush=True)

        # 2. hybrid RRF: fulltext + node similarity + edge similarity
        clients = GraphitiClients(
            driver=driver,
            llm_client=Mock(spec=LLMClient),
            embedder=Mock(spec=EmbedderClient),
            cross_encoder=Mock(spec=CrossEncoderClient),
            tracer=NoOpTracer(),
        )
        hybrid_lat: list[float] = []
        print(f'hybrid starts at {dt.datetime.now(dt.timezone.utc).isoformat()}', flush=True)
        node_pick = [rng.randrange(args.n) for _ in range(args.calls)]
        for idx in node_pick:
            group = GROUPS[idx % len(GROUPS)]
            text = ' '.join(words('n', idx, 3).split()[:2])
            t = time.perf_counter()
            await search(
                clients,
                text,
                [group],
                COMBINED_HYBRID_SEARCH_RRF,
                filt,
                query_vector=qvec(0, idx, args.dim),
            )
            hybrid_lat.append((time.perf_counter() - t) * 1000)
        print(f'hybrid ends at {dt.datetime.now(dt.timezone.utc).isoformat()}', flush=True)
        out['hybrid_rrf_ms'] = {
            'p50': pct(hybrid_lat, 0.5),
            'p95': pct(hybrid_lat, 0.95),
            'mean': statistics.fmean(hybrid_lat),
        }
        print(f'hybrid RRF p50 {out["hybrid_rrf_ms"]["p50"]:.1f} ms', flush=True)

        # 3. node similarity alone, for contrast (the ANN leg of the hybrid)
        node_lat: list[float] = []
        for idx in node_pick:
            group = GROUPS[idx % len(GROUPS)]
            t = time.perf_counter()
            await search(
                clients,
                ' '.join(words('n', idx, 3).split()[:2]),
                [group],
                COMBINED_HYBRID_SEARCH_RRF,
                filt,
                query_vector=qvec(0, idx, args.dim),
            )
            node_lat.append((time.perf_counter() - t) * 1000)
        out['hybrid_rrf_again_ms'] = {'p50': pct(node_lat, 0.5)}
    finally:
        await engine.close()

    Path(args.out).write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == '__main__':
    asyncio.run(main())
