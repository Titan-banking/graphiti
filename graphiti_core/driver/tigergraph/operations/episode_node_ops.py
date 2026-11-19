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
from datetime import datetime
from typing import Any

from graphiti_core.driver.operations.episode_node_ops import EpisodeNodeOperations
from graphiti_core.driver.query_executor import QueryExecutor, Transaction
from graphiti_core.driver.tigergraph.client import flatten
from graphiti_core.driver.tigergraph.convert import (
    dt_to_str,
    dumps,
    loads,
    str_to_dt_required,
)
from graphiti_core.driver.tigergraph.operations.common import (
    NO_LIMIT,
    client_of,
    delete_vertices,
    delete_vertices_in_group,
    get_vertices,
    save_vertices,
    vertices_in_groups,
)
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EpisodeType, EpisodicNode

logger = logging.getLogger(__name__)


def episode_attrs(node: EpisodicNode) -> dict[str, Any]:
    return {
        'name': node.name,
        'group_id': node.group_id,
        'source': node.source.value,
        'source_description': node.source_description,
        'content': node.content,
        'valid_at': dt_to_str(node.valid_at),
        'created_at': dt_to_str(node.created_at),
        'entity_edges': list(node.entity_edges or []),
        'labels': list(node.labels or []),
        'episode_metadata': dumps(node.episode_metadata),
    }


def episode_node_from_attrs(attrs: dict[str, Any]) -> EpisodicNode:
    return EpisodicNode(
        uuid=attrs['uuid'],
        name=attrs.get('name', ''),
        group_id=attrs.get('group_id', ''),
        labels=list(attrs.get('labels') or []),
        created_at=str_to_dt_required(attrs.get('created_at')),
        source=EpisodeType.from_str(attrs.get('source', 'text')),
        source_description=attrs.get('source_description', ''),
        content=attrs.get('content', ''),
        valid_at=str_to_dt_required(attrs.get('valid_at')),
        entity_edges=list(attrs.get('entity_edges') or []),
        episode_metadata=loads(attrs.get('episode_metadata')) or None,
    )


class TigerGraphEpisodeNodeOperations(EpisodeNodeOperations):
    async def save(
        self, executor: QueryExecutor, node: EpisodicNode, tx: Transaction | None = None
    ) -> None:
        await save_vertices(executor, 'Episodic', {node.uuid: episode_attrs(node)})
        logger.debug(f'Saved Episode to Graph: {node.uuid}')

    async def save_bulk(
        self,
        executor: QueryExecutor,
        nodes: list[EpisodicNode],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        await save_vertices(executor, 'Episodic', {n.uuid: episode_attrs(n) for n in nodes})

    async def delete(
        self, executor: QueryExecutor, node: EpisodicNode, tx: Transaction | None = None
    ) -> None:
        await delete_vertices(executor, 'Episodic', [node.uuid])

    async def delete_by_group_id(
        self,
        executor: QueryExecutor,
        group_id: str,
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        await delete_vertices_in_group(executor, 'Episodic', group_id)

    async def delete_by_uuids(
        self,
        executor: QueryExecutor,
        uuids: list[str],
        tx: Transaction | None = None,
        batch_size: int = 100,
    ) -> None:
        await delete_vertices(executor, 'Episodic', uuids)

    async def get_by_uuid(self, executor: QueryExecutor, uuid: str) -> EpisodicNode:
        nodes = await self.get_by_uuids(executor, [uuid])
        if not nodes:
            raise NodeNotFoundError(uuid)
        return nodes[0]

    async def get_by_uuids(self, executor: QueryExecutor, uuids: list[str]) -> list[EpisodicNode]:
        return [episode_node_from_attrs(a) for a in await get_vertices(executor, 'Episodic', uuids)]

    async def get_by_group_ids(
        self,
        executor: QueryExecutor,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ) -> list[EpisodicNode]:
        rows = await vertices_in_groups(executor, 'Episodic', group_ids, limit, uuid_cursor)
        return [episode_node_from_attrs(a) for a in rows]

    async def get_by_entity_node_uuid(
        self, executor: QueryExecutor, entity_node_uuid: str
    ) -> list[EpisodicNode]:
        results = await client_of(executor).interpret(
            'STRING id',
            'S = SELECT e FROM Episodic:e -(MENTIONS:m)-> Entity:n WHERE n.uuid == id; PRINT S;',
            {'id': entity_node_uuid},
        )
        return [
            episode_node_from_attrs(flatten(v))
            for v in ((results[0].get('S') if results else None) or [])
        ]

    async def retrieve_episodes(
        self,
        executor: QueryExecutor,
        reference_time: datetime,
        last_n: int = 3,
        group_ids: list[str] | None = None,
        source: str | None = None,
        saga: str | None = None,
    ) -> list[EpisodicNode]:
        params: dict[str, Any] = {
            'ref': dt_to_str(reference_time),
            'src': source or '',
            'lim': last_n if last_n is not None else NO_LIMIT,
        }
        if saga is not None and group_ids:
            params.update({'saga': saga, 'gid': group_ids[0]})
            results = await client_of(executor).interpret(
                'STRING saga, STRING gid, STRING ref, STRING src, INT lim',
                'S = SELECT e FROM Saga:s -(HAS_EPISODE:h)-> Episodic:e WHERE s.name == saga AND s.group_id == gid'
                ' AND e.valid_at <= ref AND (src == "" OR e.source == src) ORDER BY e.valid_at DESC LIMIT lim;'
                ' PRINT S;',
                params,
            )
        else:
            params.update({'gids': group_ids or [], 'use_groups': bool(group_ids)})
            results = await client_of(executor).interpret(
                'SET<STRING> gids, BOOL use_groups, STRING ref, STRING src, INT lim',
                'S = SELECT e FROM Episodic:e WHERE (NOT use_groups OR e.group_id IN gids) AND e.valid_at <= ref'
                ' AND (src == "" OR e.source == src) ORDER BY e.valid_at DESC LIMIT lim; PRINT S;',
                params,
            )
        return [
            episode_node_from_attrs(flatten(v))
            for v in ((results[0].get('S') if results else None) or [])
        ]
