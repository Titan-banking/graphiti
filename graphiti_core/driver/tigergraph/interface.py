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

Graphiti core saves, loads and deletes through Cypher with a branch per provider, except where
``driver.graph_operations_interface`` answers first. TigerGraph does not run Cypher, so this interface routes each of those
calls to the driver's operation classes (``graphiti_core/driver/tigergraph/operations``).
"""

from datetime import datetime
from typing import Any

from graphiti_core.driver.graph_operations.graph_operations import GraphOperationsInterface
from graphiti_core.driver.tigergraph.convert import dt_to_str, str_to_dt
from graphiti_core.driver.tigergraph.operations.common import (
    client_of,
    delete_edges_anywhere,
    delete_vertices,
    delete_vertices_in_group,
)
from graphiti_core.edges import EntityEdge, EpisodicEdge
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode

# Core hands a bulk save plain dicts in which the free-form ``attributes`` of a node or an edge sit beside its fields
_ENTITY_FIELDS = {'uuid', 'name', 'group_id', 'summary', 'created_at', 'name_embedding', 'labels'}
_EDGE_FIELDS = {
    'uuid',
    'source_node_uuid',
    'target_node_uuid',
    'name',
    'fact',
    'group_id',
    'episodes',
    'created_at',
    'expired_at',
    'valid_at',
    'invalid_at',
    'reference_time',
    'fact_embedding',
}


def _entity(node: Any) -> EntityNode:
    if isinstance(node, EntityNode):
        return node
    attributes = {k: v for k, v in node.items() if k not in _ENTITY_FIELDS}
    return EntityNode(
        **{k: v for k, v in node.items() if k in _ENTITY_FIELDS}, attributes=attributes
    )


def _entity_edge(edge: Any) -> EntityEdge:
    if isinstance(edge, EntityEdge):
        return edge
    attributes = {k: v for k, v in edge.items() if k not in _EDGE_FIELDS}
    return EntityEdge(**{k: v for k, v in edge.items() if k in _EDGE_FIELDS}, attributes=attributes)


def _episode(node: Any) -> EpisodicNode:
    if isinstance(node, EpisodicNode):
        return node
    return EpisodicNode(**{**node, 'source': EpisodeType.from_str(node['source'])})


# The kinds ``Node.delete*`` covers on every backend (a saga is deleted through its own hooks)
VERTEX_TYPES = ('Entity', 'Episodic', 'Community')


async def _delete_uuids_everywhere(driver: Any, uuids: list[str]) -> None:
    """Delete the vertices with these uuids whatever their type (the generic ``Node`` deletes name no type)."""
    for vertex_type in VERTEX_TYPES:
        await delete_vertices(driver, vertex_type, uuids)


class TigerGraphOperations(GraphOperationsInterface):
    async def node_save(self, node: Any, driver: Any):
        await driver.entity_node_ops.save(driver, node)

    async def node_delete(self, node: Any, driver: Any):
        # ``Node.delete`` is shared by every kind of node
        await _delete_uuids_everywhere(driver, [node.uuid])

    async def node_save_bulk(
        self, _cls: Any, driver: Any, transaction: Any, nodes: list[Any], batch_size: int = 100
    ):
        await driver.entity_node_ops.save_bulk(
            driver, [_entity(n) for n in nodes], batch_size=batch_size
        )

    async def node_delete_by_group_id(
        self, _cls: Any, driver: Any, group_id: str, batch_size: int = 100
    ):
        for vertex_type in VERTEX_TYPES:
            await delete_vertices_in_group(driver, vertex_type, group_id)

    async def node_delete_by_uuids(
        self,
        _cls: Any,
        driver: Any,
        uuids: list[str],
        group_id: str | None = None,
        batch_size: int = 100,
    ):
        await _delete_uuids_everywhere(driver, uuids)

    async def node_get_by_uuid(self, _cls: Any, driver: Any, uuid: str):
        return await driver.entity_node_ops.get_by_uuid(driver, uuid)

    async def node_get_by_uuids(
        self, _cls: Any, driver: Any, uuids: list[str], group_id: str | None = None
    ):
        return await driver.entity_node_ops.get_by_uuids(driver, uuids)

    async def node_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ):
        return await driver.entity_node_ops.get_by_group_ids(
            driver, group_ids, limit=limit, uuid_cursor=uuid_cursor
        )

    async def episodic_node_save(self, node: Any, driver: Any):
        await driver.episode_node_ops.save(driver, node)

    async def episodic_node_delete(self, node: Any, driver: Any):
        await driver.episode_node_ops.delete(driver, node)

    async def episodic_node_save_bulk(
        self, _cls: Any, driver: Any, transaction: Any, nodes: list[Any], batch_size: int = 100
    ):
        episodes = [_episode(n) for n in nodes]
        await driver.episode_node_ops.save_bulk(driver, episodes, batch_size=batch_size)

    async def episodic_node_delete_by_group_id(
        self, _cls: Any, driver: Any, group_id: str, batch_size: int = 100
    ):
        await driver.episode_node_ops.delete_by_group_id(driver, group_id, batch_size=batch_size)

    async def episodic_node_delete_by_uuids(
        self,
        _cls: Any,
        driver: Any,
        uuids: list[str],
        group_id: str | None = None,
        batch_size: int = 100,
    ):
        await driver.episode_node_ops.delete_by_uuids(driver, uuids, batch_size=batch_size)

    async def episodic_node_get_by_uuid(self, _cls: Any, driver: Any, uuid: str):
        return await driver.episode_node_ops.get_by_uuid(driver, uuid)

    async def episodic_node_get_by_uuids(self, _cls: Any, driver: Any, uuids: list[str]):
        return await driver.episode_node_ops.get_by_uuids(driver, uuids)

    async def episodic_node_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ):
        return await driver.episode_node_ops.get_by_group_ids(
            driver, group_ids, limit=limit, uuid_cursor=uuid_cursor
        )

    async def community_node_save(self, node: Any, driver: Any):
        await driver.community_node_ops.save(driver, node)

    async def community_node_delete(self, node: Any, driver: Any):
        await driver.community_node_ops.delete(driver, node)

    async def community_node_save_bulk(
        self, _cls: Any, driver: Any, transaction: Any, nodes: list[Any], batch_size: int = 100
    ):
        await driver.community_node_ops.save_bulk(driver, nodes, batch_size=batch_size)

    async def community_node_delete_by_group_id(
        self, _cls: Any, driver: Any, group_id: str, batch_size: int = 100
    ):
        await driver.community_node_ops.delete_by_group_id(driver, group_id, batch_size=batch_size)

    async def community_node_delete_by_uuids(
        self,
        _cls: Any,
        driver: Any,
        uuids: list[str],
        group_id: str | None = None,
        batch_size: int = 100,
    ):
        await driver.community_node_ops.delete_by_uuids(driver, uuids, batch_size=batch_size)

    async def community_node_get_by_uuid(self, _cls: Any, driver: Any, uuid: str):
        return await driver.community_node_ops.get_by_uuid(driver, uuid)

    async def community_node_get_by_uuids(self, _cls: Any, driver: Any, uuids: list[str]):
        return await driver.community_node_ops.get_by_uuids(driver, uuids)

    async def community_node_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ):
        return await driver.community_node_ops.get_by_group_ids(
            driver, group_ids, limit=limit, uuid_cursor=uuid_cursor
        )

    async def saga_node_save(self, node: Any, driver: Any):
        await driver.saga_node_ops.save(driver, node)

    async def saga_node_delete(self, node: Any, driver: Any):
        await driver.saga_node_ops.delete(driver, node)

    async def saga_node_save_bulk(
        self, _cls: Any, driver: Any, transaction: Any, nodes: list[Any], batch_size: int = 100
    ):
        await driver.saga_node_ops.save_bulk(driver, nodes, batch_size=batch_size)

    async def saga_node_delete_by_group_id(
        self, _cls: Any, driver: Any, group_id: str, batch_size: int = 100
    ):
        await driver.saga_node_ops.delete_by_group_id(driver, group_id, batch_size=batch_size)

    async def saga_node_delete_by_uuids(
        self,
        _cls: Any,
        driver: Any,
        uuids: list[str],
        group_id: str | None = None,
        batch_size: int = 100,
    ):
        await driver.saga_node_ops.delete_by_uuids(driver, uuids, batch_size=batch_size)

    async def saga_node_get_by_uuid(self, _cls: Any, driver: Any, uuid: str):
        return await driver.saga_node_ops.get_by_uuid(driver, uuid)

    async def saga_node_get_by_uuids(self, _cls: Any, driver: Any, uuids: list[str]):
        return await driver.saga_node_ops.get_by_uuids(driver, uuids)

    async def saga_node_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ):
        return await driver.saga_node_ops.get_by_group_ids(
            driver, group_ids, limit=limit, uuid_cursor=uuid_cursor
        )

    async def edge_save(self, edge: Any, driver: Any):
        await driver.entity_edge_ops.save(driver, edge)

    async def edge_delete(self, edge: Any, driver: Any):
        # ``Edge.delete`` is shared by every kind of edge
        await delete_edges_anywhere(driver, [edge.uuid])

    async def edge_save_bulk(
        self, _cls: Any, driver: Any, transaction: Any, edges: list[Any], batch_size: int = 100
    ):
        await driver.entity_edge_ops.save_bulk(
            driver, [_entity_edge(e) for e in edges], batch_size=batch_size
        )

    async def edge_delete_by_uuids(
        self, _cls: Any, driver: Any, uuids: list[str], group_id: str | None = None
    ):
        await delete_edges_anywhere(driver, uuids)

    async def edge_get_by_uuid(self, _cls: Any, driver: Any, uuid: str):
        return await driver.entity_edge_ops.get_by_uuid(driver, uuid)

    async def edge_get_by_uuids(self, _cls: Any, driver: Any, uuids: list[str]):
        return await driver.entity_edge_ops.get_by_uuids(driver, uuids)

    async def edge_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ):
        return await driver.entity_edge_ops.get_by_group_ids(
            driver, group_ids, limit=limit, uuid_cursor=uuid_cursor
        )

    async def episodic_edge_save(self, edge: Any, driver: Any):
        await driver.episodic_edge_ops.save(driver, edge)

    async def episodic_edge_delete(self, edge: Any, driver: Any):
        await driver.episodic_edge_ops.delete(driver, edge)

    async def episodic_edge_save_bulk(
        self,
        _cls: Any,
        driver: Any,
        transaction: Any,
        episodic_edges: list[Any],
        batch_size: int = 100,
    ):
        edges = [e if isinstance(e, EpisodicEdge) else EpisodicEdge(**e) for e in episodic_edges]
        await driver.episodic_edge_ops.save_bulk(driver, edges, batch_size=batch_size)

    async def episodic_edge_delete_by_uuids(
        self, _cls: Any, driver: Any, uuids: list[str], group_id: str | None = None
    ):
        await driver.episodic_edge_ops.delete_by_uuids(driver, uuids)

    async def episodic_edge_get_by_uuid(self, _cls: Any, driver: Any, uuid: str):
        return await driver.episodic_edge_ops.get_by_uuid(driver, uuid)

    async def episodic_edge_get_by_uuids(self, _cls: Any, driver: Any, uuids: list[str]):
        return await driver.episodic_edge_ops.get_by_uuids(driver, uuids)

    async def episodic_edge_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ):
        return await driver.episodic_edge_ops.get_by_group_ids(
            driver, group_ids, limit=limit, uuid_cursor=uuid_cursor
        )

    async def community_edge_save(self, edge: Any, driver: Any):
        await driver.community_edge_ops.save(driver, edge)

    async def community_edge_delete(self, edge: Any, driver: Any):
        await driver.community_edge_ops.delete(driver, edge)

    async def community_edge_delete_by_uuids(
        self, _cls: Any, driver: Any, uuids: list[str], group_id: str | None = None
    ):
        await driver.community_edge_ops.delete_by_uuids(driver, uuids)

    async def community_edge_get_by_uuid(self, _cls: Any, driver: Any, uuid: str):
        return await driver.community_edge_ops.get_by_uuid(driver, uuid)

    async def community_edge_get_by_uuids(self, _cls: Any, driver: Any, uuids: list[str]):
        return await driver.community_edge_ops.get_by_uuids(driver, uuids)

    async def community_edge_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ):
        return await driver.community_edge_ops.get_by_group_ids(
            driver, group_ids, limit=limit, uuid_cursor=uuid_cursor
        )

    async def has_episode_edge_save(self, edge: Any, driver: Any):
        await driver.has_episode_edge_ops.save(driver, edge)

    async def has_episode_edge_delete(self, edge: Any, driver: Any):
        await driver.has_episode_edge_ops.delete(driver, edge)

    async def has_episode_edge_save_bulk(
        self, _cls: Any, driver: Any, transaction: Any, edges: list[Any], batch_size: int = 100
    ):
        await driver.has_episode_edge_ops.save_bulk(driver, edges, batch_size=batch_size)

    async def has_episode_edge_delete_by_uuids(
        self, _cls: Any, driver: Any, uuids: list[str], group_id: str | None = None
    ):
        await driver.has_episode_edge_ops.delete_by_uuids(driver, uuids)

    async def has_episode_edge_get_by_uuid(self, _cls: Any, driver: Any, uuid: str):
        return await driver.has_episode_edge_ops.get_by_uuid(driver, uuid)

    async def has_episode_edge_get_by_uuids(self, _cls: Any, driver: Any, uuids: list[str]):
        return await driver.has_episode_edge_ops.get_by_uuids(driver, uuids)

    async def has_episode_edge_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ):
        return await driver.has_episode_edge_ops.get_by_group_ids(
            driver, group_ids, limit=limit, uuid_cursor=uuid_cursor
        )

    async def next_episode_edge_save(self, edge: Any, driver: Any):
        await driver.next_episode_edge_ops.save(driver, edge)

    async def next_episode_edge_delete(self, edge: Any, driver: Any):
        await driver.next_episode_edge_ops.delete(driver, edge)

    async def next_episode_edge_save_bulk(
        self, _cls: Any, driver: Any, transaction: Any, edges: list[Any], batch_size: int = 100
    ):
        await driver.next_episode_edge_ops.save_bulk(driver, edges, batch_size=batch_size)

    async def next_episode_edge_delete_by_uuids(
        self, _cls: Any, driver: Any, uuids: list[str], group_id: str | None = None
    ):
        await driver.next_episode_edge_ops.delete_by_uuids(driver, uuids)

    async def next_episode_edge_get_by_uuid(self, _cls: Any, driver: Any, uuid: str):
        return await driver.next_episode_edge_ops.get_by_uuid(driver, uuid)

    async def next_episode_edge_get_by_uuids(self, _cls: Any, driver: Any, uuids: list[str]):
        return await driver.next_episode_edge_ops.get_by_uuids(driver, uuids)

    async def next_episode_edge_get_by_group_ids(
        self,
        _cls: Any,
        driver: Any,
        group_ids: list[str],
        limit: int | None = None,
        uuid_cursor: str | None = None,
    ):
        return await driver.next_episode_edge_ops.get_by_group_ids(
            driver, group_ids, limit=limit, uuid_cursor=uuid_cursor
        )

    async def node_get_by_names(
        self, _cls: Any, driver: Any, names: list[str], group_ids: list[str]
    ):
        results = await client_of(driver).interpret(
            'SET<STRING> names, SET<STRING> gids',
            'S = SELECT v FROM Entity:v WHERE v.name IN names AND v.group_id IN gids; PRINT S;',
            {'names': names, 'gids': group_ids},
        )
        from graphiti_core.driver.tigergraph.client import flatten
        from graphiti_core.driver.tigergraph.operations.entity_node_ops import (
            entity_node_from_attrs,
        )

        rows = next((b['S'] for b in results if 'S' in b), [])
        return [entity_node_from_attrs(flatten(v)) for v in rows]

    async def node_load_embeddings(self, node: Any, driver: Any):
        await driver.entity_node_ops.load_embeddings(driver, node)

    async def node_load_embeddings_bulk(self, driver: Any, nodes: list[Any], batch_size: int = 100):
        await driver.entity_node_ops.load_embeddings_bulk(driver, nodes, batch_size=batch_size)
        return {n.uuid: n.name_embedding for n in nodes if n.name_embedding}

    async def edge_load_embeddings(self, edge: Any, driver: Any):
        await driver.entity_edge_ops.load_embeddings(driver, edge)

    async def edge_load_embeddings_bulk(self, driver: Any, edges: list[Any], batch_size: int = 100):
        await driver.entity_edge_ops.load_embeddings_bulk(driver, edges, batch_size=batch_size)
        return {e.uuid: e.fact_embedding for e in edges if e.fact_embedding}

    async def community_node_load_name_embedding(self, node: Any, driver: Any):
        await driver.community_node_ops.load_name_embedding(driver, node)

    async def episodic_node_get_by_entity_node_uuid(
        self, _cls: Any, driver: Any, entity_node_uuid: str
    ):
        return await driver.episode_node_ops.get_by_entity_node_uuid(driver, entity_node_uuid)

    async def edge_get_between_nodes(
        self, _cls: Any, driver: Any, source_node_uuid: str, target_node_uuid: str
    ):
        return await driver.entity_edge_ops.get_between_nodes(
            driver, source_node_uuid, target_node_uuid
        )

    async def edge_get_by_node_uuid(self, _cls: Any, driver: Any, node_uuid: str):
        return await driver.entity_edge_ops.get_by_node_uuid(driver, node_uuid)

    async def retrieve_episodes(
        self,
        driver: Any,
        reference_time: datetime,
        last_n: int = 3,
        group_ids: list[str] | None = None,
        source: Any | None = None,
        saga: str | None = None,
    ):
        source_value = getattr(source, 'value', source)
        episodes = await driver.episode_node_ops.retrieve_episodes(
            driver, reference_time, last_n, group_ids, source_value, saga
        )
        # Core returns them oldest first
        return list(reversed(episodes))

    async def saga_get_previous_episode_uuid(
        self, driver: Any, saga_uuid: str, current_episode_uuid: str
    ):
        results = await client_of(driver).interpret(
            'STRING saga, STRING current',
            'S = SELECT e FROM Saga:s -(HAS_EPISODE)-> Episodic:e WHERE s.uuid == saga AND e.uuid != current'
            ' ORDER BY e.valid_at DESC, e.created_at DESC LIMIT 1; PRINT S;',
            {'saga': saga_uuid, 'current': current_episode_uuid},
        )
        rows = next((b['S'] for b in results if 'S' in b), [])
        return rows[0]['v_id'] if rows else None

    async def saga_get_episode_contents(
        self, driver: Any, saga_uuid: str, since: datetime | None = None, limit: int = 200
    ):
        results = await client_of(driver).interpret(
            'STRING saga, STRING since, INT lim',
            'S = SELECT e FROM Saga:s -(HAS_EPISODE)-> Episodic:e WHERE s.uuid == saga AND e.created_at != ""'
            ' AND (since == "" OR e.created_at > since) ORDER BY e.created_at ASC, e.valid_at ASC LIMIT lim; PRINT S;',
            {'saga': saga_uuid, 'since': dt_to_str(since) if since else '', 'lim': limit},
        )
        rows = next((b['S'] for b in results if 'S' in b), [])
        episodes = []
        for v in rows:
            attrs = v.get('attributes') or {}
            created_at = str_to_dt(attrs.get('created_at'))
            if attrs.get('content') and created_at is not None:
                episodes.append((attrs['content'], str_to_dt(attrs.get('valid_at')), created_at))
        return episodes

    async def get_mentioned_nodes(self, driver: Any, episodes: list[Any]):
        return await driver.graph_ops.get_mentioned_nodes(driver, episodes)

    async def get_communities_by_nodes(self, driver: Any, nodes: list[Any]):
        return await driver.graph_ops.get_communities_by_nodes(driver, nodes)

    async def clear_data(self, driver: Any, group_ids: list[str] | None = None):
        await driver.graph_ops.clear_data(driver, group_ids)

    async def get_community_clusters(self, driver: Any, group_ids: list[str] | None):
        return await driver.graph_ops.get_community_clusters(driver, group_ids)

    async def remove_communities(self, driver: Any, group_ids: list[str] | None = None):
        await driver.graph_ops.remove_communities(driver, group_ids)

    async def determine_entity_community(self, driver: Any, entity: Any):
        return await driver.graph_ops.community_of(driver, entity)
