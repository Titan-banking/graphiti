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

Search over TigerGraph with its installed queries (see ``schema.INSTALLED_QUERIES``):

* node and community similarity: TigerGraph's vector index (``vectorSearch``);
* edge similarity: an exact cosine scan of the facts (a TigerGraph edge has no vector attribute);
* fulltext: a token scan (TigerGraph has no inverted index), scored by the number of query tokens a text contains,
  which is not Lucene BM25 and ranks differently;
* breadth-first search: a bounded walk over ``RELATES_TO`` and ``MENTIONS``.

A ``SearchFilters`` that can discard candidates is applied in Python to an oversampled candidate list.
"""

import asyncio
import re
from typing import Any

from graphiti_core.driver.operations.search_ops import SearchOperations
from graphiti_core.driver.query_executor import QueryExecutor
from graphiti_core.driver.tigergraph import filters as tg_filters
from graphiti_core.driver.tigergraph.client import flatten, flatten_edge
from graphiti_core.driver.tigergraph.operations.common import (
    client_of,
    get_vertices,
    wait_for_vector,
)
from graphiti_core.driver.tigergraph.operations.community_node_ops import community_node_from_attrs
from graphiti_core.driver.tigergraph.operations.entity_edge_ops import entity_edge_from_attrs
from graphiti_core.driver.tigergraph.operations.entity_node_ops import entity_node_from_attrs
from graphiti_core.driver.tigergraph.operations.episode_node_ops import episode_node_from_attrs
from graphiti_core.edges import EntityEdge
from graphiti_core.helpers import validate_group_ids
from graphiti_core.nodes import CommunityNode, EntityNode, EpisodicNode
from graphiti_core.search.search_filters import SearchFilters

# A query that tokenizes to more words than this is cut (a token is one ``instr`` per text field per vertex)
MAX_TOKENS = 32


def tokens(query: str) -> list[str]:
    """The lower-case words of a query, without duplicates."""
    return list(dict.fromkeys(re.findall(r'\w+', query.lower())))[:MAX_TOKENS]


def _group_params(group_ids: list[str] | None) -> dict[str, Any]:
    # An installed query takes no optional parameter: a BOOL says whether the (never empty) set applies
    return {'use_groups': bool(group_ids), 'groups': list(group_ids) if group_ids else ['']}


def _first(results: list[dict[str, Any]], key: str) -> list[Any]:
    for block in results:
        if key in block:
            return block[key] or []
    return []


async def _hits(
    executor: QueryExecutor, query: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    """The ``@@top`` heap of an installed query, best first."""
    return _first(await client_of(executor).run_query(query, params), '@@top')


class TigerGraphSearchOperations(SearchOperations):
    # --- fulltext ---

    async def _fulltext(
        self,
        executor: QueryExecutor,
        query: str,
        text: str,
        group_ids: list[str] | None,
        wanted: int,
    ) -> list[dict[str, Any]]:
        validate_group_ids(group_ids)
        toks = tokens(text)
        if not toks:
            return []
        params = {'toks': toks, 'lim': wanted, **_group_params(group_ids)}
        return await _hits(executor, query, params)

    async def node_fulltext_search(
        self,
        executor: QueryExecutor,
        query: str,
        search_filter: SearchFilters,
        group_ids: list[str] | None = None,
        limit: int = 10,
    ) -> list[EntityNode]:
        wanted = tg_filters.candidates_wanted(limit, tg_filters.has_node_filters(search_filter))
        hits = await self._fulltext(executor, 'entity_fulltext', query, group_ids, wanted)
        nodes = await self._entities(executor, [h['uuid'] for h in hits])
        return [n for n in nodes if tg_filters.node_matches(n, search_filter)][:limit]

    async def episode_fulltext_search(
        self,
        executor: QueryExecutor,
        query: str,
        search_filter: SearchFilters,
        group_ids: list[str] | None = None,
        limit: int = 10,
    ) -> list[EpisodicNode]:
        hits = await self._fulltext(executor, 'episode_fulltext', query, group_ids, limit)
        found = await get_vertices(executor, 'Episodic', [h['uuid'] for h in hits])
        return [episode_node_from_attrs(a) for a in found]

    async def community_fulltext_search(
        self,
        executor: QueryExecutor,
        query: str,
        group_ids: list[str] | None = None,
        limit: int = 10,
    ) -> list[CommunityNode]:
        hits = await self._fulltext(executor, 'community_fulltext', query, group_ids, limit)
        found = await get_vertices(executor, 'Community', [h['uuid'] for h in hits])
        return [community_node_from_attrs(a) for a in found]

    async def edge_fulltext_search(
        self,
        executor: QueryExecutor,
        query: str,
        search_filter: SearchFilters,
        group_ids: list[str] | None = None,
        limit: int = 10,
    ) -> list[EntityEdge]:
        wanted = tg_filters.candidates_wanted(limit, tg_filters.has_edge_filters(search_filter))
        hits = await self._fulltext(executor, 'edge_fulltext', query, group_ids, wanted)
        return await self._edges(executor, hits, search_filter, limit)

    # --- similarity ---

    async def node_similarity_search(
        self,
        executor: QueryExecutor,
        search_vector: list[float],
        search_filter: SearchFilters,
        group_ids: list[str] | None = None,
        limit: int = 10,
        min_score: float = 0.6,
    ) -> list[EntityNode]:
        validate_group_ids(group_ids)
        await wait_for_vector(executor, 'Entity')
        wanted = tg_filters.candidates_wanted(limit, tg_filters.has_node_filters(search_filter))
        results = await client_of(executor).run_query(
            'entity_vsearch',
            {'qv': search_vector, 'k': wanted, 'min_score': min_score, **_group_params(group_ids)},
        )
        order = [h['uuid'] for h in _first(results, '@@top')]
        by_uuid = {v['v_id']: entity_node_from_attrs(flatten(v)) for v in _first(results, 'R2')}
        nodes = [by_uuid[u] for u in order if u in by_uuid]
        return [n for n in nodes if tg_filters.node_matches(n, search_filter)][:limit]

    async def community_similarity_search(
        self,
        executor: QueryExecutor,
        search_vector: list[float],
        group_ids: list[str] | None = None,
        limit: int = 10,
        min_score: float = 0.6,
    ) -> list[CommunityNode]:
        validate_group_ids(group_ids)
        await wait_for_vector(executor, 'Community')
        results = await client_of(executor).run_query(
            'community_vsearch',
            {'qv': search_vector, 'k': limit, 'min_score': min_score, **_group_params(group_ids)},
        )
        order = [h['uuid'] for h in _first(results, '@@top')]
        by_uuid = {v['v_id']: community_node_from_attrs(flatten(v)) for v in _first(results, 'R2')}
        return [by_uuid[u] for u in order if u in by_uuid][:limit]

    async def edge_similarity_search(
        self,
        executor: QueryExecutor,
        search_vector: list[float],
        source_node_uuid: str | None,
        target_node_uuid: str | None,
        search_filter: SearchFilters,
        group_ids: list[str] | None = None,
        limit: int = 10,
        min_score: float = 0.6,
    ) -> list[EntityEdge]:
        validate_group_ids(group_ids)
        wanted = tg_filters.candidates_wanted(limit, tg_filters.has_edge_filters(search_filter))
        hits = await self._similar_edge_hits(
            executor,
            search_vector,
            source_node_uuid,
            target_node_uuid,
            group_ids,
            min_score,
            wanted,
        )
        return await self._edges(executor, hits, search_filter, limit)

    async def _similar_edge_hits(
        self,
        executor: QueryExecutor,
        vector: list[float],
        source_node_uuid: str | None,
        target_node_uuid: str | None,
        group_ids: list[str] | None,
        min_score: float,
        wanted: int,
    ) -> list[dict[str, Any]]:
        return await _hits(
            executor,
            'edge_similarity',
            {
                'qv': vector,
                'src_uuid': source_node_uuid or '',
                'dst_uuid': target_node_uuid or '',
                'min_score': min_score,
                'lim': wanted,
                **_group_params(group_ids),
            },
        )

    # --- the helpers behind search_utils.get_relevant_*; the Cypher backends do them in one query ---

    async def relevant_nodes(
        self,
        executor: QueryExecutor,
        nodes: list[EntityNode],
        search_filter: SearchFilters,
        min_score: float,
        limit: int,
    ) -> list[list[EntityNode]]:
        """For each node, the nodes of its group that are similar by embedding, then those that match its name."""

        async def one(node: EntityNode) -> list[EntityNode]:
            similar = (
                await self.node_similarity_search(
                    executor, node.name_embedding, search_filter, [node.group_id], limit, min_score
                )
                if node.name_embedding
                else []
            )
            seen = {n.uuid for n in similar}
            named = await self.node_fulltext_search(
                executor, node.name, search_filter, [node.group_id], limit
            )
            return similar + [n for n in named if n.uuid not in seen]

        return list(await asyncio.gather(*[one(n) for n in nodes]))

    async def relevant_edges(
        self,
        executor: QueryExecutor,
        edges: list[EntityEdge],
        search_filter: SearchFilters,
        min_score: float,
        limit: int,
    ) -> list[list[EntityEdge]]:
        """For each fact, the similar facts between the same two entities (either direction), best first."""

        async def one(edge: EntityEdge) -> list[EntityEdge]:
            if not edge.fact_embedding:
                return []
            wanted = tg_filters.candidates_wanted(limit, tg_filters.has_edge_filters(search_filter))
            ends = [
                (edge.source_node_uuid, edge.target_node_uuid),
                (edge.target_node_uuid, edge.source_node_uuid),
            ]
            groups = [edge.group_id]
            found = await asyncio.gather(
                *[
                    self._similar_edge_hits(
                        executor, edge.fact_embedding, s, t, groups, min_score, wanted
                    )
                    for s, t in dict.fromkeys(ends)
                ]
            )
            hits = sorted((h for part in found for h in part), key=lambda h: -h['score'])
            return await self._edges(executor, hits, search_filter, limit)

        return list(await asyncio.gather(*[one(e) for e in edges]))

    async def edge_invalidation_candidates(
        self,
        executor: QueryExecutor,
        edges: list[EntityEdge],
        search_filter: SearchFilters,
        min_score: float,
        limit: int,
    ) -> list[list[EntityEdge]]:
        """For each fact, the similar facts of its group that touch either of its two entities, best first."""

        async def one(edge: EntityEdge) -> list[EntityEdge]:
            if not edge.fact_embedding:
                return []
            wanted = tg_filters.candidates_wanted(limit, tg_filters.has_edge_filters(search_filter))
            ends = {edge.source_node_uuid, edge.target_node_uuid}
            queries = [(u, None) for u in ends] + [(None, u) for u in ends]
            found = await asyncio.gather(
                *[
                    self._similar_edge_hits(
                        executor, edge.fact_embedding, s, t, [edge.group_id], min_score, wanted
                    )
                    for s, t in queries
                ]
            )
            unique = {h['uuid']: h for part in found for h in part}
            hits = sorted(unique.values(), key=lambda h: -h['score'])
            return await self._edges(executor, hits, search_filter, limit)

        return list(await asyncio.gather(*[one(e) for e in edges]))

    # --- breadth-first search ---

    async def node_bfs_search(
        self,
        executor: QueryExecutor,
        origin_uuids: list[str],
        search_filter: SearchFilters,
        max_depth: int,
        group_ids: list[str] | None = None,
        limit: int = 10,
    ) -> list[EntityNode]:
        validate_group_ids(group_ids)
        if not origin_uuids or max_depth < 1:
            return []
        wanted = tg_filters.candidates_wanted(limit, tg_filters.has_node_filters(search_filter))
        results = await client_of(executor).run_query(
            'node_bfs',
            {
                'origins': origin_uuids,
                'depth': max_depth,
                'lim': wanted,
                **_group_params(group_ids),
            },
        )
        nodes = [entity_node_from_attrs(flatten(v)) for v in _first(results, 'Out')]
        return [n for n in nodes if tg_filters.node_matches(n, search_filter)][:limit]

    async def edge_bfs_search(
        self,
        executor: QueryExecutor,
        origin_uuids: list[str],
        max_depth: int,
        search_filter: SearchFilters,
        group_ids: list[str] | None = None,
        limit: int = 10,
    ) -> list[EntityEdge]:
        validate_group_ids(group_ids)
        if not origin_uuids or max_depth < 1:
            return []
        wanted = tg_filters.candidates_wanted(limit, tg_filters.has_edge_filters(search_filter))
        results = await client_of(executor).run_query(
            'edge_bfs',
            {
                'origins': origin_uuids,
                'depth': max_depth,
                'lim': wanted,
                **_group_params(group_ids),
            },
        )
        edges = [entity_edge_from_attrs(flatten_edge(e)) for e in _first(results, '@@edges')]
        edges = list({e.uuid: e for e in edges}.values())
        endpoints = await self._endpoints(executor, edges, search_filter)
        return [e for e in edges if tg_filters.edge_matches(e, search_filter, endpoints)][:limit]

    # --- rerankers ---

    async def node_distance_reranker(
        self,
        executor: QueryExecutor,
        node_uuids: list[str],
        center_node_uuid: str,
        min_score: float = 0,
    ) -> list[EntityNode]:
        uuids, _ = await distance_ranking(executor, node_uuids, center_node_uuid, min_score)
        return await self._entities(executor, uuids)

    async def episode_mentions_reranker(
        self,
        executor: QueryExecutor,
        node_uuids: list[str],
        min_score: float = 0,
    ) -> list[EntityNode]:
        uuids, _ = await mentions_ranking(executor, node_uuids, min_score)
        return await self._entities(executor, uuids)

    # --- filter builders (the Cypher backends return query fragments; here a filter is applied in Python) ---

    def build_node_search_filters(self, search_filters: SearchFilters) -> Any:
        return search_filters

    def build_edge_search_filters(self, search_filters: SearchFilters) -> Any:
        return search_filters

    def build_fulltext_query(
        self, query: str, group_ids: list[str] | None = None, max_query_length: int = 8000
    ) -> str:
        return ' '.join(tokens(query[:max_query_length]))

    # --- loading the hits ---

    async def _entities(self, executor: QueryExecutor, uuids: list[str]) -> list[EntityNode]:
        found = await get_vertices(executor, 'Entity', uuids)
        return [entity_node_from_attrs(a) for a in found]

    async def _endpoints(
        self, executor: QueryExecutor, edges: list[EntityEdge], search_filter: SearchFilters
    ) -> dict[str, EntityNode]:
        """The end nodes of the edges, when a ``node_labels`` filter needs their labels."""
        if search_filter.node_labels is None:
            return {}
        uuids = {u for e in edges for u in (e.source_node_uuid, e.target_node_uuid)}
        return {n.uuid: n for n in await self._entities(executor, sorted(uuids))}

    async def _edges(
        self,
        executor: QueryExecutor,
        hits: list[dict[str, Any]],
        search_filter: SearchFilters,
        limit: int,
    ) -> list[EntityEdge]:
        """The facts of ``@@top`` hits (uuid, src, dst), best first, read by point lookups of their end pair."""
        client = client_of(executor)
        pairs = list({(h['src'], h['dst']) for h in hits})
        reads = await asyncio.gather(
            *[client.get_edges('Entity', s, 'RELATES_TO', 'Entity', d) for s, d in pairs]
        )
        by_uuid = {r['uuid']: r for rows in reads for r in rows}
        edges = [entity_edge_from_attrs(by_uuid[h['uuid']]) for h in hits if h['uuid'] in by_uuid]
        endpoints = await self._endpoints(executor, edges, search_filter)
        return [e for e in edges if tg_filters.edge_matches(e, search_filter, endpoints)][:limit]


def _first_map(results: list[dict[str, Any]], key: str) -> dict[str, Any]:
    for block in results:
        if key in block:
            return block[key] or {}
    return {}


async def distance_ranking(
    executor: QueryExecutor, node_uuids: list[str], center_node_uuid: str, min_score: float = 0
) -> tuple[list[str], list[float]]:
    """Nodes ordered by closeness to a center node: 1 for a neighbour, 0.1 for the center itself, then the rest.

    The same ranking the Cypher backends compute in ``search_utils.node_distance_reranker``.
    """
    filtered_uuids = [u for u in node_uuids if u != center_node_uuid]
    scores: dict[str, float] = {center_node_uuid: 0.0}

    if filtered_uuids:
        # Walk from the center, not from every entity
        results = await client_of(executor).interpret(
            'SET<STRING> centers, SET<STRING> ids',
            'SetAccum<STRING> @@near; C = to_vertex_set(centers, "Entity");'
            ' S = SELECT n FROM C:c -((RELATES_TO>|<RELATES_TO))- Entity:n WHERE n.uuid IN ids'
            ' ACCUM @@near += n.uuid; PRINT @@near;',
            {'centers': [center_node_uuid], 'ids': filtered_uuids},
        )
        for uuid in _first(results, '@@near'):
            scores[uuid] = 1.0

    for uuid in filtered_uuids:
        scores.setdefault(uuid, float('inf'))
    filtered_uuids.sort(key=lambda u: scores[u])

    if center_node_uuid in node_uuids:
        scores[center_node_uuid] = 0.1
        filtered_uuids = [center_node_uuid, *filtered_uuids]

    kept = [u for u in filtered_uuids if (1 / scores[u]) >= min_score]
    return kept, [1 / scores[u] for u in kept]


async def mentions_ranking(
    executor: QueryExecutor, node_uuids: list[str], min_score: float = 0
) -> tuple[list[str], list[float]]:
    """Nodes ordered by how many episodes mention them, most first (``search_utils.episode_mentions_reranker``)."""
    if not node_uuids:
        return [], []
    # Walk from the entities asked about, not from every episode
    results = await client_of(executor).interpret(
        'SET<STRING> ids',
        'MapAccum<STRING, SumAccum<INT>> @@mentions; N = to_vertex_set(ids, "Entity");'
        ' S = SELECT n FROM N:n -(<MENTIONS)- Episodic:e ACCUM @@mentions += (n.uuid -> 1); PRINT @@mentions;',
        {'ids': node_uuids},
    )
    counts = {k: float(v) for k, v in _first_map(results, '@@mentions').items()}
    scores = {u: counts.get(u, 0.0) for u in node_uuids}
    ordered = sorted(node_uuids, key=lambda u: scores[u], reverse=True)
    kept = [u for u in ordered if scores[u] >= min_score]
    return kept, [scores[u] for u in kept]
