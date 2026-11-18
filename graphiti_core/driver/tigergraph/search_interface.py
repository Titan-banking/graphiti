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

``graphiti_core.search.search_utils`` searches through Cypher unless ``driver.search_interface`` answers first. TigerGraph
does not run Cypher, so this interface routes those searches to the driver's ``search_ops`` and installed queries.
"""

from typing import Any

from graphiti_core.driver.search_interface.search_interface import SearchInterface
from graphiti_core.driver.tigergraph.operations.entity_node_ops import load_embeddings_of
from graphiti_core.driver.tigergraph.operations.search_ops import distance_ranking, mentions_ranking
from graphiti_core.search.search_utils import rrf


class TigerGraphSearchInterface(SearchInterface):
    async def edge_fulltext_search(
        self, driver: Any, query: str, search_filter: Any, group_ids=None, limit: int = 100
    ):
        return await driver.search_ops.edge_fulltext_search(
            driver, query, search_filter, group_ids, limit
        )

    async def edge_similarity_search(
        self,
        driver: Any,
        search_vector: list[float],
        source_node_uuid: str | None,
        target_node_uuid: str | None,
        search_filter: Any,
        group_ids=None,
        limit: int = 100,
        min_score: float = 0.7,
    ):
        return await driver.search_ops.edge_similarity_search(
            driver,
            search_vector,
            source_node_uuid,
            target_node_uuid,
            search_filter,
            group_ids,
            limit,
            min_score,
        )

    async def node_fulltext_search(
        self, driver: Any, query: str, search_filter: Any, group_ids=None, limit: int = 100
    ):
        return await driver.search_ops.node_fulltext_search(
            driver, query, search_filter, group_ids, limit
        )

    async def node_similarity_search(
        self,
        driver: Any,
        search_vector: list[float],
        search_filter: Any,
        group_ids=None,
        limit: int = 100,
        min_score: float = 0.7,
    ):
        return await driver.search_ops.node_similarity_search(
            driver, search_vector, search_filter, group_ids, limit, min_score
        )

    async def episode_fulltext_search(
        self, driver: Any, query: str, search_filter: Any, group_ids=None, limit: int = 100
    ):
        return await driver.search_ops.episode_fulltext_search(
            driver, query, search_filter, group_ids, limit
        )

    async def edge_bfs_search(
        self,
        driver: Any,
        bfs_origin_node_uuids: list[str] | None,
        bfs_max_depth: int,
        search_filter: Any,
        group_ids=None,
        limit: int = 100,
    ):
        return await driver.search_ops.edge_bfs_search(
            driver, bfs_origin_node_uuids or [], bfs_max_depth, search_filter, group_ids, limit
        )

    async def node_bfs_search(
        self,
        driver: Any,
        bfs_origin_node_uuids: list[str] | None,
        search_filter: Any,
        bfs_max_depth: int,
        group_ids=None,
        limit: int = 100,
    ):
        return await driver.search_ops.node_bfs_search(
            driver, bfs_origin_node_uuids or [], search_filter, bfs_max_depth, group_ids, limit
        )

    async def community_fulltext_search(
        self, driver: Any, query: str, group_ids=None, limit: int = 100
    ):
        return await driver.search_ops.community_fulltext_search(driver, query, group_ids, limit)

    async def community_similarity_search(
        self,
        driver: Any,
        search_vector: list[float],
        group_ids=None,
        limit: int = 100,
        min_score: float = 0.6,
    ):
        return await driver.search_ops.community_similarity_search(
            driver, search_vector, group_ids, limit, min_score
        )

    async def get_embeddings_for_communities(self, driver: Any, communities: list[Any]):
        return await load_embeddings_of(
            driver, 'community_embeddings', [c.uuid for c in communities]
        )

    async def node_distance_reranker(
        self, driver: Any, node_uuids: list[str], center_node_uuid: str, min_score: float = 0
    ):
        return await distance_ranking(driver, node_uuids, center_node_uuid, min_score)

    async def episode_mentions_reranker(
        self, driver: Any, node_uuids: list[list[str]], min_score: float = 0
    ):
        # rrf is the preliminary ranker of the Cypher implementation too
        ranked, _ = rrf(node_uuids)
        return await mentions_ranking(driver, ranked, min_score)
