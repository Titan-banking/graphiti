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

from graphiti_core.driver.operations.has_episode_edge_ops import HasEpisodeEdgeOperations
from graphiti_core.driver.query_executor import QueryExecutor, Transaction
from graphiti_core.driver.tigergraph.operations.common import (
    delete_edges,
    edge_row,
    edges_by_uuids,
    edges_in_groups,
    plain_edge_attrs,
    plain_edge_kwargs,
    save_edges,
)
from graphiti_core.edges import HasEpisodeEdge
from graphiti_core.errors import EdgeNotFoundError

logger = logging.getLogger(__name__)

EDGE_TYPE = 'HAS_EPISODE'


async def has_episode_row(executor: QueryExecutor, edge: HasEpisodeEdge) -> tuple:
    return edge_row(
        'Saga',
        edge.source_node_uuid,
        EDGE_TYPE,
        'Episodic',
        edge.target_node_uuid,
        plain_edge_attrs(edge),
    )


class TigerGraphHasEpisodeEdgeOperations(HasEpisodeEdgeOperations):
    async def save(
        self, executor: QueryExecutor, edge: HasEpisodeEdge, tx: Transaction | None = None
    ) -> None:
        await save_edges(executor, [await has_episode_row(executor, edge)])
        logger.debug(f'Saved HAS_EPISODE edge to Graph: {edge.uuid}')

    async def save_bulk(
        self,
        executor: QueryExecutor,
        edges: list[HasEpisodeEdge],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        await save_edges(executor, [await has_episode_row(executor, e) for e in edges])

    async def delete(
        self, executor: QueryExecutor, edge: HasEpisodeEdge, tx: Transaction | None = None
    ) -> None:
        await delete_edges(executor, EDGE_TYPE, [edge.uuid])

    async def delete_by_uuids(
        self, executor: QueryExecutor, uuids: list[str], tx: Transaction | None = None
    ) -> None:
        await delete_edges(executor, EDGE_TYPE, uuids)

    async def get_by_uuid(self, executor: QueryExecutor, uuid: str) -> HasEpisodeEdge:
        edges = await self.get_by_uuids(executor, [uuid])
        if not edges:
            raise EdgeNotFoundError(uuid)
        return edges[0]

    async def get_by_uuids(self, executor: QueryExecutor, uuids: list[str]) -> list[HasEpisodeEdge]:
        rows = await edges_by_uuids(executor, EDGE_TYPE, uuids)
        return [HasEpisodeEdge(**plain_edge_kwargs(r)) for r in rows]

    async def get_by_group_ids(
        self,
        executor: QueryExecutor,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[HasEpisodeEdge]:
        rows = await edges_in_groups(executor, EDGE_TYPE, group_ids, limit, uuid_cursor)
        return [HasEpisodeEdge(**plain_edge_kwargs(r)) for r in rows]
