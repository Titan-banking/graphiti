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

One transport for the ArcadeDB repro scripts: gRPC, Bolt or HTTP behind one interface.

The repro scripts measure the *server*, so the transport must not change what the server
does. gRPC is the default because it is ArcadeDB's own data plane; Bolt is what Graphiti
sends, and HTTP is the control plane. Every transport runs the same statement with the
same parameters and returns the same rows, so a number measured over one of them is
comparable with a number measured over another.

    from benchmarks.arcadedb_transport import create

    with create('grpc', grpc='127.0.0.1:5000') as transport:
        rows = transport.query('graphiti', 'sql', 'SELECT count(*) AS c FROM RELATES_TO')

The gRPC transport needs the ``arcadedb-driver-grpc`` package
(``uv sync --extra arcadedb-grpc``); Bolt needs the ``neo4j`` driver, which is already a
core dependency; the HTTP transport needs nothing beyond the standard library.
"""

from __future__ import annotations

import base64
import contextlib
import json
import time
import urllib.error
import urllib.request
from typing import Any, Protocol

__all__ = ['Transport', 'TransportError', 'create', 'timed']

# The gRPC unary ExecuteQuery materializes the whole result, so it has a hard ceiling
# (arcadedb.server.grpcQueryMaxResultRows, default 100000). A statement that answers more
# rows than that must use the streaming RPC; the scripts here ask for aggregates and
# top-N results, so the ceiling is not in the way.
GRPC_UNARY_MAX_ROWS = 100_000

# The HTTP API caps a request body; a 1024-float vector is well inside it.
HTTP_MAX_ROWS = 100_000


class TransportError(RuntimeError):
    """A statement failed, or the transport could not reach the server."""


class Transport(Protocol):
    """The interface the repro scripts use: run a statement, get the rows back."""

    #: The credentials the transport authenticates with, for the scripts that need them elsewhere.
    user: str
    password: str

    def query(
        self, database: str, language: str, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...

    def command(
        self, database: str, language: str, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...

    def list_databases(self) -> list[str]: ...

    def create_database(self, name: str) -> bool: ...

    def drop_database(self, name: str) -> bool: ...


def _grpc_value(value: Any) -> Any:
    """A Python value as the gRPC ``GrpcValue`` the wire wants.

    A list of floats is the case that matters here: the edge-similarity scan sends a
    1024-dimension vector as a parameter, and it must arrive as a list of doubles.
    """
    from arcadedb_driver_grpc import messages as pb

    out = pb.GrpcValue()
    if value is None:
        return out
    if isinstance(value, bool):
        out.bool_value = value
    elif isinstance(value, int):
        out.int64_value = value
    elif isinstance(value, float):
        out.double_value = value
    elif isinstance(value, str):
        out.string_value = value
    elif isinstance(value, (list, tuple)):
        out.list_value.SetInParent()
        out.list_value.values.extend(_grpc_value(v) for v in value)
    elif isinstance(value, dict):
        out.map_value.SetInParent()
        for k, v in value.items():
            out.map_value.entries[str(k)].CopyFrom(_grpc_value(v))
    else:
        out.string_value = str(value)
    return out


def _grpc_plain(value: Any) -> Any:
    """A ``GrpcValue`` as the Python value it carries."""
    kind = value.WhichOneof('kind')
    if kind is None:
        return None
    if kind == 'list_value':
        return [_grpc_plain(v) for v in value.list_value.values]
    if kind == 'map_value':
        return {k: _grpc_plain(v) for k, v in value.map_value.entries.items()}
    if kind == 'link_value':
        return value.link_value.rid
    if kind == 'timestamp_value':
        return value.timestamp_value.ToMilliseconds()
    return getattr(value, kind)


def _grpc_row(record: Any) -> dict[str, Any]:
    """A gRPC record as a plain dict, the shape the other transports return."""
    return {key: _grpc_plain(value) for key, value in record.properties.items()}


class _Loop:
    """One event loop, on its own thread, for the whole life of a transport.

    The gRPC client is async-only, and its channel is bound to the loop that made it: a
    second ``asyncio.run`` would answer ``got Future attached to a different loop``. The
    repro scripts are synchronous, so the loop lives here and every call is marshalled
    onto it.
    """

    def __init__(self) -> None:
        import asyncio
        import threading

        self._asyncio = asyncio
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def run(self, coro: Any) -> Any:
        import concurrent.futures

        future = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._schedule, coro, future)
        return future.result()

    def _schedule(self, coro: Any, future: Any) -> None:
        task = self._asyncio.ensure_future(coro)
        task.add_done_callback(lambda t: self._finish(t, future))

    def _finish(self, task: Any, future: Any) -> None:
        if task.cancelled():
            future.cancel()
            return
        error = task.exception()
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(task.result())

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()


class GrpcTransport:
    """ArcadeDB's gRPC data plane.

    The ``ExecuteQuery`` RPC takes the statement, its language and its parameters, and
    answers the rows; ``ExecuteCommand`` is the write side. Both go through the channel's
    authentication interceptor, so the credentials never sit in the request body.
    """

    name = 'grpc'

    def __init__(
        self,
        grpc: str = '127.0.0.1:5000',
        user: str = 'root',
        password: str = 'playwithdata',
        timeout: float = 600.0,
    ) -> None:
        self.target = grpc
        self.user = user
        self.password = password
        self.timeout = timeout
        self._channel: Any = None
        self._stub: Any = None
        self._loop: _Loop | None = None

    def open(self) -> GrpcTransport:
        import grpc
        from arcadedb_driver_grpc import AsyncArcadeDBGrpcClient, password_auth
        from arcadedb_driver_grpc.auth import async_interceptors

        self._loop = _Loop()

        async def connect() -> None:
            channel = grpc.aio.insecure_channel(
                self.target,
                options=[
                    ('grpc.max_send_message_length', 256 * 1024 * 1024),
                    ('grpc.max_receive_message_length', 256 * 1024 * 1024),
                ],
                interceptors=[*async_interceptors(password_auth(self.user, self.password))],
            )
            client = AsyncArcadeDBGrpcClient(channel, allow_admin=True)
            self._channel = channel
            self._client = client
            self._stub = client.raw

        self._loop.run(connect())
        return self

    def close(self) -> None:
        if self._channel is not None:
            # The channel's close() is a coroutine on the loop that made it.
            async def shutdown() -> None:
                await self._channel.close()

            with contextlib.suppress(Exception):
                self._loop.run(shutdown())
            self._channel = None
        if self._loop is not None:
            self._loop.close()
            self._loop = None

    def _run(self, coro: Any) -> Any:
        assert self._loop is not None, 'the transport is not open'
        return self._loop.run(coro)

    def __enter__(self) -> GrpcTransport:
        return self.open()

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _admin(self) -> Any:
        """The admin stub: the control plane (database lifecycle) on the same channel."""
        return self._client.raw_admin

    def _credentials(self) -> Any:
        from arcadedb_driver_grpc import messages as pb

        return pb.DatabaseCredentials(username=self.user, password=self.password)

    def list_databases(self) -> list[str]:
        from arcadedb_driver_grpc import messages as pb

        async def go() -> list[str]:
            response = await self._admin().ListDatabases(
                pb.ListDatabasesRequest(credentials=self._credentials()), timeout=self.timeout
            )
            return list(response.databases)

        return self._run(go())

    def create_database(self, name: str) -> bool:
        from arcadedb_driver_grpc import messages as pb

        async def go() -> bool:
            response = await self._admin().CreateDatabase(
                pb.CreateDatabaseRequest(
                    credentials=self._credentials(), name=name, type='document'
                ),
                timeout=self.timeout,
            )
            return bool(response.created)

        return self._run(go())

    def drop_database(self, name: str) -> bool:
        from arcadedb_driver_grpc import messages as pb

        async def go() -> bool:
            response = await self._admin().DropDatabase(
                pb.DropDatabaseRequest(credentials=self._credentials(), name=name, if_exists=True),
                timeout=self.timeout,
            )
            return bool(response.dropped)

        return self._run(go())

    def query(
        self, database: str, language: str, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        from arcadedb_driver_grpc import messages as pb

        request = pb.ExecuteQueryRequest(
            database=database,
            query=statement,
            language=language,
            limit=GRPC_UNARY_MAX_ROWS,
        )
        for key, value in (params or {}).items():
            request.parameters[key].CopyFrom(_grpc_value(value))

        async def go() -> list[dict[str, Any]]:
            response = await self._stub.ExecuteQuery(request, timeout=self.timeout)
            return [_grpc_row(record) for result in response.results for record in result.records]

        return self._run(go())

    def command(
        self, database: str, language: str, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        from arcadedb_driver_grpc import messages as pb

        request = pb.ExecuteCommandRequest(
            database=database,
            command=statement,
            language=language,
            return_rows=True,
            max_rows=GRPC_UNARY_MAX_ROWS,
        )
        for key, value in (params or {}).items():
            request.parameters[key].CopyFrom(_grpc_value(value))

        async def go() -> list[dict[str, Any]]:
            response = await self._stub.ExecuteCommand(request, timeout=self.timeout)
            if not response.success:
                raise TransportError(f'{response.message}')
            return [_grpc_row(record) for record in response.records]

        return self._run(go())


class BoltTransport:
    """ArcadeDB's Bolt endpoint, the protocol Graphiti's driver speaks.

    The Neo4j driver is the one Graphiti uses, so this transport sends exactly what
    Graphiti sends: a Cypher statement with named parameters.

    Bolt has no server-level control plane, so the database lifecycle goes over HTTP,
    the way ArcadeDB's own ``create database`` command does.
    """

    name = 'bolt'

    def __init__(
        self,
        bolt: str = 'bolt://127.0.0.1:7687',
        user: str = 'root',
        password: str = 'playwithdata',
        timeout: float = 600.0,
        http: str = 'http://127.0.0.1:2480',
    ) -> None:
        self.uri = bolt
        self.user = user
        self.password = password
        self.timeout = timeout
        self.http = http
        self._driver: Any = None

    def open(self) -> BoltTransport:
        from neo4j import GraphDatabase

        self._driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
        return self

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def __enter__(self) -> BoltTransport:
        return self.open()

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def query(
        self, database: str, language: str, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        with self._driver.session(database=database) as session:
            result = session.run(statement, params or {})
            return [dict(record) for record in result.data()]

    def command(
        self, database: str, language: str, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return self.query(database, language, statement, params)

    def _control(self) -> HttpTransport:
        return HttpTransport(
            http=self.http, user=self.user, password=self.password, timeout=self.timeout
        )

    def list_databases(self) -> list[str]:
        return self._control().list_databases()

    def create_database(self, name: str) -> bool:
        return self._control().create_database(name)

    def drop_database(self, name: str) -> bool:
        return self._control().drop_database(name)


class HttpTransport:
    """ArcadeDB's HTTP API: the control plane, and the fallback when no data plane is up."""

    name = 'http'

    def __init__(
        self,
        http: str = 'http://127.0.0.1:2480',
        user: str = 'root',
        password: str = 'playwithdata',
        timeout: float = 600.0,
    ) -> None:
        self.base = http.rstrip('/')
        self.user = user
        self.password = password
        self.timeout = timeout

    def _auth(self) -> str:
        return 'Basic ' + base64.b64encode(f'{self.user}:{self.password}'.encode()).decode()

    def open(self) -> HttpTransport:
        """Nothing to open: the HTTP API has no session."""
        return self

    def close(self) -> None:
        """Nothing to close: the HTTP API has no session."""
        return None

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            method='POST',
            headers={'Authorization': self._auth(), 'Content-Type': 'application/json'},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b'{}')
        except urllib.error.HTTPError as error:
            raise TransportError(f'{error.code}: {error.read()[:500]!r}') from error

    def query(
        self, database: str, language: str, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {'language': language, 'command': statement}
        if params:
            body['params'] = params
        payload = self._post(f'/api/v1/query/{database}', body)
        return payload.get('result', [])

    def command(
        self, database: str, language: str, statement: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {'language': language, 'command': statement}
        if params:
            body['params'] = params
        payload = self._post(f'/api/v1/command/{database}', body)
        return payload.get('result', [])

    def _server(self, command: str) -> Any:
        """A server-level command (``create database``, ``drop database``)."""
        request = urllib.request.Request(
            self.base + '/api/v1/server',
            data=json.dumps({'command': command}).encode(),
            method='POST',
            headers={'Authorization': self._auth(), 'Content-Type': 'application/json'},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b'{}')
        except urllib.error.HTTPError as error:
            raise TransportError(f'{error.code}: {error.read()[:500]!r}') from error

    def list_databases(self) -> list[str]:
        request = urllib.request.Request(
            self.base + '/api/v1/databases', method='GET', headers={'Authorization': self._auth()}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return list(json.loads(response.read() or b'{}').get('result', []))
        except urllib.error.HTTPError as error:
            raise TransportError(f'{error.code}: {error.read()[:500]!r}') from error

    def create_database(self, name: str) -> bool:
        self._server(f'create database {name}')
        return True

    def drop_database(self, name: str) -> bool:
        self._server(f'drop database {name}')
        return True


def create(
    kind: str = 'grpc',
    *,
    grpc: str = '127.0.0.1:5000',
    bolt: str = 'bolt://127.0.0.1:7687',
    http: str = 'http://127.0.0.1:2480',
    user: str = 'root',
    password: str = 'playwithdata',
    timeout: float = 600.0,
) -> Transport:
    """The transport named by ``kind``: ``grpc`` (the default), ``bolt`` or ``http``."""
    if kind == 'grpc':
        return GrpcTransport(grpc=grpc, user=user, password=password, timeout=timeout)
    if kind == 'bolt':
        return BoltTransport(bolt=bolt, user=user, password=password, timeout=timeout)
    if kind == 'http':
        return HttpTransport(http=http, user=user, password=password, timeout=timeout)
    raise ValueError(f'unknown transport {kind!r}: use grpc, bolt or http')


def timed(fn: Any, *args: Any, **kwargs: Any) -> tuple[float, Any]:
    """Call ``fn`` and return the wall milliseconds it took and what it returned."""
    start = time.perf_counter()
    out = fn(*args, **kwargs)
    return (time.perf_counter() - start) * 1000, out
