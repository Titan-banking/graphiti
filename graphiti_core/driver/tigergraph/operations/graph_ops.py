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
from collections import Counter
from typing import Any

from graphiti_core.driver.operations.graph_ops import GraphMaintenanceOperations
from graphiti_core.driver.operations.graph_utils import Neighbor, label_propagation
from graphiti_core.driver.query_executor import QueryExecutor
from graphiti_core.driver.tigergraph import schema
from graphiti_core.driver.tigergraph.client import flatten
from graphiti_core.driver.tigergraph.operations.common import (
    client_of,
    get_vertices,
    vertices_in_groups,
)
from graphiti_core.driver.tigergraph.operations.community_node_ops import community_node_from_attrs
from graphiti_core.driver.tigergraph.operations.entity_node_ops import entity_node_from_attrs
from graphiti_core.nodes import CommunityNode, EntityNode, EpisodicNode

logger = logging.getLogger(__name__)

VERTEX_TYPES = ('Entity', 'Episodic', 'Community', 'Saga')


def _rows(results: list[dict[str, Any]], key: str) -> list[Any]:
    for block in results:
        if key in block:
            return block[key] or []
    return []


class TigerGraphGraphMaintenanceOperations(GraphMaintenanceOperations):
    async def clear_data(self, executor: QueryExecutor, group_ids: list[str] | None = None) -> None:
        client = client_of(executor)
        for vertex_type in (
            VERTEX_TYPES if group_ids is None else ('Entity', 'Episodic', 'Community')
        ):
            where = '' if group_ids is None else ' WHERE s.group_id IN gids'
            signature = '' if group_ids is None else 'SET<STRING> gids'
            await client.interpret(
                signature,
                f'S = {{{vertex_type}.*}}; D = SELECT s FROM S:s{where}; DELETE s FROM D:s;',
                {'gids': group_ids} if group_ids is not None else None,
            )

    async def build_indices_and_constraints(
        self, executor: QueryExecutor, delete_existing: bool = False
    ) -> None:
        if delete_existing:
            await self.delete_all_indexes(executor)
        await schema.ensure_schema(client_of(executor), executor.embedding_dim)  # type: ignore[attr-defined]

    async def delete_all_indexes(self, executor: QueryExecutor) -> None:
        # The vector attributes and installed queries are part of the schema, and dropping the schema deletes the data:
        # ``schema.drop_schema`` does it, and nothing calls it implicitly.
        logger.warning(
            'delete_all_indexes does nothing on TigerGraph: use schema.drop_schema to drop the graph and its data.'
        )

    async def get_community_clusters(
        self, executor: QueryExecutor, group_ids: list[str] | None = None
    ) -> list[Any]:
        client = client_of(executor)
        if group_ids is None:
            results = await client.interpret(
                '',
                'SetAccum<STRING> @@g; S = SELECT v FROM Entity:v ACCUM @@g += v.group_id; PRINT @@g;',
            )
            group_ids = list(_rows(results, '@@g'))

        community_clusters: list[list[EntityNode]] = []
        for group_id in group_ids:
            rows = await vertices_in_groups(executor, 'Entity', [group_id], None, None)
            nodes = {a['uuid']: entity_node_from_attrs(a) for a in rows}

            # Edges between entities of the group, counted per neighbour in both directions
            results = await client.interpret(
                'STRING gid',
                'MapAccum<STRING, MapAccum<STRING, SumAccum<INT>>> @@adj;'
                ' S = SELECT t FROM Entity:s -((RELATES_TO>|<RELATES_TO))- Entity:t WHERE s.group_id == gid AND t.group_id == gid'
                ' ACCUM @@adj += (s.uuid -> (t.uuid -> 1)); PRINT @@adj;',
                {'gid': group_id},
            )
            adjacency = (_rows_map(results, '@@adj')) or {}
            projection = {
                uuid: [
                    Neighbor(node_uuid=n, edge_count=int(c))
                    for n, c in adjacency.get(uuid, {}).items()
                ]
                for uuid in nodes
            }
            for cluster in label_propagation(projection):
                if cluster:
                    community_clusters.append([nodes[u] for u in cluster if u in nodes])

        return community_clusters

    async def remove_communities(
        self, executor: QueryExecutor, group_ids: list[str] | None = None
    ) -> None:
        condition = '' if group_ids is None else ' WHERE s.group_id IN gids'
        await client_of(executor).interpret(
            'SET<STRING> gids',
            f'S = {{Community.*}}; D = SELECT s FROM S:s{condition}; DELETE s FROM D:s;',
            {'gids': group_ids or []},
        )

    async def determine_entity_community(self, executor: QueryExecutor, entity: EntityNode) -> None:
        await self.community_of(executor, entity)

    async def community_of(
        self, executor: QueryExecutor, entity: EntityNode
    ) -> tuple[CommunityNode | None, bool]:
        """The community the entity belongs to, else the most common community of its neighbours: ``(community, is_new)``.

        ``is_new`` is true when the entity has no community of its own yet and would have to be added to the returned one.
        """
        client = client_of(executor)
        own = await client.interpret(
            'STRING id',
            'S = SELECT c FROM Community:c -(HAS_MEMBER)-> Entity:n WHERE n.uuid == id; PRINT S;',
            {'id': entity.uuid},
        )
        found = _rows(own, 'S')
        if found:
            return community_node_from_attrs(flatten(found[0])), False

        around = await client.interpret(
            'STRING id',
            'ListAccum<STRING> @@c; SetAccum<STRING> @@n;'
            ' N = SELECT m FROM Entity:n -((RELATES_TO>|<RELATES_TO))- Entity:m WHERE n.uuid == id ACCUM @@n += m.uuid;'
            ' C = SELECT c FROM Community:c -(HAS_MEMBER>)- Entity:m WHERE @@n.contains(m.uuid)'
            ' ACCUM @@c += c.uuid; PRINT @@c;',
            {'id': entity.uuid},
        )
        counts = Counter(_rows(around, '@@c'))
        if not counts:
            return None, False
        best = counts.most_common(1)[0][0]
        community = await get_vertices(executor, 'Community', [best])
        return (community_node_from_attrs(community[0]), True) if community else (None, False)

    async def get_mentioned_nodes(
        self, executor: QueryExecutor, episodes: list[EpisodicNode]
    ) -> list[EntityNode]:
        if not episodes:
            return []
        results = await client_of(executor).interpret(
            'SET<STRING> ids',
            'S = SELECT n FROM Episodic:e -(MENTIONS)-> Entity:n WHERE e.uuid IN ids; PRINT S;',
            {'ids': [e.uuid for e in episodes]},
        )
        return [entity_node_from_attrs(flatten(v)) for v in _rows(results, 'S')]

    async def get_communities_by_nodes(
        self, executor: QueryExecutor, nodes: list[EntityNode]
    ) -> list[CommunityNode]:
        if not nodes:
            return []
        results = await client_of(executor).interpret(
            'SET<STRING> ids',
            'S = SELECT c FROM Community:c -(HAS_MEMBER)-> Entity:m WHERE m.uuid IN ids; PRINT S;',
            {'ids': [n.uuid for n in nodes]},
        )
        return [community_node_from_attrs(flatten(v)) for v in _rows(results, 'S')]


def _rows_map(results: list[dict[str, Any]], key: str) -> dict[str, Any]:
    for block in results:
        if key in block:
            return block[key] or {}
    return {}
