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

Result-parity check between two engines on the same dataset (run one engine at a time).

    uv run python benchmarks/parity_check.py --variant arcadedb_ann --out /tmp/parity-arcadedb.json
    uv run python benchmarks/parity_check.py --variant tigergraph --out /tmp/parity-tigergraph.json
    uv run python benchmarks/parity_check.py --compare /tmp/parity-arcadedb.json /tmp/parity-tigergraph.json

The dataset is the benchmark's own (10,000 entities, 20,000 facts, 1024 dims, 3 groups); the
queries are the benchmark's own vectors and words, so both engines see identical input.
"""

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent))

from benchmarks.compare_arcadedb_falkordb import (  # noqa: E402
    GROUPS,
    MIN_SCORE,
    Engine,
    load,
    node_uuid,
    qvec,
    wait_for_vector_index,
    words,
)
from graphiti_core.cross_encoder.client import CrossEncoderClient  # noqa: E402
from graphiti_core.driver.tigergraph_driver import TigerGraphDriver  # noqa: E402
from graphiti_core.embedder.client import EmbedderClient  # noqa: E402
from graphiti_core.graphiti_types import GraphitiClients  # noqa: E402
from graphiti_core.llm_client import LLMClient  # noqa: E402
from graphiti_core.search.search import search  # noqa: E402
from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF  # noqa: E402
from graphiti_core.search.search_filters import SearchFilters  # noqa: E402
from graphiti_core.search.search_utils import (  # noqa: E402
    edge_bfs_search,
    edge_fulltext_search,
    edge_similarity_search,
    node_bfs_search,
    node_fulltext_search,
    node_similarity_search,
)
from graphiti_core.tracer import NoOpTracer  # noqa: E402

N = 10_000
DIM = 1024
CALLS = 20
SEED = 42


def uuids(items: Any) -> list[str]:
    return [x.uuid for x in items]


async def collect(engine: Engine) -> dict[str, Any]:
    driver = engine.driver
    filt = SearchFilters()
    rng = random.Random(SEED)

    # Identical inputs for both engines: the benchmark's own seeds.
    origin_sets = [[node_uuid(rng.randrange(N)) for _ in range(10)] for _ in range(CALLS)]
    node_pick = [rng.randrange(N) for _ in range(CALLS)]
    edge_pick = [rng.randrange(2 * N) for _ in range(CALLS)]

    out: dict[str, Any] = {}

    # BFS, depth 2, unlimited and limited; 20 calls, one origin set per call
    for limit in (100000, 10):
        for origins in origin_sets:
            res = await node_bfs_search(driver, origins, filt, 2, None, limit)
            out.setdefault(f'node_bfs_depth2_limit{limit}', []).append(uuids(res))

    for limit in (100000, 10):
        for origins in origin_sets:
            res = await edge_bfs_search(driver, origins, 2, filt, None, limit)
            out.setdefault(f'edge_bfs_depth2_limit{limit}', []).append(uuids(res))

    # Similarity, group-filtered
    for i, idx in enumerate(node_pick):
        group = GROUPS[idx % len(GROUPS)]
        res = await node_similarity_search(
            driver, qvec(0, idx, DIM), filt, [group], limit=10, min_score=MIN_SCORE
        )
        out.setdefault('node_similarity', []).append(uuids(res))
        del i

    for idx in edge_pick:
        group = GROUPS[idx % len(GROUPS)]
        res = await edge_similarity_search(
            driver, qvec(1, idx, DIM), None, None, filt, [group], limit=10, min_score=MIN_SCORE
        )
        out.setdefault('edge_similarity', []).append(uuids(res))

    # Fulltext
    for idx in node_pick:
        group = GROUPS[idx % len(GROUPS)]
        text = ' '.join(words('n', idx, 3).split()[:2])
        res = await node_fulltext_search(driver, text, filt, [group], limit=10)
        out.setdefault('node_fulltext', []).append(uuids(res))

    for idx in edge_pick:
        group = GROUPS[idx % len(GROUPS)]
        text = ' '.join(words('f', idx, 6).split()[:3])
        res = await edge_fulltext_search(driver, text, filt, [group], limit=10)
        out.setdefault('edge_fulltext', []).append(uuids(res))

    # Hybrid RRF
    clients = GraphitiClients(
        driver=driver,
        llm_client=Mock(spec=LLMClient),
        embedder=Mock(spec=EmbedderClient),
        cross_encoder=Mock(spec=CrossEncoderClient),
        tracer=NoOpTracer(),
    )
    for idx in node_pick:
        group = GROUPS[idx % len(GROUPS)]
        text = ' '.join(words('n', idx, 3).split()[:2])
        res = await search(
            clients,
            text,
            [group],
            COMBINED_HYBRID_SEARCH_RRF,
            filt,
            query_vector=qvec(0, idx, DIM),
        )
        out.setdefault('hybrid_rrf_nodes', []).append(uuids(res.nodes))
        out.setdefault('hybrid_rrf_edges', []).append(uuids(res.edges))

    return out


def jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    union = sa | sb
    if not union:
        return 1.0
    return len(sa & sb) / len(union)


def same_order(a: list[str], b: list[str]) -> bool:
    return a == b


def compare(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for key in left:
        if key not in right:
            continue
        lv, rv = left[key], right[key]
        if isinstance(lv, list) and lv and isinstance(lv[0], list):
            # a list of calls: compare per call and aggregate
            per = [
                {
                    'jaccard': jaccard(x, y),
                    'same_order': same_order(x, y),
                    'left_n': len(x),
                    'right_n': len(y),
                }
                for x, y in zip(lv, rv, strict=False)
            ]
            rows[key] = {
                'jaccard_mean': sum(p['jaccard'] for p in per) / len(per),
                'jaccard_min': min(p['jaccard'] for p in per),
                'same_order_all': all(p['same_order'] for p in per),
                'calls': len(per),
            }
        else:
            rows[key] = {
                'jaccard': jaccard(lv, rv),
                'same_order': same_order(lv, rv),
                'left_n': len(lv),
                'right_n': len(rv),
            }
    return rows


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--variant', choices=['arcadedb_ann', 'tigergraph'])
    p.add_argument('--out', required=True)
    p.add_argument('--compare', nargs=2)
    args = p.parse_args()

    if args.compare:
        left = json.loads(Path(args.compare[0]).read_text())
        right = json.loads(Path(args.compare[1]).read_text())
        rows = compare(left['results'], right['results'])
        print(json.dumps(rows, indent=2))
        Path(args.out).write_text(json.dumps(rows, indent=2))
        return

    engine = Engine(args.variant, DIM)
    await engine.open()
    try:
        await load(engine, N, 8)
        if isinstance(engine.driver, TigerGraphDriver):
            await wait_for_vector_index(engine.driver, N, DIM)
        results = await collect(engine)
    finally:
        await engine.close()
    payload = {'variant': args.variant, 'n': N, 'dim': DIM, 'results': results}
    Path(args.out).write_text(json.dumps(payload, indent=1))
    print(f'wrote {args.out}')


if __name__ == '__main__':
    asyncio.run(main())
