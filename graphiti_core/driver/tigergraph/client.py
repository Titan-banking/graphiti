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
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class TigerGraphError(Exception):
    """A GSQL or REST++ call that TigerGraph refused."""


# The one value an empty SET parameter is sent as (see ``_query_params``)
EMPTY_SET_VALUE = '\x00none'
# Seconds a GSQL statement (DDL, DROP ALL, INSTALL QUERY) may take
GSQL_TIMEOUT = 900.0


def _query_params(params: dict[str, Any] | None) -> list[tuple[str, str]]:
    """Query-string form of GSQL parameters: a list becomes a repeated key, a bool is lower-case."""
    out: list[tuple[str, str]] = []
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            # An empty collection has no query-string form (TigerGraph says the parameter is missing), so it is sent as
            # one value that no uuid, name or group id equals: ``x IN {that}`` matches nothing, as ``x IN {}`` would
            out.extend((key, _scalar(v)) for v in (value or (EMPTY_SET_VALUE,)))
        else:
            out.append((key, _scalar(value)))
    return out


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def wrap(attributes: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """REST++ upsert wants every attribute as ``{'name': {'value': v}}``."""
    return {k: {'value': v} for k, v in attributes.items()}


def flatten(vertex: dict[str, Any]) -> dict[str, Any]:
    """A vertex of a query result or of the REST++ vertex API as one flat dict of its attributes (``uuid`` included)."""
    attrs = {k: v for k, v in (vertex.get('attributes') or {}).items() if not k.startswith('@')}
    attrs.setdefault('uuid', vertex.get('v_id'))
    return attrs


def flatten_edge(edge: dict[str, Any]) -> dict[str, Any]:
    """An edge of a query result as a flat dict of its attributes plus ``source_node_uuid`` / ``target_node_uuid``."""
    attrs = {k: v for k, v in (edge.get('attributes') or {}).items() if not k.startswith('@')}
    attrs['source_node_uuid'] = edge.get('from_id')
    attrs['target_node_uuid'] = edge.get('to_id')
    return attrs


class TigerGraphClient:
    """TigerGraph over HTTP: GSQL statements and interpreted queries (port 14240), REST++ (port 9000).

    There is no session and no transaction: every call is its own request. Durability of a write after REST++ answers
    was not tested against a server crash.
    """

    def __init__(
        self,
        host: str = 'localhost',
        gsql_port: int = 14240,
        restpp_port: int = 9000,
        user: str = 'tigergraph',
        password: str = 'tigergraph',
        graph: str = 'graphiti',
        timeout: float = 120.0,
    ):
        self.graph = graph
        # Vectors written per vertex type that a vector search may not see yet: (uuid, vector), a sample of each write
        self.pending_vectors: dict[str, list[tuple[str, list[float]]]] = {}
        self.host = host
        self._gsql = f'http://{host}:{gsql_port}/gsql/v1'
        self._rest = f'http://{host}:{restpp_port}'
        self._http = httpx.AsyncClient(
            auth=(user, password),
            timeout=timeout,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=64),
        )

    async def close(self) -> None:
        await self._http.aclose()

    def with_graph(self, graph: str) -> 'TigerGraphClient':
        """A client on another graph that shares this client's connection pool."""
        other = object.__new__(TigerGraphClient)
        other.__dict__.update(self.__dict__)
        other.graph = graph
        return other

    # --- GSQL -------------------------------------------------------------------------------------------------

    async def gsql(self, text: str, use_graph: bool = True) -> str:
        """Run GSQL statements (DDL, ``CREATE QUERY``, ``INSTALL QUERY``) and return the server's text.

        The endpoint answers 200 whatever happened, so the text is searched for the words of a failure.
        """
        script = f'USE GRAPH {self.graph}\n{text}' if use_graph else text
        # DDL, DROP ALL and INSTALL QUERY take minutes: the pool's shorter timeout is for REST++ and interpreted queries
        r = await self._http.post(
            f'{self._gsql}/statements',
            content=script,
            headers={'Content-Type': 'text/plain'},
            timeout=GSQL_TIMEOUT,
        )
        if r.status_code >= 400:
            raise TigerGraphError(f'GSQL HTTP {r.status_code}: {r.text[:500]}')
        body = r.text
        lowered = body.lower()
        if 'semantic check fail' in lowered or 'failed to' in lowered or 'syntax error' in lowered:
            raise TigerGraphError(f'GSQL refused {text[:80]!r}: {body[-600:]}')
        return body

    async def interpret(
        self, signature: str, body: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Run an interpreted GSQL query (no install) and return its ``results`` list.

        ``signature`` is the parameter list (``'STRING g, INT k'``) and ``body`` the statements. An interpreted query
        costs about a second of fixed overhead, so a hot path is installed instead (see ``schema.INSTALLED_QUERIES``).
        Vector search is not available in interpreted mode.
        """
        text = f'INTERPRET QUERY ({signature}) FOR GRAPH {self.graph} {{\n{body}\n}}'
        query: list[tuple[str, Any]] = [('graph', self.graph), *_query_params(params)]
        r = await self._http.post(
            f'{self._gsql}/queries/interpret',
            params=httpx.QueryParams(query),
            content=text,
            headers={'Content-Type': 'text/plain'},
        )
        data = self._json(r)
        return data.get('results') or []

    # --- REST++ -----------------------------------------------------------------------------------------------

    async def run_query(
        self, name: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Run an installed query and return its ``results`` list."""
        r = await self._http.post(f'{self._rest}/query/{self.graph}/{name}', json=params or {})
        return self._json(r).get('results') or []

    async def installed_query_names(self) -> set[str]:
        """The names of the queries that are installed (compiled) on this graph, from the REST++ endpoint list."""
        r = await self._http.get(f'{self._rest}/endpoints/{self.graph}', params={'dynamic': 'true'})
        endpoints = r.json() if r.status_code == 200 else {}
        prefix = f'/query/{self.graph}/'
        return {key.split(prefix)[1] for key in endpoints if prefix in key}

    async def upsert(
        self,
        vertices: dict[str, Any] | None = None,
        edges: dict[str, Any] | None = None,
        vertex_must_exist: bool = False,
    ) -> dict[str, int]:
        """Upsert vertices and edges in one call; returns the accepted counts.

        ``vertices`` is ``{type: {id: wrap(attrs)}}``; ``edges`` is ``{src_type: {src_id: {edge_type: {dst_type:
        {dst_id: wrap(attrs) | [wrap(attrs), ...]}}}}}`` (a list gives several parallel edges of a discriminator type).
        """
        payload: dict[str, Any] = {}
        if vertices:
            payload['vertices'] = vertices
        if edges:
            payload['edges'] = edges
        if not payload:
            return {'accepted_vertices': 0, 'accepted_edges': 0}
        params = {'vertex_must_exist': 'true'} if vertex_must_exist else None
        r = await self._http.post(f'{self._rest}/graph/{self.graph}', json=payload, params=params)
        results = self._json(r).get('results') or [{}]
        return {
            'accepted_vertices': int(results[0].get('accepted_vertices', 0)),
            'accepted_edges': int(results[0].get('accepted_edges', 0)),
        }

    async def get_vertex(self, vertex_type: str, vertex_id: str) -> dict[str, Any] | None:
        r = await self._http.get(
            f'{self._rest}/graph/{self.graph}/vertices/{vertex_type}/{quote(vertex_id, safe="")}'
        )
        results = self._json(r, allow_missing=True).get('results') or []
        return flatten(results[0]) if results else None

    async def delete_vertex(self, vertex_type: str, vertex_id: str) -> None:
        r = await self._http.delete(
            f'{self._rest}/graph/{self.graph}/vertices/{vertex_type}/{quote(vertex_id, safe="")}'
        )
        self._json(r, allow_missing=True)

    async def get_edges(
        self, source_type: str, source_id: str, edge_type: str, target_type: str, target_id: str
    ) -> list[dict[str, Any]]:
        """The edges of one type between two vertices, flat (parallel edges of a discriminator type come back each)."""
        path = '/'.join(
            quote(x, safe='') for x in (source_type, source_id, edge_type, target_type, target_id)
        )
        r = await self._http.get(f'{self._rest}/graph/{self.graph}/edges/{path}')
        return [flatten_edge(e) for e in (self._json(r, allow_missing=True).get('results') or [])]

    # --- plumbing ---------------------------------------------------------------------------------------------

    @staticmethod
    def _json(r: httpx.Response, allow_missing: bool = False) -> dict[str, Any]:
        try:
            data = r.json()
        except ValueError as e:
            raise TigerGraphError(f'HTTP {r.status_code}, not JSON: {r.text[:300]}') from e
        if data.get('error'):
            message = str(data.get('message') or data)
            lowered = message.lower()
            if allow_missing and any(
                m in lowered for m in ('not exist', 'not found', 'not a valid vertex id')
            ):
                return {}
            raise TigerGraphError(f'{data.get("code", r.status_code)}: {message[:600]}')
        return data
