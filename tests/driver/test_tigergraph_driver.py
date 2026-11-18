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

Unit tests of the TigerGraph driver that need no server: value conversion, the REST++ payloads, the Python-side search
filters and the client's reading of TigerGraph's answers (a fake HTTP transport). The behaviour against a server is
covered by the shared integration suites with ENABLE_TIGERGRAPH=1.
"""

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from graphiti_core.driver.tigergraph.client import TigerGraphClient, TigerGraphError
from graphiti_core.driver.tigergraph.convert import dt_to_str, dumps, loads, str_to_dt
from graphiti_core.driver.tigergraph.filters import edge_matches, node_matches
from graphiti_core.driver.tigergraph.interface import _entity, _entity_edge
from graphiti_core.driver.tigergraph.operations.common import edges_payload, save_edges
from graphiti_core.driver.tigergraph.operations.search_ops import tokens
from graphiti_core.driver.tigergraph.schema import INSTALLED_QUERIES, queries_to_install, query_text
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters

NOW = datetime(2024, 5, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)


class TestConversion:
    def test_datetime_round_trip_keeps_microseconds(self):
        # TigerGraph's DATETIME drops the fraction, so datetimes are stored as strings
        assert str_to_dt(dt_to_str(NOW)) == NOW

    def test_string_order_is_time_order_across_time_zones(self):
        earlier = datetime(2024, 5, 1, 23, 0, tzinfo=timezone(timedelta(hours=5)))  # 18:00 UTC
        later = datetime(2024, 5, 1, 19, 0, tzinfo=timezone.utc)
        assert earlier < later
        assert dt_to_str(earlier) < dt_to_str(later)

    def test_missing_datetime_is_empty_string(self):
        # TigerGraph has no NULL
        assert dt_to_str(None) == ''
        assert str_to_dt('') is None

    def test_attributes_with_datetimes_serialise(self):
        assert loads(dumps({'when': NOW, 'n': 1})) == {'when': str(NOW), 'n': 1}
        assert dumps({}) == ''
        assert loads('') == {}


class TestEdgePayload:
    @staticmethod
    def _row(uuid: str, source: str, target: str):
        return ('Entity', source, 'RELATES_TO', 'Entity', target, {'uuid': uuid})

    def test_parallel_facts_of_one_pair_share_one_list(self):
        payload = edges_payload([self._row('f1', 'a', 'b'), self._row('f2', 'a', 'b')])
        attrs = payload['Entity']['a']['RELATES_TO']['Entity']['b']
        assert [a['uuid']['value'] for a in attrs] == ['f1', 'f2']

    def test_different_pairs_stay_apart(self):
        payload = edges_payload([self._row('f1', 'a', 'b'), self._row('f2', 'a', 'c')])
        assert set(payload['Entity']['a']['RELATES_TO']['Entity']) == {'b', 'c'}


class TestBulkDicts:
    """Core hands a bulk save plain dicts with the free-form attributes beside the fields."""

    def test_entity_attributes_are_the_extra_keys(self):
        node = _entity(
            {
                'uuid': 'n1',
                'name': 'Alice',
                'group_id': 'g',
                'summary': '',
                'created_at': NOW,
                'name_embedding': [0.1],
                'labels': ['Entity', 'Person'],
                'age': 30,
            }
        )
        assert node.attributes == {'age': 30}
        assert node.labels == ['Entity', 'Person']

    def test_edge_attributes_do_not_swallow_fields(self):
        edge = _entity_edge(
            {
                'uuid': 'e1',
                'source_node_uuid': 'a',
                'target_node_uuid': 'b',
                'name': 'KNOWS',
                'fact': 'a knows b',
                'group_id': 'g',
                'episodes': [],
                'created_at': NOW,
                'expired_at': None,
                'valid_at': NOW,
                'invalid_at': None,
                'reference_time': None,
                'fact_embedding': [0.1],
                'weight': 3,
            }
        )
        assert edge.attributes == {'weight': 3}
        assert edge.valid_at == NOW


class TestFilters:
    @staticmethod
    def _edge(**kwargs) -> EntityEdge:
        base = {
            'uuid': 'e1',
            'source_node_uuid': 'a',
            'target_node_uuid': 'b',
            'group_id': 'g',
            'created_at': NOW,
            'name': 'KNOWS',
            'fact': 'a knows b',
        }
        return EntityEdge(**{**base, **kwargs})

    def test_date_filters_are_an_or_of_ands(self):
        edge = self._edge(valid_at=NOW)
        inside = [
            [
                DateFilter(
                    date=NOW - timedelta(days=1),
                    comparison_operator=ComparisonOperator.greater_than,
                ),
                DateFilter(
                    date=NOW + timedelta(days=1), comparison_operator=ComparisonOperator.less_than
                ),
            ]
        ]
        outside = [
            [
                DateFilter(
                    date=NOW + timedelta(days=1),
                    comparison_operator=ComparisonOperator.greater_than,
                )
            ],
            [
                DateFilter(
                    date=NOW - timedelta(days=1), comparison_operator=ComparisonOperator.less_than
                )
            ],
        ]
        assert edge_matches(edge, SearchFilters(valid_at=inside))
        assert not edge_matches(edge, SearchFilters(valid_at=outside))

    def test_is_null_matches_an_absent_date(self):
        edge = self._edge()
        null = [[DateFilter(comparison_operator=ComparisonOperator.is_null)]]
        assert edge_matches(edge, SearchFilters(invalid_at=null))
        assert not edge_matches(edge, SearchFilters(valid_at=null)) or edge.valid_at is None

    def test_a_comparison_with_an_absent_date_never_matches(self):
        greater = [[DateFilter(date=NOW, comparison_operator=ComparisonOperator.greater_than)]]
        assert not edge_matches(self._edge(), SearchFilters(valid_at=greater))

    def test_node_labels_apply_to_both_ends_of_an_edge(self):
        edge = self._edge()
        person = EntityNode(uuid='a', name='a', group_id='g', labels=['Entity', 'Person'])
        thing = EntityNode(uuid='b', name='b', group_id='g', labels=['Entity'])
        filters = SearchFilters(node_labels=['Person'])
        assert not edge_matches(edge, filters, {'a': person, 'b': thing})
        assert edge_matches(
            edge, filters, {'a': person, 'b': person.model_copy(update={'uuid': 'b'})}
        )

    def test_endpoint_and_type_filters(self):
        edge = self._edge()
        assert edge_matches(edge, SearchFilters(edge_types=['KNOWS'], edge_source_node_uuids=['a']))
        assert not edge_matches(edge, SearchFilters(edge_types=['LIKES']))
        assert not edge_matches(edge, SearchFilters(edge_target_node_uuids=['z']))

    def test_node_label_filter_requires_every_label(self):
        node = EntityNode(uuid='a', name='a', group_id='g', labels=['Entity', 'Person'])
        assert node_matches(node, SearchFilters(node_labels=['Person']))
        assert not node_matches(node, SearchFilters(node_labels=['Person', 'Robot']))


class TestFulltextTokens:
    def test_tokens_are_lower_case_words_without_duplicates(self):
        assert tokens('Alice, alice! knows Bob-2') == ['alice', 'knows', 'bob', '2']

    def test_a_long_query_is_cut(self):
        assert len(tokens(' '.join(f'w{i}' for i in range(100)))) == 32


def _client(handler) -> TigerGraphClient:
    client = TigerGraphClient()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


class TestClient:
    async def test_gsql_failure_hidden_in_an_http_200_is_raised(self):
        # The statements endpoint answers 200 whatever happened
        client = _client(
            lambda request: httpx.Response(200, text='Semantic Check Fails: no such type')
        )
        with pytest.raises(TigerGraphError):
            await client.gsql('CREATE GRAPH g(Nope)')

    async def test_reading_a_missing_vertex_gives_none(self):
        body = {
            'error': True,
            'code': '601',
            'message': "The input vertex id 'x' is not a valid vertex id for vertex type = Entity.",
        }
        client = _client(lambda request: httpx.Response(200, json=body))
        assert await client.get_vertex('Entity', 'x') is None

    async def test_other_rest_errors_are_raised(self):
        client = _client(
            lambda request: httpx.Response(200, json={'error': True, 'message': 'boom'})
        )
        with pytest.raises(TigerGraphError):
            await client.get_vertex('Entity', 'x')

    async def test_interpreted_query_sends_list_parameters_as_repeated_keys(self):
        seen: dict[str, list[str]] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update({k: request.url.params.get_list(k) for k in request.url.params})
            return httpx.Response(200, json={'error': False, 'results': [{'S': []}]})

        client = _client(handler)
        await client.interpret(
            'SET<STRING> ids, BOOL flag', 'PRINT ids;', {'ids': ['a', 'b'], 'flag': True}
        )
        assert seen['ids'] == ['a', 'b']
        assert seen['flag'] == ['true']

    async def test_an_empty_set_parameter_is_still_sent_and_matches_nothing(self):
        # TigerGraph answers "Missing parameter" for a key with no value, so an empty set must not be left out
        seen: dict[str, list[str]] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update({k: request.url.params.get_list(k) for k in request.url.params})
            return httpx.Response(200, json={'error': False, 'results': []})

        client = _client(handler)
        await client.interpret('SET<STRING> gids', 'PRINT gids;', {'gids': []})
        assert len(seen['gids']) == 1
        assert seen['gids'][0] not in ('', 'None')

    async def test_vertex_ids_are_url_quoted(self):
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.raw_path.decode())
            return httpx.Response(200, json={'error': False, 'results': []})

        client = _client(handler)
        await client.get_vertex('Entity', 'a/b c')
        assert paths[0].endswith('/vertices/Entity/a%2Fb%20c')


class TestStrictEdgeSaves:
    @staticmethod
    def _executor(accepted: int, seen: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            seen['vertex_must_exist'] = request.url.params.get('vertex_must_exist')
            return httpx.Response(
                200,
                json={
                    'error': False,
                    'results': [{'accepted_vertices': 0, 'accepted_edges': accepted}],
                },
            )

        return SimpleNamespace(client=_client(handler))

    async def test_an_edge_to_a_missing_vertex_is_an_error_not_a_placeholder(self):
        # Without vertex_must_exist TigerGraph would create empty vertices for the ends
        seen: dict = {}
        rows = [('Entity', 'a', 'RELATES_TO', 'Entity', 'b', {'uuid': 'f1'})]
        with pytest.raises(TigerGraphError):
            await save_edges(self._executor(0, seen), rows)
        assert seen['vertex_must_exist'] == 'true'

    async def test_all_edges_accepted_is_fine(self):
        rows = [('Entity', 'a', 'RELATES_TO', 'Entity', 'b', {'uuid': f'f{i}'}) for i in range(3)]
        await save_edges(self._executor(3, {}), rows)


class TestQueryVersions:
    def test_the_text_of_a_query_carries_a_hash_of_its_definition(self):
        text = query_text('node_bfs', 'g')
        assert re.search(r'FOR GRAPH g \{ /\* v:[0-9a-f]{8} \*/', text)

    def test_a_changed_definition_gets_another_hash(self, monkeypatch):
        before = re.search(r'v:(\w+)', query_text('node_bfs', 'g')).group(1)
        monkeypatch.setitem(INSTALLED_QUERIES, 'node_bfs', INSTALLED_QUERIES['node_bfs'] + '\n')
        after = re.search(r'v:(\w+)', query_text('node_bfs', 'g')).group(1)
        assert before != after

    async def test_only_stale_queries_are_reinstalled(self):
        current = {
            n: re.search(r'v:(\w+)', query_text(n, 'graphiti')).group(1) for n in INSTALLED_QUERIES
        }
        shown = '\n'.join(
            f'CREATE OR REPLACE QUERY {n}(INT x) FOR GRAPH graphiti {{ /* v:{h} */ }}'
            for n, h in current.items()
        )
        shown = shown.replace(f'v:{current["node_bfs"]}', 'v:00000000')

        class Client:
            graph = 'graphiti'

            async def gsql(self, text: str) -> str:
                return shown

            async def installed_query_names(self) -> set[str]:
                return set(INSTALLED_QUERIES)

        assert await queries_to_install(Client()) == ['node_bfs']  # type: ignore[arg-type]
