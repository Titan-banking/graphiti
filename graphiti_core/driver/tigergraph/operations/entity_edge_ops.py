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
"""

import logging
from typing import Any

from graphiti_core.driver.operations.entity_edge_ops import EntityEdgeOperations
from graphiti_core.driver.query_executor import QueryExecutor, Transaction
from graphiti_core.driver.tigergraph.convert import (
    dt_to_str,
    dumps,
    float_list,
    loads,
    str_to_dt,
    str_to_dt_required,
)
from graphiti_core.driver.tigergraph.operations.common import (
    delete_edges,
    edge_row,
    edges_by_uuids,
    edges_in_groups,
    edges_where,
    save_edges,
)
from graphiti_core.edges import EntityEdge
from graphiti_core.errors import EdgeNotFoundError

logger = logging.getLogger(__name__)


def entity_edge_row(edge: EntityEdge) -> tuple:
    return edge_row(
        'Entity',
        edge.source_node_uuid,
        'RELATES_TO',
        'Entity',
        edge.target_node_uuid,
        {
            'uuid': edge.uuid,
            'name': edge.name,
            'fact': edge.fact,
            'group_id': edge.group_id,
            'episodes': list(edge.episodes or []),
            'created_at': dt_to_str(edge.created_at),
            'expired_at': dt_to_str(edge.expired_at),
            'valid_at': dt_to_str(edge.valid_at),
            'invalid_at': dt_to_str(edge.invalid_at),
            'reference_time': dt_to_str(edge.reference_time),
            'attributes': dumps(edge.attributes),
            'fact_embedding': float_list(edge.fact_embedding),
        },
    )


def entity_edge_from_attrs(attrs: dict[str, Any]) -> EntityEdge:
    """An ``EntityEdge`` from the flat attributes of an edge (``flatten_edge``), without its embedding."""
    return EntityEdge(
        uuid=attrs['uuid'],
        source_node_uuid=attrs['source_node_uuid'],
        target_node_uuid=attrs['target_node_uuid'],
        group_id=attrs.get('group_id', ''),
        created_at=str_to_dt_required(attrs.get('created_at')),
        name=attrs.get('name', ''),
        fact=attrs.get('fact', ''),
        episodes=list(attrs.get('episodes') or []),
        expired_at=str_to_dt(attrs.get('expired_at')),
        valid_at=str_to_dt(attrs.get('valid_at')),
        invalid_at=str_to_dt(attrs.get('invalid_at')),
        reference_time=str_to_dt(attrs.get('reference_time')),
        attributes=loads(attrs.get('attributes')),
    )


class TigerGraphEntityEdgeOperations(EntityEdgeOperations):
    async def save(
        self, executor: QueryExecutor, edge: EntityEdge, tx: Transaction | None = None
    ) -> None:
        await save_edges(executor, [entity_edge_row(edge)])
        logger.debug(f'Saved edge to Graph: {edge.uuid}')

    async def save_bulk(
        self,
        executor: QueryExecutor,
        edges: list[EntityEdge],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        await save_edges(executor, [entity_edge_row(e) for e in edges])

    async def delete(
        self, executor: QueryExecutor, edge: EntityEdge, tx: Transaction | None = None
    ) -> None:
        await delete_edges(executor, 'RELATES_TO', [edge.uuid])

    async def delete_by_uuids(
        self, executor: QueryExecutor, uuids: list[str], tx: Transaction | None = None
    ) -> None:
        await delete_edges(executor, 'RELATES_TO', uuids)

    async def get_by_uuid(self, executor: QueryExecutor, uuid: str) -> EntityEdge:
        edges = await self.get_by_uuids(executor, [uuid])
        if not edges:
            raise EdgeNotFoundError(uuid)
        return edges[0]

    async def get_by_uuids(self, executor: QueryExecutor, uuids: list[str]) -> list[EntityEdge]:
        rows = await edges_by_uuids(executor, 'RELATES_TO', uuids)
        return [entity_edge_from_attrs(r) for r in rows]

    async def get_by_group_ids(
        self,
        executor: QueryExecutor,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[EntityEdge]:
        rows = await edges_in_groups(executor, 'RELATES_TO', group_ids, limit, uuid_cursor)
        return [entity_edge_from_attrs(r) for r in rows]

    async def get_between_nodes(
        self, executor: QueryExecutor, source_node_uuid: str, target_node_uuid: str
    ) -> list[EntityEdge]:
        rows = await edges_where(
            executor,
            'RELATES_TO',
            'STRING src, STRING dst',
            's.uuid == src AND t.uuid == dst',
            {'src': source_node_uuid, 'dst': target_node_uuid},
        )
        return [entity_edge_from_attrs(r) for r in rows]

    async def get_by_node_uuid(self, executor: QueryExecutor, node_uuid: str) -> list[EntityEdge]:
        rows = await edges_where(
            executor, 'RELATES_TO', 'STRING id', 's.uuid == id OR t.uuid == id', {'id': node_uuid}
        )
        return [entity_edge_from_attrs(r) for r in rows]

    async def load_embeddings(self, executor: QueryExecutor, edge: EntityEdge) -> None:
        await self.load_embeddings_bulk(executor, [edge])

    async def load_embeddings_bulk(
        self, executor: QueryExecutor, edges: list[EntityEdge], batch_size: int = 100
    ) -> None:
        rows = await edges_by_uuids(executor, 'RELATES_TO', [e.uuid for e in edges])
        found = {r['uuid']: r.get('fact_embedding') for r in rows}
        for edge in edges:
            embedding = found.get(edge.uuid)
            if embedding:
                edge.fact_embedding = [float(x) for x in embedding]


async def existing_duplicate_pairs(
    executor: QueryExecutor, pairs: list[tuple[str, str]]
) -> set[tuple[str, str]]:
    """The ``(source, target)`` pairs that already have an ``IS_DUPLICATE_OF`` fact, in one scan of the facts."""
    if not pairs:
        return set()
    rows = await edges_where(
        executor,
        'RELATES_TO',
        'SET<STRING> srcs, SET<STRING> dsts',
        'e.name == "IS_DUPLICATE_OF" AND s.uuid IN srcs AND t.uuid IN dsts',
        {'srcs': sorted({s for s, _ in pairs}), 'dsts': sorted({d for _, d in pairs})},
    )
    found = {(r['source_node_uuid'], r['target_node_uuid']) for r in rows}
    return found & set(pairs)
