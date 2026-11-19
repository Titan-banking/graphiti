#!/usr/bin/env python3
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
Reproduction: concurrent `MERGE (a)-[e:R {uuid: $u}]->(b)` of the SAME relationship creates it
more than once, with no error, when its endpoints already have edges. A UNIQUE constraint on
the relationship property makes the same workload exact.

Every relationship is merged by `--threads` clients at the same moment over the HTTP API (each
request a separate auto-committed statement; a client retries a 5xx a few times, as a driver
would). Afterwards every relationship uuid must exist exactly once. Three scenarios:

  A  every relationship has its own two vertices           (control: expected exact)
  B  all relationships share the same two vertices          (the failing case)
  C  like B, with CREATE CONSTRAINT ... REQUIRE e.uuid IS UNIQUE   (workaround)

Needs only Python 3.9+ (standard library) and a running ArcadeDB with its HTTP API:

    docker run --rm -p 2480:2480 \
      -e JAVA_OPTS="-Darcadedb.server.rootPassword=playwithdata" \
      arcadedata/arcadedb:26.11.1

    python3 arcadedb_concurrent_merge_repro.py                      # localhost:2480
    python3 arcadedb_concurrent_merge_repro.py --threads 2 --relationships 300

Exit status 1 when scenario B stores more relationships than it should, 0 otherwise.
"""

from __future__ import annotations

import argparse
import base64
import collections
import json
import random
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


class Server:
    def __init__(self, host: str, port: int, user: str, password: str):
        self.base = f'http://{host}:{port}'
        token = base64.b64encode(f'{user}:{password}'.encode()).decode()
        self.headers = {'Authorization': f'Basic {token}', 'Content-Type': 'application/json'}

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(), headers=self.headers, method='POST'
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.status, json.loads(response.read() or b'{}')
        except urllib.error.HTTPError as error:
            return error.code, {'error': error.read().decode()[:300]}

    def admin(self, command: str) -> tuple[int, dict]:
        return self.post('/api/v1/server', {'command': command})

    def cypher(self, database: str, command: str, params: dict | None = None):
        body: dict = {'language': 'opencypher', 'command': command}
        if params:
            body['params'] = params
        return self.post(f'/api/v1/command/{database}', body)

    def version(self) -> str:
        request = urllib.request.Request(self.base + '/api/v1/server', headers=self.headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read()).get('version', 'unknown')


MERGE = 'MATCH (a:N {uuid: $a}), (b:N {uuid: $b}) MERGE (a)-[e:R {uuid: $u}]->(b)'


def scenario(server: Server, db: str, args, *, shared: bool, unique: bool) -> dict:
    server.admin(f'drop database {db}')
    server.admin(f'create database {db}')
    server.cypher(db, 'CREATE INDEX FOR (n:N) ON (n.uuid)')
    if unique:
        status, body = server.cypher(
            db, 'CREATE CONSTRAINT FOR ()-[e:R]-() REQUIRE e.uuid IS UNIQUE'
        )
        if status != 200:
            raise SystemExit(f'cannot create the unique constraint: {status} {body}')
    else:
        server.cypher(db, 'CREATE INDEX FOR ()-[e:R]-() ON (e.uuid)')
    count = 2 if shared else 2 * args.relationships
    server.cypher(
        db, 'UNWIND range(1, $n) AS i CREATE (:N {uuid: "n" + toString(i)})', {'n': count}
    )

    unrecovered: collections.Counter = collections.Counter()
    retried = 0

    def merge(i: int) -> None:
        nonlocal retried
        a, b = (1, 2) if shared else (2 * i + 1, 2 * i + 2)
        for attempt in range(args.retries + 1):
            status, _ = server.cypher(db, MERGE, {'a': f'n{a}', 'b': f'n{b}', 'u': f'e{i}'})
            if status == 200:
                return
            if status < 500 or attempt == args.retries:
                unrecovered[f'HTTP {status}'] += 1
                return
            retried += 1
            time.sleep(random.random() * 0.02 * (attempt + 1))

    tasks = [i for i in range(args.relationships) for _ in range(args.threads)]
    with ThreadPoolExecutor(args.threads) as pool:
        list(pool.map(merge, tasks))

    _, body = server.cypher(
        db,
        'MATCH ()-[e:R]->() WITH e.uuid AS u, count(*) AS c '
        'RETURN count(u) AS uuids, sum(c) AS edges, sum(CASE WHEN c > 1 THEN 1 ELSE 0 END) AS dup',
    )
    row = body['result'][0]
    server.admin(f'drop database {db}')
    return {
        'edges': int(row['edges'] or 0),
        'uuids': int(row['uuids'] or 0),
        'duplicated': int(row['dup'] or 0),
        'retried': retried,
        'failed': dict(unrecovered),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--port', type=int, default=2480)
    parser.add_argument('--user', default='root')
    parser.add_argument('--password', default='playwithdata')
    parser.add_argument('--threads', type=int, default=4, help='concurrent MERGEs of each rel.')
    parser.add_argument('--relationships', type=int, default=200)
    parser.add_argument('--retries', type=int, default=10, help='client retries of a 5xx')
    parser.add_argument('--database', default='concurrent_merge_repro')
    args = parser.parse_args()

    server = Server(args.host, args.port, args.user, args.password)
    print(f'ArcadeDB {server.version()}')
    print(
        f'{args.relationships} relationships, each MERGEd by {args.threads} concurrent clients; '
        f'expected stored: {args.relationships}\n'
    )

    scenarios = [
        ('A  own endpoints per relationship', dict(shared=False, unique=False)),
        ('B  all share the same two vertices', dict(shared=True, unique=False)),
        ('C  B + UNIQUE constraint on e.uuid', dict(shared=True, unique=True)),
    ]
    header = (
        f'{"scenario":38s} {"stored":>7s} {"uuids stored >1x":>17s} {"5xx retried":>12s}  failed'
    )
    print(header + '\n' + '-' * len(header))
    results = {}
    for label, options in scenarios:
        r = scenario(server, args.database, args, **options)
        results[label[0]] = r
        flag = '' if r['edges'] == args.relationships else '   <-- duplicates'
        print(
            f'{label:38s} {r["edges"]:7d} {r["duplicated"]:17d} {r["retried"]:12d}  '
            f'{r["failed"] or "none"}{flag}'
        )

    bug = results['B']['edges'] > args.relationships
    print(
        '\nPRESENT: concurrent MERGE of one relationship stored it more than once (scenario B).'
        if bug
        else '\nOK: scenario B stored every relationship exactly once.'
    )
    return 1 if bug else 0


if __name__ == '__main__':
    sys.exit(main())
