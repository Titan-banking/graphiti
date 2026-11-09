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

Load and latency comparison of FalkorDB and ArcadeDB behind Graphiti's own drivers and
search functions. Needs both servers (see --help for the container names it manages):

    docker compose -f docker-compose.arcadedb.yml up -d
    docker run -d --name falkordb-bench -p 6380:6379 --cpus 4 --memory 8g falkordb/falkordb:latest
    uv run python benchmarks/compare_arcadedb_falkordb.py --sizes 1000 10000 50000

Columns: FalkorDB (exact scan), ArcadeDB exact (use_vector_index=False, loaded without a
vector index), ArcadeDB ANN (HNSW, no quantization) and ArcadeDB ANN scalar. Each column is
loaded and measured on its own database. Reads are one query at a time (p50/p95 of the
per-call latency); writes report throughput and per-call latency at the stated concurrency.
"""

import argparse
import asyncio
import datetime as dt
import json
import platform
import random
import re
import string
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import httpx
import numpy as np
from neo4j.exceptions import TransientError

from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.arcadedb_driver import ArcadeDBDriver
from graphiti_core.driver.driver import GraphDriver
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder.client import EMBEDDING_DIM, EmbedderClient
from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.llm_client import LLMClient
from graphiti_core.nodes import EntityNode
from graphiti_core.search.search import search
from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF
from graphiti_core.search.search_filters import SearchFilters
from graphiti_core.search.search_utils import (
    edge_fulltext_search,
    edge_similarity_search,
    node_bfs_search,
    node_fulltext_search,
    node_similarity_search,
    rrf,
)
from graphiti_core.tracer import NoOpTracer
from graphiti_core.utils.bulk_utils import add_nodes_and_edges_bulk

SEED = 42
GROUPS = ['g0', 'g1', 'g2']
LIMIT = 10
BATCH = 500
FALKOR_CONTAINER = 'falkordb-bench'
ARCADE_CONTAINER = 'arcadedb-graphiti'
ARCADE_BOLT = 'bolt://localhost:7687'
ARCADE_HTTP = 'http://localhost:2481'
ARCADE_AUTH = ('root', 'playwithdata')
FALKOR_PORT = 6380

# (column key, label)
VARIANTS = [
    ('falkordb', 'FalkorDB'),
    ('arcadedb_exact', 'ArcadeDB exact'),
    ('arcadedb_ann', 'ArcadeDB ANN'),
    ('arcadedb_ann_scalar', 'ArcadeDB ANN scalar'),
]

_VOCAB_RNG = random.Random(SEED)
VOCAB = sorted(
    {
        ''.join(_VOCAB_RNG.choices(string.ascii_lowercase, k=_VOCAB_RNG.randint(4, 9)))
        for _ in range(2000)
    }
)


# --------------------------------------------------------------------------- data


def node_uuid(i: int) -> str:
    return str(UUID(int=i + 1))


def edge_uuid(i: int) -> str:
    return str(UUID(int=(1 << 64) + i + 1))


CLUSTERS = 64


def _unit_noise(g: np.random.Generator, dim: int) -> np.ndarray:
    v = g.standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def vec(kind: int, i: int, dim: int) -> list[float]:
    """Deterministic unit vector `i` of stream `kind`; nothing is kept in memory.

    With CLUSTERS > 0 a vector is its cluster's centroid plus noise, so every vector has
    close neighbours (as real embeddings do); uniform random vectors in 1024 dimensions are
    almost equidistant and give an ANN index nothing to find.
    """
    g = np.random.default_rng([SEED, kind, i])
    v = _unit_noise(g, dim)
    if CLUSTERS > 0:
        centroid = _unit_noise(np.random.default_rng([SEED, 9, kind, i % CLUSTERS]), dim)
        v = centroid + 0.8 * v
        v /= np.linalg.norm(v)
    return v.tolist()


def qvec(kind: int, i: int, dim: int) -> list[float]:
    """A query near stored vector `i`: the vector itself plus a little noise."""
    v = np.asarray(vec(kind, i, dim), dtype=np.float32)
    v += 0.3 * _unit_noise(np.random.default_rng([SEED, 7, kind, i]), dim)
    v /= np.linalg.norm(v)
    return v.tolist()


def words(tag: str, i: int, k: int) -> str:
    return ' '.join(random.Random(f'{tag}{i}').choices(VOCAB, k=k))


NOW = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)


def make_node(i: int, dim: int) -> EntityNode:
    return EntityNode(
        uuid=node_uuid(i),
        name=words('n', i, 3),
        group_id=GROUPS[i % len(GROUPS)],
        labels=['Entity'],
        summary=words('s', i, 8),
        name_embedding=vec(0, i, dim),
        created_at=NOW,
    )


def make_edge(i: int, n_nodes: int, dim: int) -> EntityEdge:
    rng = random.Random(f'e{i}')
    g = i % len(GROUPS)
    per_group = (n_nodes - g + len(GROUPS) - 1) // len(GROUPS)
    src = g + len(GROUPS) * rng.randrange(per_group)
    dst = g + len(GROUPS) * rng.randrange(per_group)
    return EntityEdge(
        uuid=edge_uuid(i),
        source_node_uuid=node_uuid(src),
        target_node_uuid=node_uuid(dst),
        group_id=GROUPS[g],
        name=rng.choice(['LIKES', 'KNOWS', 'WORKS_WITH', 'MANAGES']),
        fact=words('f', i, 6),
        fact_embedding=vec(1, i, dim),
        episodes=[],
        created_at=NOW,
    )


# --------------------------------------------------------------------------- timing


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def summarize(ms: list[float]) -> dict[str, Any]:
    return {'p50': pct(ms, 0.5), 'p95': pct(ms, 0.95), 'n': len(ms)}


async def measure(call, args_list: list[Any], warmup: int) -> list[float]:
    """Latency in ms of `call(*args)` run one at a time; the first `warmup` are dropped."""
    out: list[float] = []
    for i, args in enumerate(args_list):
        t0 = time.perf_counter()
        await call(*args)
        if i >= warmup:
            out.append((time.perf_counter() - t0) * 1000)
    return out


async def run_concurrent(coros_factory, count: int, concurrency: int) -> tuple[list[float], float]:
    """Run `count` calls with `concurrency` in flight; per-call ms and wall seconds."""
    sem = asyncio.Semaphore(concurrency)
    lat: list[float] = []

    async def one(i: int):
        async with sem:
            t0 = time.perf_counter()
            await coros_factory(i)
            lat.append((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    await asyncio.gather(*[one(i) for i in range(count)])
    return lat, time.perf_counter() - t0


# --------------------------------------------------------------------------- containers


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def container_running(name: str) -> bool:
    return sh('docker', 'inspect', '-f', '{{.State.Running}}', name) == 'true'


def only_run(keep: str, other: str) -> None:
    if container_running(other):
        sh('docker', 'stop', other)
    if not container_running(keep):
        sh('docker', 'start', keep)


def to_mib(text: str) -> float:
    m = re.match(r'([\d.]+)\s*([KMG]i?B)', text)
    if not m:
        return 0.0
    scale = {'KB': 1 / 1024, 'KIB': 1 / 1024, 'MB': 1, 'MIB': 1, 'GB': 1024, 'GIB': 1024}
    return float(m.group(1)) * scale[m.group(2).upper()]


class MemorySampler:
    """Peak `docker stats` memory of one container, sampled in the background."""

    def __init__(self, container: str):
        self.container = container
        self.peak = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                out = sh(
                    'docker', 'stats', '--no-stream', '--format', '{{.MemUsage}}', self.container
                )
                self.peak = max(self.peak, to_mib(out.split('/')[0].strip()))
            except subprocess.CalledProcessError:
                pass
            self._stop.wait(2)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        self._thread.join()


# --------------------------------------------------------------------------- engines


class Engine:
    """One benchmark column: how to create, load, size and drop its database."""

    def __init__(self, key: str, dim: int):
        self.key = key
        self.dim = dim
        self.name = f'bench_{key}_{int(time.time())}'
        self.driver: GraphDriver
        self.disk_base: float | None = None

    @property
    def container(self) -> str:
        return FALKOR_CONTAINER if self.key == 'falkordb' else ARCADE_CONTAINER

    async def open(self) -> None:
        if self.key == 'falkordb':
            # Its default 1 s read timeout aborts exact scans (and anything queued behind
            # them) at larger sizes; measure them instead.
            sh(
                'docker',
                'exec',
                FALKOR_CONTAINER,
                'redis-cli',
                'GRAPH.CONFIG',
                'SET',
                'TIMEOUT',
                '0',
            )
            self.driver = FalkorDriver(host='localhost', port=FALKOR_PORT, database=self.name)
        else:
            httpx.post(
                f'{ARCADE_HTTP}/api/v1/server',
                json={'command': f'create database {self.name}'},
                auth=ARCADE_AUTH,
                timeout=60,
            ).raise_for_status()
            self.driver = ArcadeDBDriver(
                uri=ARCADE_BOLT,
                user=ARCADE_AUTH[0],
                password=ARCADE_AUTH[1],
                database=self.name,
                http_uri=ARCADE_HTTP,
                embedding_dim=self.dim,
                use_vector_index=self.key != 'arcadedb_exact',
                vector_quantization='scalar' if self.key == 'arcadedb_ann_scalar' else 'none',
            )
        await self.driver.build_indices_and_constraints()
        self.disk_base = self._du_mb()

    async def close(self) -> None:
        if self.key == 'falkordb':
            await self.driver.client.select_graph(self.name).delete()  # type: ignore[attr-defined]
        await self.driver.close()
        if self.key != 'falkordb':
            httpx.post(
                f'{ARCADE_HTTP}/api/v1/server',
                json={'command': f'drop database {self.name}'},
                auth=ARCADE_AUTH,
                timeout=120,
            )

    def _du_mb(self) -> float | None:
        try:
            if self.key == 'falkordb':
                sh('docker', 'exec', FALKOR_CONTAINER, 'redis-cli', 'SAVE')
                path = '/var/lib/falkordb/data'
            else:
                path = f'/home/arcadedb/databases/{self.name}'
            out = sh('docker', 'exec', self.container, 'sh', '-c', f'du -sk {path} | cut -f1')
            return float(out) / 1024
        except (subprocess.CalledProcessError, ValueError):
            return None

    def disk_mb(self) -> float | None:
        """On-disk size after the load. ArcadeDB: the database's own directory. FalkorDB
        shares one data dir between graphs, so it is the growth since this graph was created."""
        now = self._du_mb()
        if now is None:
            return None
        if self.key == 'falkordb':
            return None if self.disk_base is None else now - self.disk_base
        return now


# --------------------------------------------------------------------------- measurements


async def load(engine: Engine, n: int, concurrency: int) -> dict[str, float]:
    embedder = Mock(spec=EmbedderClient)
    driver, dim = engine.driver, engine.dim
    sem = asyncio.Semaphore(concurrency)

    retries = 0

    async def write(nodes: list[EntityNode], edges: list[EntityEdge]):
        nonlocal retries
        async with sem:
            # ArcadeDB reports MVCC page conflicts as transient errors; the driver's
            # managed transaction retries for 30 s, then gives up. Retry like an app would.
            for attempt in range(20):
                try:
                    await add_nodes_and_edges_bulk(driver, [], [], nodes, edges, embedder)
                    return
                except TransientError:
                    retries += 1
                    await asyncio.sleep(random.random() * 0.1 * (attempt + 1))
            raise RuntimeError('bulk write kept hitting transient errors')

    t0 = time.perf_counter()
    await asyncio.gather(
        *[
            write([make_node(i, dim) for i in range(s, min(s + BATCH, n))], [])
            for s in range(0, n, BATCH)
        ]
    )
    node_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    await asyncio.gather(
        *[
            write([], [make_edge(i, n, dim) for i in range(s, min(s + BATCH, 2 * n))])
            for s in range(0, 2 * n, BATCH)
        ]
    )
    edge_s = time.perf_counter() - t0
    return {'nodes_per_s': n / node_s, 'edges_per_s': 2 * n / edge_s, 'retries': retries}


async def bench_engine(engine: Engine, n: int, args) -> dict[str, Any]:
    d, dim, q, w = engine.driver, engine.dim, args.queries, args.warmup
    exact_q = args.exact_queries
    r: dict[str, Any] = {}
    rng = random.Random(SEED)

    # 1. bulk write
    r['bulk_write'] = await load(engine, n, args.concurrency)
    r['disk_mb'] = engine.disk_mb()

    # 2. single saves, concurrency 1 and 8
    base = 10_000_000
    for c in (1, args.concurrency):
        nodes = [make_node(base + c * 1000 + i, dim) for i in range(q)]
        lat, wall = await run_concurrent(lambda i, nodes=nodes: nodes[i].save(d), q, c)
        r[f'save_node_c{c}'] = {**summarize(lat), 'per_s': q / wall}
        edges = [make_edge(base + c * 1000 + i, n, dim) for i in range(q)]
        lat, wall = await run_concurrent(lambda i, edges=edges: edges[i].save(d), q, c)
        r[f'save_edge_c{c}'] = {**summarize(lat), 'per_s': q / wall}

    total = q + w
    pick = [rng.randrange(n) for _ in range(total)]
    pick_edge = [rng.randrange(2 * n) for _ in range(total)]
    groups = [GROUPS[i % len(GROUPS)] for i in pick]
    filt = SearchFilters()

    def text(i: int) -> str:
        return ' '.join(words('n', i, 3).split()[:2])

    def edge_text(i: int) -> str:
        return ' '.join(words('f', i, 6).split()[:3])

    # 3. fulltext
    r['node_fulltext'] = summarize(
        await measure(
            lambda t, g: node_fulltext_search(d, t, filt, [g], limit=LIMIT),
            [(text(i), g) for i, g in zip(pick, groups, strict=True)],
            w,
        )
    )
    r['edge_fulltext'] = summarize(
        await measure(
            lambda t, g: edge_fulltext_search(d, t, filt, [g], limit=LIMIT),
            [(edge_text(i), GROUPS[i % 3]) for i in pick_edge],
            w,
        )
    )

    # 4. node similarity; the query is a stored vector (a real hit exists)
    node_args = [(qvec(0, i, dim), g) for i, g in zip(pick, groups, strict=True)]

    async def node_sim(v, g):
        return await node_similarity_search(d, v, filt, [g], limit=LIMIT, min_score=0.0)

    n_exact = (
        exact_q if engine.key != 'arcadedb_ann' and engine.key != 'arcadedb_ann_scalar' else total
    )
    r['node_similarity'] = summarize(
        await measure(node_sim, node_args[:n_exact], min(w, n_exact // 4))
    )

    # 5. edge similarity: exact everywhere
    edge_args = [(qvec(1, i, dim), GROUPS[i % 3]) for i in pick_edge[: exact_q + w]]

    async def edge_sim(v, g):
        return await edge_similarity_search(d, v, None, None, filt, [g], limit=LIMIT, min_score=0.0)

    r['edge_similarity'] = summarize(await measure(edge_sim, edge_args, min(w, exact_q // 4)))

    # 6. BFS depth 2 from 10 origins
    origins = [node_uuid(rng.randrange(n)) for _ in range(10)]
    r['node_bfs'] = summarize(
        await measure(
            lambda: node_bfs_search(d, origins, filt, 2, None, LIMIT), [()] * min(total, 60), 5
        )
    )

    # 7. full hybrid search (RRF) without an LLM: the query vector is passed in
    clients = GraphitiClients(
        driver=d,
        llm_client=Mock(spec=LLMClient),
        embedder=Mock(spec=EmbedderClient),
        cross_encoder=Mock(spec=CrossEncoderClient),
        tracer=NoOpTracer(),
    )

    async def hybrid(i: int, g: str):
        return await search(
            clients,
            text(i),
            [g],
            COMBINED_HYBRID_SEARCH_RRF,
            filt,
            query_vector=qvec(0, i, dim),
        )

    r['hybrid_search'] = summarize(
        await measure(
            hybrid, [(i, g) for i, g in zip(pick, groups, strict=True)][: min(total, 60)], 5
        )
    )

    # ANN recall@10 against the exact scan on the same data (arcadedb ANN columns only)
    if engine.key in ('arcadedb_ann', 'arcadedb_ann_scalar'):
        exact_driver = ArcadeDBDriver(
            uri=ARCADE_BOLT,
            user=ARCADE_AUTH[0],
            password=ARCADE_AUTH[1],
            database=engine.name,
            http_uri=ARCADE_HTTP,
            use_vector_index=False,
        )
        hits = tot = 0
        try:
            for v, g in node_args[w : w + 50]:
                ann = {
                    x.uuid
                    for x in await node_similarity_search(
                        d, v, filt, [g], limit=LIMIT, min_score=0.0
                    )
                }
                ex = {
                    x.uuid
                    for x in await node_similarity_search(
                        exact_driver, v, filt, [g], limit=LIMIT, min_score=0.0
                    )
                }
                hits += len(ann & ex)
                tot += len(ex)
        finally:
            await exact_driver.close()
        r['ann_recall_at_10'] = hits / tot if tot else None

    # 8. ArcadeDB's engine-side fused search vs Graphiti's Python RRF over the same two legs
    if engine.key == 'arcadedb_ann':
        r['engine_hybrid'] = await bench_engine_hybrid(engine, node_args, pick, text, w)

    return r


async def bench_engine_hybrid(engine: Engine, node_args, pick, text, warmup: int) -> dict[str, Any]:
    d = engine.driver
    filt = SearchFilters()
    calls = list(zip(pick, [a[0] for a in node_args], strict=True))[: warmup + 40]

    async def python_rrf(i: int, v: list[float]):
        ft, sim = await asyncio.gather(
            node_fulltext_search(d, text(i), filt, None, limit=LIMIT),
            node_similarity_search(d, v, filt, None, limit=LIMIT, min_score=0.0),
        )
        return rrf([[x.uuid for x in ft], [x.uuid for x in sim]])

    out: dict[str, Any] = {'python_rrf': summarize(await measure(python_rrf, calls, warmup))}

    async with httpx.AsyncClient(auth=ARCADE_AUTH, timeout=120) as client:

        async def engine_fused(i: int, v: list[float]):
            body = {
                'vectorIndexName': 'entity_name_embedding',  # the name given in the DDL
                'queryVector': v,
                'k': LIMIT,
                'fulltextQuery': text(i),
                'fulltextIndexName': 'node_name_and_summary',
            }
            resp = await client.post(f'{ARCADE_HTTP}/api/v1/vector/{engine.name}/hybrid', json=body)
            resp.raise_for_status()
            return resp.json()

        try:
            out['engine_fused'] = summarize(await measure(engine_fused, calls, warmup))
        except httpx.HTTPError as e:
            out['engine_fused'] = {'error': str(e)[:200]}
    return out


# --------------------------------------------------------------------------- reporting


def fmt(v: dict[str, Any] | float | None) -> str:
    if v is None:
        return 'n/a'
    if isinstance(v, dict):
        if 'error' in v:
            return 'err'
        if 'p50' in v:
            return f'{v["p50"]:.1f} / {v["p95"]:.1f}'
    return f'{v:.0f}'


def render(results: dict[str, Any], args, meta: dict[str, Any]) -> str:
    sizes = [str(s) for s in args.sizes]
    lines = [
        f'# FalkorDB vs ArcadeDB benchmark ({dt.date.today().isoformat()})',
        '',
        '## Setup',
        '',
        *[f'- {k}: {v}' for k, v in meta.items()],
        f'- dim={args.dim}, reads: {args.queries} queries after {args.warmup} warmup '
        f'({args.exact_queries} for exact full scans), write concurrency {args.concurrency}, batch {BATCH}, '
        f'limit {LIMIT}, min_score 0 for similarity (random vectors would never pass 0.6)',
        '- latencies are `p50 / p95` in ms; ANN recall is recall@10 against the exact scan of the same data',
        '- ArcadeDB runs in a JVM started with MaxRAMPercentage=75, so its container memory is dominated by '
        'heap sizing rather than data size; FalkorDB is the data resident in Redis. Disk is the database '
        'directory (ArcadeDB pre-sizes 16 bucket files per type) or, for FalkorDB, the RDB after SAVE.',
        '',
    ]

    def row(label: str, metric: str, size: str) -> str:
        cells = []
        for key, _ in VARIANTS:
            v = results.get(key, {}).get(size, {}).get(metric)
            cells.append(fmt(v))
        return f'| {label} | {size} | ' + ' | '.join(cells) + ' |'

    header = '| metric | N nodes | ' + ' | '.join(label for _, label in VARIANTS) + ' |'
    sep = '|---|---|' + '---|' * len(VARIANTS)
    lines += [header, sep]

    def rate(metric: str, field: str, label: str):
        for s in sizes:
            cells = []
            for key, _ in VARIANTS:
                m = results.get(key, {}).get(s, {}).get(metric)
                cells.append('n/a' if not m else f'{m[field]:.0f}')
            lines.append(f'| {label} | {s} | ' + ' | '.join(cells) + ' |')

    rate('bulk_write', 'nodes_per_s', 'bulk write nodes/s')
    rate('bulk_write', 'edges_per_s', 'bulk write edges/s')
    for c in (1, args.concurrency):
        for s in sizes:
            lines.append(row(f'save node c={c} (ms)', f'save_node_c{c}', s))
        for s in sizes:
            lines.append(row(f'save edge c={c} (ms)', f'save_edge_c{c}', s))
    for metric, label in [
        ('node_fulltext', 'node fulltext (ms)'),
        ('edge_fulltext', 'edge fulltext (ms)'),
        ('node_similarity', 'node similarity (ms)'),
        ('edge_similarity', 'edge similarity, exact (ms)'),
        ('node_bfs', 'node BFS depth 2 (ms)'),
        ('hybrid_search', 'hybrid search RRF (ms)'),
    ]:
        for s in sizes:
            lines.append(row(label, metric, s))
    for s in sizes:
        cells = []
        for key, _ in VARIANTS:
            v = results.get(key, {}).get(s, {}).get('ann_recall_at_10')
            cells.append('n/a' if v is None else f'{v:.3f}')
        lines.append(f'| ANN recall@10 | {s} | ' + ' | '.join(cells) + ' |')
    for s in sizes:
        cells = []
        for key, _ in VARIANTS:
            m = results.get(key, {}).get(s, {})
            disk = m.get('disk_mb')
            peak = m.get('peak_mem_mib')
            cells.append(
                'n/a'
                if peak is None
                else f'{peak:.0f} MiB peak / {disk if disk is not None else "?"} MB disk'
            )
        lines.append(f'| container memory / disk | {s} | ' + ' | '.join(cells) + ' |')

    lines += [
        '',
        '## ArcadeDB engine-fused search vs Graphiti Python RRF (informational, ungrouped)',
        '',
    ]
    lines += [
        '| N nodes | Graphiti Python RRF p50 / p95 | engine `/vector/{db}/hybrid` p50 / p95 |',
        '|---|---|---|',
    ]
    for s in sizes:
        h = results.get('arcadedb_ann', {}).get(s, {}).get('engine_hybrid', {})
        lines.append(f'| {s} | {fmt(h.get("python_rrf"))} | {fmt(h.get("engine_fused"))} |')
    lines.append('')
    return '\n'.join(lines)


# --------------------------------------------------------------------------- main


async def run(args) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for key, label in VARIANTS:
        keep, other = (
            (FALKOR_CONTAINER, ARCADE_CONTAINER)
            if key == 'falkordb'
            else (ARCADE_CONTAINER, FALKOR_CONTAINER)
        )
        if not args.no_docker_control:
            only_run(keep, other)
            await asyncio.sleep(8)  # let the restarted server come up
        results[key] = {}
        if key == 'arcadedb_exact':
            # The server was just (re)started: run a throwaway load so the JVM is warm
            # and the first ArcadeDB column is not charged for it.
            print('== ArcadeDB JVM warmup (discarded)', flush=True)
            warm = Engine(key, args.dim)
            await warm.open()
            try:
                await load(warm, 2000, args.concurrency)
            finally:
                await warm.close()
        for n in args.sizes:
            print(f'== {label} N={n}', flush=True)
            engine = Engine(key, args.dim)
            with MemorySampler(engine.container) as mem:
                await engine.open()
                try:
                    res = await bench_engine(engine, n, args)
                finally:
                    await engine.close()
            res['peak_mem_mib'] = mem.peak
            results[key][str(n)] = res
            Path(args.out).with_suffix('.json').write_text(json.dumps(results, indent=1))
    return results


def environment(pr_sha: str) -> dict[str, Any]:
    def safe(*cmd: str) -> str:
        try:
            return sh(*cmd)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return 'unknown'

    arcade = (
        httpx.get(f'{ARCADE_HTTP}/api/v1/server', auth=ARCADE_AUTH, timeout=10)
        .json()
        .get('version', 'unknown')
    )
    return {
        'ArcadeDB': f'{arcade}, image {safe("docker", "inspect", "-f", "{{index .RepoDigests 0}}", "arcadedata/arcadedb:26.12.1-SNAPSHOT")}',
        'FalkorDB image': safe(
            'docker', 'inspect', '-f', '{{index .RepoDigests 0}}', 'falkordb/falkordb:latest'
        ),
        'container limits': '4 CPU / 8 GB each (docker), one engine running at a time',
        'host': f'{platform.platform()}, {safe("sysctl", "-n", "machdep.cpu.brand_string")}',
        'graphiti PR #1310 head (fetched)': pr_sha,
        'this branch': safe('git', 'rev-parse', 'HEAD'),
        'python': sys.version.split()[0],
    }


def main() -> None:
    global CLUSTERS
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument('--sizes', type=int, nargs='+', default=[1000, 10000, 50000])
    p.add_argument(
        '--dim',
        type=int,
        default=EMBEDDING_DIM,
        help='embedding dimension (default: Graphiti EMBEDDING_DIM)',
    )
    p.add_argument('--queries', type=int, default=200)
    p.add_argument('--warmup', type=int, default=20)
    p.add_argument(
        '--exact-queries',
        type=int,
        default=50,
        help='queries for exact full-scan legs (slow at scale)',
    )
    p.add_argument('--concurrency', type=int, default=8)
    p.add_argument(
        '--clusters',
        type=int,
        default=64,
        help='number of embedding clusters (0 = uniform random vectors, which have no '
        'neighbour structure for an ANN index to find)',
    )
    p.add_argument(
        '--pr-sha',
        default='15963f0616f29ea0bd5865033b47f1c22f1ff455',
        help='head of getzep/graphiti#1310 this branch was built from',
    )
    p.add_argument('--out', default=f'benchmarks/results/{dt.date.today().isoformat()}.md')
    p.add_argument(
        '--no-docker-control', action='store_true', help='do not stop/start the engine containers'
    )
    args = p.parse_args()
    CLUSTERS = args.clusters

    if not args.no_docker_control:
        # environment() asks the ArcadeDB server for its version; run() stops one at a time.
        for container in (ARCADE_CONTAINER, FALKOR_CONTAINER):
            if not container_running(container):
                sh('docker', 'start', container)
        time.sleep(10)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    meta = environment(args.pr_sha)
    meta['embeddings'] = (
        f'{args.clusters} clusters (centroid + noise), queries are a stored vector + noise'
        if args.clusters
        else 'uniform random unit vectors'
    )
    meta['FalkorDB TIMEOUT'] = (
        'raised from the 1000 ms default to 0 so slow exact scans are measured, not aborted'
    )
    results = asyncio.run(run(args))
    report = render(results, args, meta)
    Path(args.out).write_text(report)
    print(report)


if __name__ == '__main__':
    main()
