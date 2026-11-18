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

Helpers shared by the TigerGraph operation classes: vertices and edges are addressed by type and uuid, so what differs
between the Graphiti node and edge kinds is only their attributes.
"""

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from graphiti_core.driver.query_executor import QueryExecutor
from graphiti_core.driver.tigergraph.client import (
    TigerGraphClient,
    TigerGraphError,
    flatten,
    flatten_edge,
    wrap,
)
from graphiti_core.driver.tigergraph.convert import chunked, dt_to_str, str_to_dt_required

logger = logging.getLogger(__name__)

# Vertices per REST++ call
UPSERT_BATCH = 500

# GSQL has no unbounded LIMIT
NO_LIMIT = 2147483647


def client_of(executor: QueryExecutor) -> TigerGraphClient:
    return executor.client  # type: ignore[attr-defined]


async def save_vertices(
    executor: QueryExecutor, vertex_type: str, rows: dict[str, dict[str, Any]]
) -> None:
    """Upsert ``{uuid: plain attributes}`` (wrapped here), in REST++ calls of ``UPSERT_BATCH`` vertices."""
    client = client_of(executor)
    for batch in chunked(list(rows.items()), UPSERT_BATCH):
        await client.upsert(vertices={vertex_type: {uuid: wrap(attrs) for uuid, attrs in batch}})
    embedded = [
        (uuid, attrs['name_embedding'])
        for uuid, attrs in rows.items()
        if attrs.get('name_embedding')
    ]
    if embedded:
        # The index may apply writes out of order: remember a spread of this write, the last one included
        step = max(1, len(embedded) // PENDING_SAMPLE)
        sample = embedded[::step][:PENDING_SAMPLE]
        if sample[-1] != embedded[-1]:
            sample.append(embedded[-1])
        client.pending_vectors.setdefault(vertex_type, []).extend(sample)
        del client.pending_vectors[vertex_type][:-PENDING_KEPT]


# Vectors remembered per write, and in all, for ``wait_for_vector``
PENDING_SAMPLE = 4
PENDING_KEPT = 16

VECTOR_QUERIES = {'Entity': 'entity_vsearch', 'Community': 'community_vsearch'}


async def wait_for_vector(executor: QueryExecutor, vertex_type: str, timeout: float = 10.0) -> None:
    """Return once a vector search sees the vectors this client recently wrote to ``vertex_type``.

    TigerGraph's vector index trails its writes by up to about a second and does not apply them in order, so without this a
    search issued straight after a save can miss it. Only a sample of each write is probed.
    """
    client = client_of(executor)
    pending = list(client.pending_vectors.get(vertex_type, ()))
    if not pending:
        return
    deadline = time.monotonic() + timeout

    async def seen(uuid: str, vector: list[float]) -> bool:
        results = await client.run_query(
            VECTOR_QUERIES[vertex_type],
            {'qv': vector, 'k': 50, 'use_groups': False, 'groups': [''], 'min_score': 0.0},
        )
        return any(h['uuid'] == uuid for block in results for h in (block.get('@@top') or []))

    while pending:
        visible = await asyncio.gather(*[seen(u, v) for u, v in pending])
        pending = [p for p, ok in zip(pending, visible, strict=True) if not ok]
        if pending and time.monotonic() > deadline:
            logger.warning(
                'TigerGraph vector index did not show %d %s vectors within %.0f s',
                len(pending),
                vertex_type,
                timeout,
            )
            break
        if pending:
            await asyncio.sleep(0.05)
    client.pending_vectors[vertex_type] = [
        p for p in client.pending_vectors.get(vertex_type, []) if p in pending
    ]


async def get_vertices(
    executor: QueryExecutor, vertex_type: str, uuids: Iterable[str]
) -> list[dict[str, Any]]:
    """Flat attributes of the vertices that exist, in the order asked (one point read per uuid, run concurrently)."""
    client = client_of(executor)
    found = await asyncio.gather(*[client.get_vertex(vertex_type, u) for u in uuids])
    return [v for v in found if v is not None]


async def vertices_in_groups(
    executor: QueryExecutor,
    vertex_type: str,
    group_ids: list[str],
    limit: int | None,
    uuid_cursor: str | None,
) -> list[dict[str, Any]]:
    """Newest uuid first; a cursor returns the uuids below it (the order the Cypher drivers page in)."""
    results = await client_of(executor).interpret(
        'SET<STRING> gids, STRING cursor, INT lim',
        f'S = SELECT v FROM {vertex_type}:v WHERE v.group_id IN gids AND (cursor == "" OR v.uuid < cursor)'
        ' ORDER BY v.uuid DESC LIMIT lim; PRINT S;',
        {
            'gids': group_ids,
            'cursor': uuid_cursor or '',
            'lim': limit if limit is not None else NO_LIMIT,
        },
    )
    return [flatten(v) for v in ((results[0].get('S') if results else None) or [])]


async def delete_vertices(executor: QueryExecutor, vertex_type: str, uuids: list[str]) -> None:
    """Delete vertices (and with them their edges)."""
    client = client_of(executor)
    await asyncio.gather(*[client.delete_vertex(vertex_type, u) for u in uuids])


async def delete_vertices_in_group(
    executor: QueryExecutor, vertex_type: str, group_id: str
) -> None:
    await client_of(executor).interpret(
        'STRING gid',
        f'S = SELECT v FROM {vertex_type}:v WHERE v.group_id == gid; DELETE v FROM S:v;',
        {'gid': group_id},
    )


def edge_row(
    source_type: str,
    source: str,
    edge_type: str,
    target_type: str,
    target: str,
    attrs: dict[str, Any],
) -> tuple:
    return (source_type, source, edge_type, target_type, target, attrs)


def edges_payload(rows: list[tuple]) -> dict[str, Any]:
    """The REST++ ``edges`` section for rows of ``edge_row``: parallel edges of a pair share one list."""
    grouped: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for source_type, source, edge_type, target_type, target, attrs in rows:
        grouped[(source_type, source, edge_type, target_type, target)].append(wrap(attrs))
    payload: dict[str, Any] = {}
    for (source_type, source, edge_type, target_type, target), attrs in grouped.items():
        by_edge = payload.setdefault(source_type, {}).setdefault(source, {})
        by_edge.setdefault(edge_type, {}).setdefault(target_type, {})[target] = attrs
    return payload


async def save_edges(executor: QueryExecutor, rows: list[tuple]) -> None:
    """Upsert edges; an edge whose end vertex does not exist is refused (as the Cypher backends, which ``MATCH`` both
    ends first), instead of creating an empty placeholder vertex."""
    client = client_of(executor)
    for batch in chunked(rows, UPSERT_BATCH):
        accepted = await client.upsert(edges=edges_payload(batch), vertex_must_exist=True)
        if accepted['accepted_edges'] < len(batch):
            raise TigerGraphError(
                f'TigerGraph accepted {accepted["accepted_edges"]} of {len(batch)} edges: an end vertex is missing'
            )


async def edges_where(
    executor: QueryExecutor, edge_type: str, signature: str, condition: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    """Flat attributes of the edges of one type that satisfy a GSQL condition over ``e`` (the edge), ``s`` and ``t``.

    TigerGraph cannot look an edge up by an attribute, so this scans the type: the by-uuid and by-group reads cost
    ``O(edges of the type)``, and Graphiti's hot paths (saves, BFS, similarity) avoid them.
    """
    results = await client_of(executor).interpret(
        signature,
        'ListAccum<EDGE> @@edges; S = {Entity.*, Episodic.*, Community.*, Saga.*};'
        f' R = SELECT t FROM S:s -({edge_type}:e)-> :t WHERE {condition} ACCUM @@edges += e;'
        ' PRINT @@edges;',
        params,
    )
    return [flatten_edge(e) for e in ((results[0].get('@@edges') if results else None) or [])]


async def edges_by_uuids(
    executor: QueryExecutor, edge_type: str, uuids: list[str]
) -> list[dict[str, Any]]:
    if not uuids:
        return []
    return await edges_where(
        executor, edge_type, 'SET<STRING> ids', 'e.uuid IN ids', {'ids': uuids}
    )


async def edges_in_groups(
    executor: QueryExecutor,
    edge_type: str,
    group_ids: list[str],
    limit: int | None,
    uuid_cursor: str | None,
) -> list[dict[str, Any]]:
    rows = await edges_where(
        executor,
        edge_type,
        'SET<STRING> gids, STRING cursor',
        'e.group_id IN gids AND (cursor == "" OR e.uuid < cursor)',
        {'gids': group_ids, 'cursor': uuid_cursor or ''},
    )
    rows.sort(key=lambda r: r['uuid'], reverse=True)
    return rows[:limit] if limit is not None else rows


async def delete_edges(executor: QueryExecutor, edge_type: str, uuids: list[str]) -> None:
    if not uuids:
        return
    await client_of(executor).interpret(
        'SET<STRING> ids',
        f'S = {{Entity.*, Episodic.*, Community.*, Saga.*}}; R = SELECT t FROM S:s -({edge_type}:e)-> :t'
        ' WHERE e.uuid IN ids ACCUM DELETE (e);',
        {'ids': uuids},
    )


def plain_edge_attrs(edge: Any) -> dict[str, Any]:
    """The attributes of an edge that has no content of its own (mentions, membership, episode order)."""
    return {'uuid': edge.uuid, 'group_id': edge.group_id, 'created_at': dt_to_str(edge.created_at)}


def plain_edge_kwargs(attrs: dict[str, Any]) -> dict[str, Any]:
    """The constructor arguments shared by the edges that have no content of their own."""
    return {
        'uuid': attrs['uuid'],
        'group_id': attrs.get('group_id', ''),
        'source_node_uuid': attrs['source_node_uuid'],
        'target_node_uuid': attrs['target_node_uuid'],
        'created_at': str_to_dt_required(attrs.get('created_at')),
    }


ALL_EDGE_TYPES = 'RELATES_TO|MENTIONS|HAS_MEMBER|HAS_EPISODE|NEXT_EPISODE'


async def delete_edges_anywhere(executor: QueryExecutor, uuids: list[str]) -> None:
    """Delete the edges with these uuids whatever their type (the generic ``Edge`` deletes name no type)."""
    if not uuids:
        return
    await client_of(executor).interpret(
        'SET<STRING> ids',
        'S = {Entity.*, Episodic.*, Community.*, Saga.*};'
        f' R = SELECT t FROM S:s -(({ALL_EDGE_TYPES}):e)-> :t WHERE e.uuid IN ids ACCUM DELETE (e);',
        {'ids': uuids},
    )
