#!/usr/bin/env python3
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

Standalone reproduction of ArcadeDB's slow exact edge-similarity scan.

It builds the same shape Graphiti uses (an ``Entity`` vertex type and a ``RELATES_TO`` edge type
whose ``fact_embedding`` property holds a 1024-dimension vector), then times a ladder of queries,
each one stripping one layer from the query Graphiti sends, so the cost of every layer is visible.

    python repro_arcadedb_edge_similarity.py --n 50000

The transport is gRPC by default (ArcadeDB's own data plane); ``--transport bolt`` runs the same
statements over the protocol Graphiti's driver speaks, and ``--transport http`` over the HTTP API.
The ladder is the same either way, so the three numbers are comparable.

The ladder, from the bottom up:

  0. count the edges                        - the scan itself
  1. read the embedding of every edge       - the scan + deserializing the vector
  2. + cosine against a query vector        - the scan + deserializing + the similarity
  3. + the DISTINCT and the GROUP BY        - Graphiti's own query
  4. + the ORDER BY / LIMIT / RETURN        - Graphiti's own query, complete
"""

import argparse
import contextlib
import json
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from benchmarks.arcadedb_transport import Transport, create  # noqa: E402

DIM = 1024
SEED = 42


def make_vector(rng: random.Random, dim: int) -> list[float]:
    """A unit vector, so the cosine is a real number rather than a degenerate one."""
    values = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
    norm = sum(v * v for v in values) ** 0.5
    return [v / norm for v in values]


def build_database(transport: Transport, db: str, n: int, dim: int, batch: int = 500) -> None:
    """The Graphiti shape: ``Entity`` vertices and ``RELATES_TO`` edges with 1024-float embeddings.

    The rows go in through ``UNWIND``, the way Graphiti writes them: one statement per
    batch, with the vectors as parameters. ArcadeDB's SQL parser takes one statement per
    command, so a batch of inserts cannot be concatenated into one string.
    """
    if db in transport.list_databases():
        transport.drop_database(db)
        # The drop is asynchronous: wait until the database is gone before creating it again.
        for _ in range(120):
            if db not in transport.list_databases():
                break
            time.sleep(0.5)
    transport.create_database(db)
    schema = [
        'CREATE VERTEX TYPE Entity',
        'CREATE PROPERTY Entity.uuid STRING',
        'CREATE INDEX ON Entity (uuid) UNIQUE',
        'CREATE EDGE TYPE RELATES_TO',
        'CREATE PROPERTY RELATES_TO.uuid STRING',
        'CREATE PROPERTY RELATES_TO.group_id STRING',
        # Graphiti writes the embedding with `SET e = $edge_data`, so ArcadeDB infers the
        # property type from the value: a LIST of DOUBLE, not a typed array.
        'CREATE PROPERTY RELATES_TO.fact_embedding LIST',
        'CREATE PROPERTY RELATES_TO.fact STRING',
    ]
    for statement in schema:
        # The type or the property already exists: the statement is refused, and the shape
        # it declares is what matters.
        with contextlib.suppress(Exception):
            transport.command(db, 'sql', statement)

    rng = random.Random(SEED)

    # The vertices, in batches
    for start in range(0, n, batch):
        rows = [{'uuid': f'{i:032x}'} for i in range(start, min(start + batch, n))]
        transport.command(
            db,
            'cypher',
            'UNWIND $rows AS row CREATE (n:Entity) SET n = row',
            {'rows': rows},
        )

    # The edges, in batches: each carries a full 1024-dimension vector, as Graphiti's do.
    for start in range(0, 2 * n, batch):
        rows = []
        for i in range(start, min(start + batch, 2 * n)):
            source = i % n
            target = (i * 7 + 3) % n
            rows.append(
                {
                    'uuid': f'{i:032x}',
                    'source_uuid': f'{source:032x}',
                    'target_uuid': f'{target:032x}',
                    'group_id': f'g{i % 3}',
                    'fact': f'fact {i}',
                    'fact_embedding': make_vector(rng, dim),
                }
            )
        transport.command(
            db,
            'cypher',
            'UNWIND $rows AS row '
            'MATCH (source:Entity {uuid: row.source_uuid}) '
            'MATCH (target:Entity {uuid: row.target_uuid}) '
            'CREATE (source)-[e:RELATES_TO]->(target) SET e = row',
            {'rows': rows},
        )


def time_query(
    transport: Transport,
    db: str,
    command_text: str,
    language: str,
    params: dict[str, Any],
    calls: int,
) -> dict[str, float]:
    latencies: list[float] = []
    for _ in range(calls):
        t = time.perf_counter()
        transport.query(db, language, command_text, params)
        latencies.append((time.perf_counter() - t) * 1000)
    return {
        'p50': statistics.median(latencies),
        'mean': statistics.fmean(latencies),
        'min': min(latencies),
        'max': max(latencies),
    }


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument('--transport', choices=['grpc', 'bolt', 'http'], default='grpc')
    p.add_argument('--grpc', default='127.0.0.1:5000', help='gRPC host:port')
    p.add_argument('--bolt', default='bolt://127.0.0.1:7687', help='Bolt URI')
    p.add_argument('--http', default='http://127.0.0.1:2480', help='HTTP base URL')
    p.add_argument('--user', default='root')
    p.add_argument('--password', default='playwithdata')
    p.add_argument('--db', default='repro_edge_sim')
    p.add_argument('--n', type=int, default=50000)
    p.add_argument('--dim', type=int, default=DIM)
    p.add_argument('--calls', type=int, default=10)
    p.add_argument('--skip-build', action='store_true')
    args = p.parse_args()

    transport = create(
        args.transport,
        grpc=args.grpc,
        bolt=args.bolt,
        http=args.http,
        user=args.user,
        password=args.password,
    )
    with transport:
        if not args.skip_build:
            print(
                f'building {args.n} entities and {2 * args.n} facts in {args.db} over '
                f'{args.transport} ...',
                flush=True,
            )
            build_database(transport, args.db, args.n, args.dim)
            print('built', flush=True)

        rng = random.Random(SEED + 1)
        qvec = make_vector(rng, args.dim)
        params = {'search_vector': qvec, 'min_score': 0.6, 'limit': 10, 'group_ids': ['g0']}

        ladder: list[tuple[str, str, str, dict]] = [
            ('0. count the edges', 'SELECT count(*) AS c FROM RELATES_TO', 'sql', {}),
            (
                '1. read the embedding of every edge',
                'SELECT sum(e.fact_embedding[0]) AS s FROM RELATES_TO',
                'sql',
                {},
            ),
            (
                '1b. read the embedding of every edge, full vector',
                'SELECT e.fact_embedding AS v FROM RELATES_TO',
                'sql',
                {},
            ),
            (
                '2. + cosine against the query vector',
                'SELECT vector.cosineSimilarity(e.fact_embedding, :search_vector) AS score '
                'FROM RELATES_TO',
                'sql',
                {'search_vector': qvec},
            ),
            (
                '2b. + cosine, no vector read (constant)',
                'SELECT vector.cosineSimilarity([0.0], [0.0]) AS score FROM RELATES_TO',
                'sql',
                {},
            ),
            (
                '3. + DISTINCT and GROUP BY (Graphiti shape)',
                'MATCH (n:Entity)-[e:RELATES_TO]->(m:Entity) '
                'WITH DISTINCT e, n, m, vector.cosineSimilarity(e.fact_embedding, $search_vector) '
                'AS score WHERE score > $min_score RETURN e.uuid AS uuid ORDER BY score DESC '
                'LIMIT $limit',
                'cypher',
                params,
            ),
            (
                "4. Graphiti's query, complete",
                'MATCH (n:Entity)-[e:RELATES_TO]->(m:Entity) '
                'WHERE e.group_id IN $group_ids AND e.fact_embedding IS NOT NULL '
                'WITH DISTINCT e, n, m, vector.cosineSimilarity(e.fact_embedding, $search_vector) '
                'AS score WHERE score > $min_score '
                'RETURN e.uuid AS uuid, e.group_id AS group_id, e.fact AS fact, '
                'n.uuid AS source_node_uuid, m.uuid AS target_node_uuid '
                'ORDER BY score DESC LIMIT $limit',
                'cypher',
                params,
            ),
        ]

        results: dict[str, dict[str, float]] = {}
        for label, command_text, language, qparams in ladder:
            try:
                stats = time_query(transport, args.db, command_text, language, qparams, args.calls)
                results[label] = stats
                print(f'{label:<48} p50 {stats["p50"]:9.1f} ms', flush=True)
            except Exception as e:  # noqa: BLE001
                print(f'{label:<48} FAILED: {e!r}', flush=True)

    print()
    print(json.dumps(results, indent=1))


if __name__ == '__main__':
    main()
