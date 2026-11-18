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

from graphiti_core.driver.operations.entity_node_ops import EntityNodeOperations
from graphiti_core.driver.query_executor import QueryExecutor, Transaction
from graphiti_core.driver.tigergraph.convert import (
    dt_to_str,
    dumps,
    float_list,
    loads,
    str_to_dt_required,
)
from graphiti_core.driver.tigergraph.operations.common import (
    client_of,
    delete_vertices,
    delete_vertices_in_group,
    get_vertices,
    save_vertices,
    vertices_in_groups,
    wait_for_vector,
)
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EntityNode

logger = logging.getLogger(__name__)


def entity_attrs(node: EntityNode) -> dict[str, Any]:
    """The attributes of an entity vertex; the embedding goes in its vector attribute."""
    return {
        'name': node.name,
        'group_id': node.group_id,
        'summary': node.summary,
        'created_at': dt_to_str(node.created_at),
        'labels': list(dict.fromkeys([*node.labels, 'Entity'])),
        'attributes': dumps(node.attributes),
        'name_embedding': float_list(node.name_embedding),
    }


def entity_node_from_attrs(attrs: dict[str, Any]) -> EntityNode:
    """An ``EntityNode`` from the flat attributes of a vertex (its embedding is read apart: ``load_embeddings``)."""
    return EntityNode(
        uuid=attrs['uuid'],
        name=attrs.get('name', ''),
        group_id=attrs.get('group_id', ''),
        labels=list(attrs.get('labels') or ['Entity']),
        created_at=str_to_dt_required(attrs.get('created_at')),
        summary=attrs.get('summary', ''),
        attributes=loads(attrs.get('attributes')),
    )


async def load_embeddings_of(
    executor: QueryExecutor, query: str, uuids: list[str]
) -> dict[str, list[float]]:
    """``{uuid: embedding}`` from an installed ``*_embeddings`` query (``PRINT ... WITH VECTOR``)."""
    if not uuids:
        return {}
    # The vector is read from the index, which trails the write
    await wait_for_vector(executor, 'Entity' if query == 'entity_embeddings' else 'Community')
    results = await client_of(executor).run_query(query, {'ids': uuids})
    vertices = (results[0].get('T') if results else None) or []
    return {
        v['v_id']: [float(x) for x in v['Embeddings']['name_embedding']]
        for v in vertices
        if (v.get('Embeddings') or {}).get('name_embedding')
    }


class TigerGraphEntityNodeOperations(EntityNodeOperations):
    async def save(
        self, executor: QueryExecutor, node: EntityNode, tx: Transaction | None = None
    ) -> None:
        await save_vertices(executor, 'Entity', {node.uuid: entity_attrs(node)})
        logger.debug(f'Saved Node to Graph: {node.uuid}')

    async def save_bulk(
        self,
        executor: QueryExecutor,
        nodes: list[EntityNode],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        await save_vertices(executor, 'Entity', {n.uuid: entity_attrs(n) for n in nodes})

    async def delete(
        self, executor: QueryExecutor, node: EntityNode, tx: Transaction | None = None
    ) -> None:
        await delete_vertices(executor, 'Entity', [node.uuid])

    async def delete_by_group_id(
        self,
        executor: QueryExecutor,
        group_id: str,
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        await delete_vertices_in_group(executor, 'Entity', group_id)

    async def delete_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        await delete_vertices(executor, 'Entity', uuids)

    async def get_by_uuid(self, executor: QueryExecutor, uuid: str) -> EntityNode:
        nodes = await self.get_by_uuids(executor, [uuid])
        if not nodes:
            raise NodeNotFoundError(uuid)
        return nodes[0]

    async def get_by_uuids(self, executor: QueryExecutor, uuids: list[str]) -> list[EntityNode]:
        return [entity_node_from_attrs(a) for a in await get_vertices(executor, 'Entity', uuids)]

    async def get_by_group_ids(
        self,
        executor: QueryExecutor,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[EntityNode]:
        rows = await vertices_in_groups(executor, 'Entity', group_ids, limit, uuid_cursor)
        return [entity_node_from_attrs(a) for a in rows]

    async def load_embeddings(self, executor: QueryExecutor, node: EntityNode) -> None:
        await self.load_embeddings_bulk(executor, [node])

    async def load_embeddings_bulk(
        self, executor: QueryExecutor, nodes: list[EntityNode], batch_size: int = 100
    ) -> None:
        embeddings = await load_embeddings_of(
            executor, 'entity_embeddings', [n.uuid for n in nodes]
        )
        for node in nodes:
            if node.uuid in embeddings:
                node.name_embedding = embeddings[node.uuid]
