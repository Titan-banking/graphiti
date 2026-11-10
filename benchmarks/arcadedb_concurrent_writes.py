#!/usr/bin/env python3
"""
Concurrent writers against ArcadeDB through Graphiti's production write path
(`add_nodes_and_edges_bulk`), with a post-run integrity audit.

Each scenario gets a fresh database holding `--nodes` entities (preloaded), then W writers
share a queue of small edge batches (`--batch` edges each, the size of one add_episode call).
A fraction of the edges (`--hub-share`) touches one of `--hubs` heavily connected entities, as
real graphs do, so that concurrent batches contend for the same vertices' edge lists.

Reports throughput, per-batch latency, errors, and then audits the result:

  edge records   SELECT count(*) FROM RELATES_TO              (what was stored)
  reached OUT    sum(outE('RELATES_TO').size()) over Entity   (what vertices' OUT lists reach)
  reached IN     sum(inE('RELATES_TO').size()) over Entity    (what vertices' IN lists reach)
  expected       edges whose batch committed

All four must be equal. A difference means an edge exists in only one place (dangling or ghost).

    uv run python benchmarks/arcadedb_concurrent_writes.py                       # 1, 2, 8 writers
    uv run python benchmarks/arcadedb_concurrent_writes.py --writers 8 --batches 200
"""

import argparse
import asyncio
import random
import statistics
import time
from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

import httpx
from neo4j import AsyncGraphDatabase
from neo4j.exceptions import TransientError

from graphiti_core.driver.arcadedb_driver import ArcadeDBDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.nodes import EntityNode
from graphiti_core.utils.bulk_utils import add_nodes_and_edges_bulk

BOLT = 'bolt://localhost:7687'
HTTP = 'http://localhost:2481'
AUTH = ('root', 'playwithdata')


def sql(database: str, command: str) -> list[dict]:
    response = httpx.post(
        f'{HTTP}/api/v1/command/{database}',
        json={'language': 'sql', 'command': command},
        auth=AUTH,
        timeout=600,
    )
    response.raise_for_status()
    return response.json()['result']


def admin(command: str) -> None:
    httpx.post(
        f'{HTTP}/api/v1/server', json={'command': command}, auth=AUTH, timeout=120
    ).raise_for_status()


def vector(rng: random.Random, dim: int) -> list[float]:
    return [rng.uniform(-1, 1) for _ in range(dim)]


async def scenario(args, writers: int) -> dict:
    database = f'conc_{uuid4().hex[:8]}'
    admin(f'create database {database}')
    driver = ArcadeDBDriver(
        uri=BOLT,
        user=AUTH[0],
        password=AUTH[1],
        database=database,
        http_uri=HTTP,
        embedding_dim=args.dim,
        use_vector_index=False,
        write_concurrency=args.write_concurrency or None,
    )
    if args.retry_delay is not None:
        await driver.client.close()
        driver.client = AsyncGraphDatabase.driver(
            BOLT, auth=AUTH, initial_retry_delay=args.retry_delay
        )
    embedder = Mock(spec=EmbedderClient)
    rng = random.Random(args.seed)
    try:
        await driver.build_indices_and_constraints()
        nodes = [
            EntityNode(
                uuid=f'n{i}',
                name=f'node {i}',
                group_id='g',
                summary='s',
                name_embedding=vector(rng, args.dim),
            )
            for i in range(args.nodes)
        ]
        for start in range(0, args.nodes, 500):
            await add_nodes_and_edges_bulk(driver, [], [], nodes[start : start + 500], [], embedder)

        hubs = [f'n{i}' for i in range(args.hubs)]

        def endpoint() -> str:
            if rng.random() < args.hub_share:
                return rng.choice(hubs)
            return f'n{rng.randrange(args.nodes)}'

        batches = [
            [
                EntityEdge(
                    group_id='g',
                    source_node_uuid=endpoint(),
                    target_node_uuid=endpoint(),
                    name='RELATES',
                    fact=f'fact {b}-{j}',
                    fact_embedding=vector(rng, args.dim),
                    episodes=[],
                    created_at=datetime.now(timezone.utc),
                )
                for j in range(args.batch)
            ]
            for b in range(args.batches)
        ]

        queue: asyncio.Queue = asyncio.Queue()
        for index, batch in enumerate(batches):
            queue.put_nowait((index, batch))
        latencies: list[float] = []
        committed: set[int] = set()
        failures: list[str] = []
        app_retries = 0

        async def writer():
            nonlocal app_retries
            while True:
                try:
                    index, batch = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                for attempt in range(args.attempts):
                    started = time.perf_counter()
                    try:
                        await add_nodes_and_edges_bulk(driver, [], [], [], batch, embedder)
                    except TransientError as e:
                        app_retries += 1
                        if attempt + 1 == args.attempts:
                            failures.append(f'batch {index}: {str(e)[:120]}')
                        else:
                            await asyncio.sleep(random.random() * 0.1 * (attempt + 1))
                    except Exception as e:  # any other error is a finding, keep going
                        failures.append(f'batch {index}: {type(e).__name__}: {str(e)[:160]}')
                        break
                    else:
                        latencies.append((time.perf_counter() - started) * 1000)
                        committed.add(index)
                        break

        wall = time.perf_counter()
        await asyncio.gather(*[writer() for _ in range(writers)])
        wall = time.perf_counter() - wall

        expected = len(committed) * args.batch
        stored = sql(database, 'SELECT count(*) AS c FROM RELATES_TO')[0]['c']
        out = sql(database, "SELECT sum(outE('RELATES_TO').size()) AS c FROM Entity")[0]['c']
        into = sql(database, "SELECT sum(inE('RELATES_TO').size()) AS c FROM Entity")[0]['c']
        return {
            'writers': writers,
            'edges_per_s': expected / wall,
            'p50_ms': statistics.median(latencies) if latencies else float('nan'),
            'p95_ms': (
                statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20 else float('nan')
            ),
            'app_retries': app_retries,
            'failures': failures,
            'expected': expected,
            'stored': stored,
            'reached_out': out,
            'reached_in': into,
        }
    finally:
        await driver.close()
        admin(f'drop database {database}')


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument('--writers', type=int, nargs='+', default=[1, 2, 8])
    p.add_argument('--nodes', type=int, default=20000)
    p.add_argument('--hubs', type=int, default=50)
    p.add_argument('--hub-share', type=float, default=0.5, help='share of endpoints on a hub')
    p.add_argument('--batch', type=int, default=25, help='edges per write (one add_episode)')
    p.add_argument('--batches', type=int, default=120)
    p.add_argument('--dim', type=int, default=128)
    p.add_argument('--attempts', type=int, default=5, help='app-level retries per batch')
    p.add_argument('--seed', type=int, default=7)
    p.add_argument(
        '--write-concurrency',
        type=int,
        default=1,
        help="the driver's write_concurrency: writers admitted at once; 0 = unlimited",
    )
    p.add_argument(
        '--retry-delay',
        type=float,
        default=None,
        help="initial delay (s) of the neo4j driver's transaction retry; its default is 1.0",
    )
    args = p.parse_args()

    print(
        f'{args.nodes} entities ({args.hubs} hubs take {args.hub_share:.0%} of endpoints), '
        f'{args.batches} batches x {args.batch} edges, dim {args.dim}\n'
    )
    header = (
        f'{"writers":>7} {"edges/s":>8} {"p50 ms":>8} {"p95 ms":>8} {"retries":>8} {"failed":>7} '
        f'{"expected":>9} {"records":>8} {"out":>8} {"in":>8}  audit'
    )
    print(header + '\n' + '-' * len(header))
    bad = False
    for writers in args.writers:
        r = await scenario(args, writers)
        consistent = r['expected'] == r['stored'] == r['reached_out'] == r['reached_in']
        bad |= not consistent
        print(
            f'{r["writers"]:7d} {r["edges_per_s"]:8.0f} {r["p50_ms"]:8.0f} {r["p95_ms"]:8.0f} '
            f'{r["app_retries"]:8d} {len(r["failures"]):7d} {r["expected"]:9d} {r["stored"]:8d} '
            f'{r["reached_out"]:8d} {r["reached_in"]:8d}  {"ok" if consistent else "MISMATCH"}'
        )
        for failure in r['failures'][:3]:
            print(f'        {failure}')
    return 1 if bad else 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
