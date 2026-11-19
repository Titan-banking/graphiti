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

from graphiti_core.driver.operations.saga_node_ops import SagaNodeOperations
from graphiti_core.driver.query_executor import QueryExecutor, Transaction
from graphiti_core.driver.tigergraph.convert import dt_to_str, str_to_dt, str_to_dt_required
from graphiti_core.driver.tigergraph.operations.common import (
    delete_vertices,
    delete_vertices_in_group,
    get_vertices,
    save_vertices,
    vertices_in_groups,
)
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import SagaNode

logger = logging.getLogger(__name__)


def saga_attrs(node: SagaNode) -> dict[str, Any]:
    return {
        'name': node.name,
        'group_id': node.group_id,
        'summary': node.summary,
        'created_at': dt_to_str(node.created_at),
        'first_episode_uuid': node.first_episode_uuid or '',
        'last_episode_uuid': node.last_episode_uuid or '',
        'last_summarized_at': dt_to_str(node.last_summarized_at),
        'last_summarized_episode_valid_at': dt_to_str(node.last_summarized_episode_valid_at),
        'labels': list(node.labels or []),
    }


def saga_node_from_attrs(attrs: dict[str, Any]) -> SagaNode:
    return SagaNode(
        uuid=attrs['uuid'],
        name=attrs.get('name', ''),
        group_id=attrs.get('group_id', ''),
        labels=list(attrs.get('labels') or []),
        created_at=str_to_dt_required(attrs.get('created_at')),
        summary=attrs.get('summary', ''),
        first_episode_uuid=attrs.get('first_episode_uuid') or None,
        last_episode_uuid=attrs.get('last_episode_uuid') or None,
        last_summarized_at=str_to_dt(attrs.get('last_summarized_at')),
        last_summarized_episode_valid_at=str_to_dt(attrs.get('last_summarized_episode_valid_at')),
    )


class TigerGraphSagaNodeOperations(SagaNodeOperations):
    async def save(
        self, executor: QueryExecutor, node: SagaNode, tx: Transaction | None = None
    ) -> None:
        await save_vertices(executor, 'Saga', {node.uuid: saga_attrs(node)})
        logger.debug(f'Saved Saga Node to Graph: {node.uuid}')

    async def save_bulk(
        self,
        executor: QueryExecutor,
        nodes: list[SagaNode],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        await save_vertices(executor, 'Saga', {n.uuid: saga_attrs(n) for n in nodes})

    async def delete(
        self, executor: QueryExecutor, node: SagaNode, tx: Transaction | None = None
    ) -> None:
        await delete_vertices(executor, 'Saga', [node.uuid])

    async def delete_by_group_id(
        self,
        executor: QueryExecutor,
        group_id: str,
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        await delete_vertices_in_group(executor, 'Saga', group_id)

    async def delete_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        await delete_vertices(executor, 'Saga', uuids)

    async def get_by_uuid(self, executor: QueryExecutor, uuid: str) -> SagaNode:
        nodes = await self.get_by_uuids(executor, [uuid])
        if not nodes:
            raise NodeNotFoundError(uuid)
        return nodes[0]

    async def get_by_uuids(self, executor: QueryExecutor, uuids: list[str]) -> list[SagaNode]:
        return [saga_node_from_attrs(a) for a in await get_vertices(executor, 'Saga', uuids)]

    async def get_by_group_ids(
        self,
        executor: QueryExecutor,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[SagaNode]:
        rows = await vertices_in_groups(executor, 'Saga', group_ids, limit, uuid_cursor)
        return [saga_node_from_attrs(a) for a in rows]
