# Copyright 2024, Zep Software, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Reproduction: a UNIQUE index is not enforced when a transaction creates a record and then updates
an EXTERNAL property of it.

Each race: two transactions create a record with the same UNIQUE key, run one more statement, wait
for each other and commit. Exactly one commit must win. When the statement is an UPDATE of an
EXTERNAL property of the new record, both commit: the type then holds two records with one key,
and the UNIQUE index holds only one of them (an index lookup returns one row, a scan two).

Needs only Python 3.9+ (standard library) and a running ArcadeDB with its HTTP API:

    docker run --rm -p 2480:2480 \
      -e JAVA_OPTS="-Darcadedb.server.rootPassword=playwithdata" \
      arcadedata/arcadedb:26.11.1

    python3 arcadedb_unique_external_update_repro.py                 # localhost:2480

Exit status 1 when any case ends with a duplicate key, 0 when none does, 2 when the server could not be
reached or a statement failed (so a crash is never mistaken for the bug).
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
import urllib.error
import urllib.request


class Server:
    def __init__(self, host: str, port: int, user: str, password: str):
        self.base = f'http://{host}:{port}'
        token = base64.b64encode(f'{user}:{password}'.encode()).decode()
        self.auth = f'Basic {token}'

    def post(self, path: str, body: dict, session: str | None = None) -> tuple[str | None, dict]:
        headers = {'Authorization': self.auth, 'Content-Type': 'application/json'}
        if session:
            headers['arcadedb-session-id'] = session
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(), headers=headers, method='POST'
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.headers.get('arcadedb-session-id'), json.loads(response.read() or b'{}')

    def admin(self, command: str) -> None:
        self.post('/api/v1/server', {'command': command})

    def sql(self, db: str, command: str, params: dict | None = None, session: str | None = None):
        return self.post(
            f'/api/v1/command/{db}',
            {'language': 'sql', 'command': command, 'params': params or {}},
            session,
        )[1]

    def version(self) -> str:
        request = urllib.request.Request(
            self.base + '/api/v1/server', headers={'Authorization': self.auth}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read()).get('version', 'unknown')


def schema(buckets: int, external: bool) -> list[str]:
    ddl = [
        f'CREATE DOCUMENT TYPE T BUCKETS {buckets}',
        'CREATE PROPERTY T.name STRING',
        'CREATE PROPERTY T.ext STRING',
        'CREATE INDEX ON T (name) UNIQUE',
    ]
    if external:
        ddl.append('ALTER PROPERTY T.ext EXTERNAL true')
    return ddl


INSERT = 'INSERT INTO T SET name = :name'
INSERT_WITH_EXT = "INSERT INTO T SET name = :name, ext = 'x'"
UPDATE_EXT = "UPDATE T SET ext = 'x' WHERE name = :name"

# (label, buckets, EXTERNAL property, statements of the transaction)
CASES = [
    ('A  insert, update an EXTERNAL property      16 buckets', 16, True, [INSERT, UPDATE_EXT]),
    ('B  same, 2 buckets', 2, True, [INSERT, UPDATE_EXT]),
    ('C  same, 1 bucket', 1, True, [INSERT, UPDATE_EXT]),
    ('D  insert, update a plain property          16 buckets', 16, False, [INSERT, UPDATE_EXT]),
    ('E  insert with the EXTERNAL value inline    16 buckets', 16, True, [INSERT_WITH_EXT]),
]


def run_case(server: Server, db: str, buckets: int, external: bool, statements, races: int):
    try:
        server.admin(f'drop database {db}')
    except urllib.error.HTTPError:
        pass
    server.admin(f'create database {db}')
    for ddl in schema(buckets, external):
        server.sql(db, ddl)

    commits = [0]
    lock = threading.Lock()

    def race(name: str) -> None:
        barrier = threading.Barrier(2)

        def writer() -> None:
            try:
                session = server.post(f'/api/v1/begin/{db}', {})[0]
                for statement in statements:
                    server.sql(db, statement, {'name': name}, session)
                barrier.wait()
                server.post(f'/api/v1/commit/{db}', {}, session)
                with lock:
                    commits[0] += 1
            except urllib.error.HTTPError:
                pass  # refused: the other transaction won

        threads = [threading.Thread(target=writer) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    for key in range(races):
        race(f'key-{key}')

    rows = server.sql(db, 'SELECT name, count(*) AS c FROM T WHERE @rid IS NOT NULL GROUP BY name')[
        'result'
    ]
    duplicates = sum(1 for row in rows if row['c'] > 1)
    through_index = sum(
        len(server.sql(db, 'SELECT FROM T WHERE name = :name', {'name': f'key-{key}'})['result'])
        for key in range(races)
    )
    server.admin(f'drop database {db}')
    return commits[0], duplicates, through_index


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--port', type=int, default=2480)
    parser.add_argument('--user', default='root')
    parser.add_argument('--password', default='playwithdata')
    parser.add_argument(
        '--races', type=int, default=40, help='keys, each raced by two transactions'
    )
    parser.add_argument('--database', default='unique_external_update_repro')
    args = parser.parse_args()

    server = Server(args.host, args.port, args.user, args.password)
    print(f'ArcadeDB {server.version()}')
    print(f'{args.races} keys per case, two transactions per key, exactly one may commit\n')
    header = f'{"case":58s} {"commits":>7s} {"dup keys":>8s} {"index rows":>10s}'
    print(header + '\n' + '-' * len(header))
    bug = False
    for label, buckets, external, statements in CASES:
        commits, duplicates, through_index = run_case(
            server, args.database, buckets, external, statements, args.races
        )
        broken = duplicates > 0
        bug |= broken
        flag = '   <-- UNIQUE broken' if broken else ''
        print(f'{label:58s} {commits:7d} {duplicates:8d} {through_index:10d}{flag}')

    print(
        '\nPRESENT: two transactions committed the same UNIQUE key.'
        if bug
        else '\nOK: every key was committed once.'
    )
    return 1 if bug else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (urllib.error.URLError, ConnectionError, TimeoutError) as error:
        print(f'cannot run: {error}', file=sys.stderr)
        sys.exit(2)
