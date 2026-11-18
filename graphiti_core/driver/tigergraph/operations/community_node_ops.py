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

from graphiti_core.driver.operations.community_node_ops import CommunityNodeOperations
from graphiti_core.driver.query_executor import QueryExecutor, Transaction
from graphiti_core.driver.tigergraph.convert import dt_to_str, float_list, str_to_dt_required
from graphiti_core.driver.tigergraph.operations.common import (
    delete_vertices,
    delete_vertices_in_group,
    get_vertices,
    save_vertices,
    vertices_in_groups,
)
from graphiti_core.driver.tigergraph.operations.entity_node_ops import load_embeddings_of
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import CommunityNode

logger = logging.getLogger(__name__)


def community_attrs(node: CommunityNode) -> dict[str, Any]:
    return {
        'name': node.name,
        'group_id': node.group_id,
        'summary': node.summary,
        'created_at': dt_to_str(node.created_at),
        'labels': list(node.labels or []),
        'name_embedding': float_list(node.name_embedding),
    }


def community_node_from_attrs(attrs: dict[str, Any]) -> CommunityNode:
    return CommunityNode(
        uuid=attrs['uuid'],
        name=attrs.get('name', ''),
        group_id=attrs.get('group_id', ''),
        labels=list(attrs.get('labels') or []),
        created_at=str_to_dt_required(attrs.get('created_at')),
        summary=attrs.get('summary', ''),
    )


class TigerGraphCommunityNodeOperations(CommunityNodeOperations):
    async def save(
        self, executor: QueryExecutor, node: CommunityNode, tx: Transaction | None = None
    ) -> None:
        await save_vertices(executor, 'Community', {node.uuid: community_attrs(node)})
        logger.debug(f'Saved Community Node to Graph: {node.uuid}')

    async def save_bulk(
        self,
        executor: QueryExecutor,
        nodes: list[CommunityNode],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        await save_vertices(executor, 'Community', {n.uuid: community_attrs(n) for n in nodes})

    async def delete(
        self, executor: QueryExecutor, node: CommunityNode, tx: Transaction | None = None
    ) -> None:
        await delete_vertices(executor, 'Community', [node.uuid])

    async def delete_by_group_id(
        self,
        executor: QueryExecutor,
        group_id: str,
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        await delete_vertices_in_group(executor, 'Community', group_id)

    async def delete_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        await delete_vertices(executor, 'Community', uuids)

    async def get_by_uuid(self, executor: QueryExecutor, uuid: str) -> CommunityNode:
        nodes = await self.get_by_uuids(executor, [uuid])
        if not nodes:
            raise NodeNotFoundError(uuid)
        return nodes[0]

    async def get_by_uuids(self, executor: QueryExecutor, uuids: list[str]) -> list[CommunityNode]:
        return [
            community_node_from_attrs(a) for a in await get_vertices(executor, 'Community', uuids)
        ]

    async def get_by_group_ids(
        self,
        executor: QueryExecutor,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[CommunityNode]:
        rows = await vertices_in_groups(executor, 'Community', group_ids, limit, uuid_cursor)
        return [community_node_from_attrs(a) for a in rows]

    async def load_name_embedding(self, executor: QueryExecutor, node: CommunityNode) -> None:
        embeddings = await load_embeddings_of(executor, 'community_embeddings', [node.uuid])
        if node.uuid in embeddings:
            node.name_embedding = embeddings[node.uuid]
