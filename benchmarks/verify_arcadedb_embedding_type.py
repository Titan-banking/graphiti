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

Verify that the ArcadeDB driver stores the embeddings as float arrays and reads them back as
``list[float]``.

    uv run python benchmarks/verify_arcadedb_embedding_type.py --bolt bolt://localhost:17691 \
        --http http://localhost:12485 --database graphtest
"""

import argparse
import asyncio
import random
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent))

from graphiti_core.driver.arcadedb_driver import ArcadeDBDriver  # noqa: E402
from graphiti_core.edges import EntityEdge  # noqa: E402
from graphiti_core.embedder.client import EmbedderClient  # noqa: E402
from graphiti_core.nodes import EntityNode  # noqa: E402
from graphiti_core.utils.bulk_utils import add_nodes_and_edges_bulk  # noqa: E402

DIM = 1024


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bolt', default='bolt://localhost:7687')
    p.add_argument('--http', default='http://localhost:2480')
    p.add_argument('--user', default='root')
    p.add_argument('--password', default='playwithdata')
    p.add_argument('--database', default='graphiti')
    args = p.parse_args()

    rng = random.Random(42)
    embedding = [rng.uniform(-1.0, 1.0) for _ in range(DIM)]

    driver = ArcadeDBDriver(
        uri=args.bolt,
        user=args.user,
        password=args.password,
        database=args.database,
        http_uri=args.http,
        embedding_dim=DIM,
    )
    await driver.build_indices_and_constraints()

    node = EntityNode(
        uuid='00000000-0000-0000-0000-000000000001',
        name='n1',
        group_id='g0',
        name_embedding=embedding,
        created_at='2024-01-01T00:00:00',
    )
    other = EntityNode(
        uuid='00000000-0000-0000-0000-000000000002',
        name='n2',
        group_id='g0',
        name_embedding=[0.0] * DIM,
        created_at='2024-01-01T00:00:00',
    )
    await add_nodes_and_edges_bulk(driver, [], [], [node, other], [], Mock(spec=EmbedderClient))

    edge = EntityEdge(
        uuid='00000000-0000-0000-0000-000000000003',
        source_node_uuid=node.uuid,
        target_node_uuid=other.uuid,
        name='e1',
        fact='f1',
        group_id='g0',
        fact_embedding=embedding,
        created_at='2024-01-01T00:00:00',
    )
    await add_nodes_and_edges_bulk(driver, [], [], [], [edge], Mock(spec=EmbedderClient))

    # The node's embedding, read back through the driver's own loader
    node.name_embedding = None
    await node.load_name_embedding(driver)
    read_node = node.name_embedding
    print(f'node.name_embedding  : {type(read_node).__name__} of {len(read_node or [])}')
    assert isinstance(read_node, list), type(read_node)
    assert all(isinstance(v, float) for v in read_node), type(read_node[0])
    max_error = max(abs(a - b) for a, b in zip(embedding, read_node, strict=True))
    print(f'  max |written - read| : {max_error:.3e}')

    # The edge's embedding
    edge.fact_embedding = None
    await edge.load_fact_embedding(driver)
    read_edge = edge.fact_embedding
    print(f'edge.fact_embedding  : {type(read_edge).__name__} of {len(read_edge or [])}')
    assert isinstance(read_edge, list), type(read_edge)
    assert all(isinstance(v, float) for v in read_edge), type(read_edge[0])
    max_error = max(abs(a - b) for a, b in zip(embedding, read_edge, strict=True))
    print(f'  max |written - read| : {max_error:.3e}')
    assert max_error < 1e-5, max_error

    print('OK: the embeddings round-trip as list[float]')


if __name__ == '__main__':
    asyncio.run(main())
