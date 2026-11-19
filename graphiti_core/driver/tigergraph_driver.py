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
from collections.abc import Coroutine
from typing import Any

from graphiti_core.driver.driver import GraphDriver, GraphDriverSession, GraphProvider
from graphiti_core.driver.operations.community_edge_ops import CommunityEdgeOperations
from graphiti_core.driver.operations.community_node_ops import CommunityNodeOperations
from graphiti_core.driver.operations.entity_edge_ops import EntityEdgeOperations
from graphiti_core.driver.operations.entity_node_ops import EntityNodeOperations
from graphiti_core.driver.operations.episode_node_ops import EpisodeNodeOperations
from graphiti_core.driver.operations.episodic_edge_ops import EpisodicEdgeOperations
from graphiti_core.driver.operations.graph_ops import GraphMaintenanceOperations
from graphiti_core.driver.operations.has_episode_edge_ops import HasEpisodeEdgeOperations
from graphiti_core.driver.operations.next_episode_edge_ops import NextEpisodeEdgeOperations
from graphiti_core.driver.operations.saga_node_ops import SagaNodeOperations
from graphiti_core.driver.operations.search_ops import SearchOperations
from graphiti_core.driver.tigergraph import schema
from graphiti_core.driver.tigergraph.client import TigerGraphClient
from graphiti_core.driver.tigergraph.interface import TigerGraphOperations
from graphiti_core.driver.tigergraph.operations.community_edge_ops import (
    TigerGraphCommunityEdgeOperations,
)
from graphiti_core.driver.tigergraph.operations.community_node_ops import (
    TigerGraphCommunityNodeOperations,
)
from graphiti_core.driver.tigergraph.operations.entity_edge_ops import (
    TigerGraphEntityEdgeOperations,
)
from graphiti_core.driver.tigergraph.operations.entity_node_ops import (
    TigerGraphEntityNodeOperations,
)
from graphiti_core.driver.tigergraph.operations.episode_node_ops import (
    TigerGraphEpisodeNodeOperations,
)
from graphiti_core.driver.tigergraph.operations.episodic_edge_ops import (
    TigerGraphEpisodicEdgeOperations,
)
from graphiti_core.driver.tigergraph.operations.graph_ops import (
    TigerGraphGraphMaintenanceOperations,
)
from graphiti_core.driver.tigergraph.operations.has_episode_edge_ops import (
    TigerGraphHasEpisodeEdgeOperations,
)
from graphiti_core.driver.tigergraph.operations.next_episode_edge_ops import (
    TigerGraphNextEpisodeEdgeOperations,
)
from graphiti_core.driver.tigergraph.operations.saga_node_ops import TigerGraphSagaNodeOperations
from graphiti_core.driver.tigergraph.operations.search_ops import TigerGraphSearchOperations
from graphiti_core.driver.tigergraph.search_interface import TigerGraphSearchInterface

logger = logging.getLogger(__name__)


class TigerGraphDriver(GraphDriver):
    """Graphiti on TigerGraph, through GSQL and REST++ (TigerGraph's openCypher has no ``MERGE``, so Graphiti's own
    Cypher cannot run on it).

    Every group lives in one graph (``group_id`` is an attribute): creating a TigerGraph graph takes seconds, so a graph
    per group as in the FalkorDB driver would be slow, and ``clone`` returns this driver.

    Behaviour that differs from the Cypher backends:

    * there are no transactions and no NULLs: each write is its own request, datetimes are stored as fixed-width UTC
      strings and an absent value as ``''``;
    * a write is an upsert keyed by uuid (TigerGraph has no unique-constraint violation to catch), so the duplicate-edge
      race of ``MERGE`` does not arise, and a re-saved edge replaces the previous one;
    * a fact (edge) is read by uuid with a scan of the ``RELATES_TO`` edges, as TigerGraph cannot index an edge attribute;
    * fulltext is a token scan, not Lucene BM25 (see ``operations/search_ops.py``);
    * node and community similarity use TigerGraph's vector index, edge similarity scans the facts exactly.
    """

    provider = GraphProvider.TIGERGRAPH
    default_group_id: str = ''

    def __init__(
        self,
        host: str = 'localhost',
        gsql_port: int = 14240,
        restpp_port: int = 9000,
        user: str = 'tigergraph',
        password: str = 'tigergraph',
        graph: str = 'graphiti',
        embedding_dim: int = 1024,
    ):
        super().__init__()
        self.client = TigerGraphClient(host, gsql_port, restpp_port, user, password, graph)
        self._database = graph
        self.embedding_dim = embedding_dim

        self._entity_node_ops = TigerGraphEntityNodeOperations()
        self._episode_node_ops = TigerGraphEpisodeNodeOperations()
        self._community_node_ops = TigerGraphCommunityNodeOperations()
        self._saga_node_ops = TigerGraphSagaNodeOperations()
        self._entity_edge_ops = TigerGraphEntityEdgeOperations()
        self._episodic_edge_ops = TigerGraphEpisodicEdgeOperations()
        self._community_edge_ops = TigerGraphCommunityEdgeOperations()
        self._has_episode_edge_ops = TigerGraphHasEpisodeEdgeOperations()
        self._next_episode_edge_ops = TigerGraphNextEpisodeEdgeOperations()
        self._search_ops = TigerGraphSearchOperations()
        self._graph_ops = TigerGraphGraphMaintenanceOperations()

        # Graphiti core saves and loads through Cypher unless this answers first
        self.graph_operations_interface = TigerGraphOperations()
        self.search_interface = TigerGraphSearchInterface()
        self.aoss_client = None

    @property
    def entity_node_ops(self) -> EntityNodeOperations:
        return self._entity_node_ops

    @property
    def episode_node_ops(self) -> EpisodeNodeOperations:
        return self._episode_node_ops

    @property
    def community_node_ops(self) -> CommunityNodeOperations:
        return self._community_node_ops

    @property
    def saga_node_ops(self) -> SagaNodeOperations:
        return self._saga_node_ops

    @property
    def entity_edge_ops(self) -> EntityEdgeOperations:
        return self._entity_edge_ops

    @property
    def episodic_edge_ops(self) -> EpisodicEdgeOperations:
        return self._episodic_edge_ops

    @property
    def community_edge_ops(self) -> CommunityEdgeOperations:
        return self._community_edge_ops

    @property
    def has_episode_edge_ops(self) -> HasEpisodeEdgeOperations:
        return self._has_episode_edge_ops

    @property
    def next_episode_edge_ops(self) -> NextEpisodeEdgeOperations:
        return self._next_episode_edge_ops

    @property
    def search_ops(self) -> SearchOperations:
        return self._search_ops

    @property
    def graph_ops(self) -> GraphMaintenanceOperations:
        return self._graph_ops

    async def execute_query(self, cypher_query_: str, **kwargs: Any) -> Any:
        raise NotImplementedError(
            'TigerGraph does not run Cypher: use the driver operations (entity_node_ops, search_ops, ...) '
            'or the GSQL client (driver.client).'
        )

    def session(self, database: str | None = None) -> GraphDriverSession:
        return _TigerGraphSession(self)

    def clone(self, database: str) -> 'TigerGraphDriver':
        # One graph holds every group
        return self

    async def close(self) -> None:
        await self.client.close()

    def delete_all_indexes(self) -> Coroutine:
        return self.graph_ops.delete_all_indexes(self)

    async def build_indices_and_constraints(self, delete_existing: bool = False):
        await self.graph_ops.build_indices_and_constraints(self, delete_existing)

    async def health_check(self) -> None:
        await schema.graph_exists(self.client)


class _TigerGraphSession(GraphDriverSession):
    """TigerGraph has no sessions: this carries the client for the operation classes and runs nothing else."""

    provider = GraphProvider.TIGERGRAPH

    def __init__(self, driver: TigerGraphDriver):
        self.client = driver.client
        self.embedding_dim = driver.embedding_dim

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def run(self, query: str, **kwargs: Any) -> Any:
        raise NotImplementedError('TigerGraph does not run Cypher')

    async def close(self):
        return None

    async def execute_write(self, func, *args, **kwargs):
        return await func(self, *args, **kwargs)
