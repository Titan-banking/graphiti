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

Compare the two declared array types for an embedding property: ``ARRAY_OF_FLOATS`` (float32) and
``ARRAY_OF_DOUBLES`` (float64).

The same float64 vectors are written to both, each in its own database, and the run reports:

- how long the driver's own edge-similarity query takes against each, the query that dominates an
  exact scan;
- how far the stored values are from the float64 originals;
- what ArcadeDB's ``vector.similarity.cosine`` answers against a float64 ground truth: the error in
  the score, and whether the rows it ranks and the order it ranks them in move.

The arms are measured in turn, alternating which one goes first, so an arm that pays for a cold
cache does not always pay it.

    uv run python benchmarks/compare_arcadedb_embedding_array_type.py \\
        --bolt bolt://localhost:17686 --http http://localhost:12486 --rows 2000
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

DIM = 1024
# the Bolt protocol caps a message at 16 MB, and a row carries DIM doubles
BATCH = 100
# a fresh database name per run, so the script never collides with what an earlier run left
RUN = str(uuid.uuid4().hex[:6])
# the library defaults the benchmark uses
MIN_SCORE = 0.6
LIMIT = 10
ARMS = (('ARRAY_OF_FLOATS', 'ARRAY_OF_FLOATS'), ('ARRAY_OF_DOUBLES', 'ARRAY_OF_DOUBLES'))


def http_post(port: int, path: str, body: dict, user: str, password: str) -> bytes:
    import base64

    request = urllib.request.Request(
        f'http://127.0.0.1:{port}{path}',
        data=json.dumps(body).encode(),
        method='POST',
        headers={
            'Authorization': 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode(),
            'Content-Type': 'application/json',
        },
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return response.read()


def sql(port: int, database: str, command: str, user: str, password: str) -> None:
    http_post(
        port, f'/api/v1/command/{database}', {'language': 'sql', 'command': command}, user, password
    )


def create_database(port: int, database: str, user: str, password: str) -> None:
    import urllib.error

    try:
        http_post(
            port, '/api/v1/server', {'command': f'create database {database}'}, user, password
        )
    except urllib.error.HTTPError as e:
        # 409: it is already there, which is what a re-run wants
        if e.code != 409:
            raise


def vectors(rows: int, dim: int) -> np.ndarray:
    """Vectors in the shape the benchmark uses: a cluster centroid plus a little noise."""
    rng = np.random.default_rng(42)
    clusters = 64
    centroids = rng.normal(size=(clusters, dim)).astype(np.float64)
    noise = rng.normal(scale=0.05, size=(rows, dim)).astype(np.float64)
    assignment = rng.integers(0, clusters, size=rows)
    return centroids[assignment] + noise


async def load_arm(
    driver,
    port: int,
    database: str,
    declared: str,
    data: np.ndarray,
    uids: list[str],
    user: str,
    password: str,
) -> None:
    """A database with the driver's own shape: Entity vertices joined by RELATES_TO edges."""
    create_database(port, database, user, password)
    sql(port, database, 'CREATE VERTEX TYPE Entity', user, password)
    sql(port, database, 'CREATE EDGE TYPE RELATES_TO', user, password)
    sql(
        port,
        database,
        f'CREATE PROPERTY RELATES_TO.fact_embedding IF NOT EXISTS {declared}',
        user,
        password,
    )

    # the vertices, over Bolt, the way the driver writes them
    for start in range(0, len(data), BATCH):
        rows = [{'uid': uids[i]} for i in range(start, min(start + BATCH, len(data)))]
        await driver.execute_query(
            'UNWIND $rows AS row CREATE (v:Entity {uid: row.uid})',
            params={'rows': rows},
            database_=database,
        )

    # the edges: the shape the similarity query scans, an edge between two entities
    for start in range(0, len(data), BATCH):
        rows = [
            {
                'uid': uids[i],
                'source': uids[i],
                'target': uids[(i + 1) % len(data)],
                'emb': data[i].tolist(),
            }
            for i in range(start, min(start + BATCH, len(data)))
        ]
        await driver.execute_query(
            'UNWIND $rows AS row '
            'MATCH (s:Entity {uid: row.source}), (t:Entity {uid: row.target}) '
            'CREATE (s)-[e:RELATES_TO {uid: row.uid, fact_embedding: row.emb}]->(t)',
            params={'rows': rows},
            database_=database,
        )


async def measure_arm(
    driver,
    database: str,
    data: np.ndarray,
    queries: np.ndarray,
    uids: list[str],
) -> dict:
    """The driver's own edge-similarity query, against this arm's data."""
    # the exact query the driver runs for an exact edge-similarity scan
    query = (
        'MATCH (n:Entity)-[e:RELATES_TO]->(m:Entity) '
        'WITH DISTINCT e, n, m, (1 + vector.similarity.cosine(e.fact_embedding, $search_vector)) / 2 AS score '
        'WHERE score > $min_score '
        'RETURN e.uid AS uid, score '
        'ORDER BY score DESC '
        'LIMIT $limit'
    )

    # the float64 ground truth, computed here
    norms = np.linalg.norm(data, axis=1)
    truth = (1 + (data @ queries.T) / (norms[:, None] * np.linalg.norm(queries, axis=1))) / 2

    scan_ms = []
    score_error = []
    set_mismatch = 0
    order_mismatch = 0
    order_moved_in = []
    for qi, query_vector in enumerate(queries):
        # a warm-up pass, so the first query does not pay for the page cache
        await driver.execute_query(
            query,
            params={'search_vector': query_vector.tolist(), 'min_score': MIN_SCORE, 'limit': LIMIT},
            database_=database,
        )
        start = time.perf_counter()
        records = await driver.execute_query(
            query,
            params={'search_vector': query_vector.tolist(), 'min_score': MIN_SCORE, 'limit': LIMIT},
            database_=database,
        )
        scan_ms.append((time.perf_counter() - start) * 1000)

        returned = [r['uid'] for r in records.records]
        scores = np.array([r['score'] for r in records.records], dtype=np.float64)
        best = list(np.argsort(-truth[:, qi])[:LIMIT])

        if len(returned) == LIMIT:
            score_error.append(float(np.max(np.abs(scores - truth[best, qi]))))
            # the rows the database ranked, against the rows the float64 truth ranks
            truth_order = [uids[i] for i in best]
            if set(returned) != set(truth_order):
                set_mismatch += 1
            if returned != truth_order:
                order_mismatch += 1
                order_moved_in.append(qi)

    # the stored values, read back and compared with the float64 originals
    stored = []
    for start in range(0, len(data), BATCH):
        chunk = await driver.execute_query(
            'MATCH (v:Entity) RETURN v.uid AS uid ORDER BY uid SKIP $skip LIMIT $limit',
            params={'skip': start, 'limit': BATCH},
            database_=database,
        )
        stored.extend(r['uid'] for r in chunk.records)
    by_uid = {}
    for start in range(0, len(data), BATCH):
        chunk = await driver.execute_query(
            'MATCH (v:Entity)-[e:RELATES_TO]->(m:Entity) RETURN e.uid AS uid, e.fact_embedding AS emb '
            'ORDER BY e.uid SKIP $skip LIMIT $limit',
            params={'skip': start, 'limit': BATCH},
            database_=database,
        )
        for r in chunk.records:
            by_uid[r['uid']] = r['emb']
    order = {uid: i for i, uid in enumerate(stored)}
    error = 0.0
    for uid in stored:
        values = np.array(by_uid[uid], dtype=np.float64)
        error = max(error, float(np.max(np.abs(values - data[order[uid]]))))

    return {
        'scan_ms_median': statistics.median(scan_ms),
        'max_score_error': max(score_error) if score_error else 0.0,
        'top10_set_differs': set_mismatch,
        'top10_order_differs': order_mismatch,
        'order_moved_in': order_moved_in,
        'max_stored_error': error,
        'queries': len(queries),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bolt', default='bolt://localhost:17686')
    parser.add_argument('--http', default='http://localhost:12486')
    parser.add_argument('--user', default='root')
    parser.add_argument('--password', default='playwithdata')
    parser.add_argument('--database', default='embtype')
    parser.add_argument('--rows', type=int, default=2000)
    parser.add_argument('--queries', type=int, default=20)
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()

    from graphiti_core.driver.arcadedb_driver import ArcadeDBDriver

    port = int(args.http.rsplit(':', 1)[1])
    data = vectors(args.rows, DIM)
    queries = data[: args.queries]

    driver = ArcadeDBDriver(
        uri=args.bolt,
        user=args.user,
        password=args.password,
        database=f'{args.database}_{RUN}',
    )

    # the driver's own database, which the driver opens on the first query
    create_database(port, f'{args.database}_{RUN}', args.user, args.password)

    databases = {}
    arm_uids = {}
    for label, declared in ARMS:
        database = f'{args.database}_{label[-6:].lower()}_{RUN}'
        databases[label] = database
        # the uid carries the row's index, so the read-back order is the order the vectors were
        # written in and the stored values can be matched with the originals
        uids = [f'{i:08d}-{uuid.uuid4().hex[:8]}' for i in range(len(data))]
        arm_uids[label] = uids
        print(f'loading {label} into {database}...', flush=True)
        await load_arm(driver, port, database, declared, data, uids, args.user, args.password)

    results = {label: {'arms': []} for label, _ in ARMS}
    try:
        await driver.execute_query('RETURN 1')
        for round_index in range(args.rounds):
            # alternate which arm goes first, so a cold cache does not always land on the same one
            order = ARMS if round_index % 2 == 0 else tuple(reversed(ARMS))
            for label, _ in order:
                measured = await measure_arm(
                    driver, databases[label], data, queries, arm_uids[label]
                )
                results[label]['arms'].append(measured)
                summary = {k: v for k, v in measured.items() if k != 'order_moved_in'}
            print(f'round {round_index + 1} {label}: {json.dumps(summary)}', flush=True)

        print()
        print(
            f'{"type":18} {"scan ms (median)":>18} {"max score err":>14} {"max stored err":>15} '
            f'{"set differs":>12} {"order differs":>14}'
        )
        for label, _ in ARMS:
            arms = results[label]['arms']
            print(
                f'{label:18} {statistics.median(a["scan_ms_median"] for a in arms):18.3f} '
                f'{max(a["max_score_error"] for a in arms):14.3e} '
                f'{max(a["max_stored_error"] for a in arms):15.3e} '
                f'{sum(a["top10_set_differs"] for a in arms):6}/{sum(a["queries"] for a in arms)} '
                f'{sum(a["top10_order_differs"] for a in arms):6}/{sum(a["queries"] for a in arms)}'
            )
        # the queries whose order moved, per arm, so the two can be compared
        for label, _ in ARMS:
            moved = sorted({q for a in results[label]['arms'] for q in a['order_moved_in']})
            print(f'{label}: order moved in queries {moved}')
        print()
        print(f'databases: {json.dumps(databases)}')
    finally:
        await driver.close()


if __name__ == '__main__':
    asyncio.run(main())
