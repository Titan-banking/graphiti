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
Reproduction: an OpenCypher lookup of one relationship by an indexed property is answered by
the relationship index (`MATCH EDGE BY INDEX`) only for one narrow spelling. Spelling the same
lookup with an inline property map (`[e:T {uuid: $u}]`), a label on either endpoint
(`(n:Entity)-[e:T]->(m:Entity)`) or a type union (`[e:A|B]`) is planned as a scan of every
edge or every vertex of the label, ~100x slower at 20k edges, although every variant returns
the same single row.

Needs only Python 3.9+ (standard library) and a running ArcadeDB with its HTTP API:

    docker run --rm -p 2480:2480 \
      -e JAVA_OPTS="-Darcadedb.server.rootPassword=playwithdata" \
      arcadedata/arcadedb:26.11.1

    python3 arcadedb_relationship_index_repro.py                      # localhost:2480, N=20000
    python3 arcadedb_relationship_index_repro.py --port 2480 --edges 50000

Exit status 1 when a spelling expected to use the index is planned as a scan, 0 otherwise.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import statistics
import sys
import urllib.error
import urllib.request


class Server:
    def __init__(self, host: str, port: int, user: str, password: str):
        self.base = f'http://{host}:{port}'
        token = base64.b64encode(f'{user}:{password}'.encode()).decode()
        self.headers = {'Authorization': f'Basic {token}', 'Content-Type': 'application/json'}

    def post(self, path: str, body: dict, *, tolerate_errors: bool = False):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(), headers=self.headers, method='POST'
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.loads(response.read() or b'{}')
        except urllib.error.HTTPError as error:
            if tolerate_errors:
                return {}
            raise SystemExit(f'{path}: HTTP {error.code}: {error.read().decode()[:400]}') from None

    def admin(self, command: str, *, tolerate_errors: bool = False):
        return self.post('/api/v1/server', {'command': command}, tolerate_errors=tolerate_errors)

    def cypher(self, database: str, command: str, params: dict | None = None):
        body: dict = {'language': 'opencypher', 'command': command}
        if params:
            body['params'] = params
        return self.post(f'/api/v1/command/{database}', body)

    def version(self) -> str:
        request = urllib.request.Request(self.base + '/api/v1/server', headers=self.headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read()).get('version', 'unknown')


def access_path(plan_text: str) -> str:
    """The operator that reads the graph: 'MATCH EDGE BY INDEX', 'MATCH NODE (n:Entity)', ..."""
    if 'MATCH EDGE BY INDEX' in plan_text:
        return 'MATCH EDGE BY INDEX'
    match = re.search(
        r'\+ (MATCH (?:NODE|RELATIONSHIP|EDGE)\b[^\n(]*(?:\([^)\n]*\))?|NodeByLabelScan\([^)\n]*\))',
        plan_text,
    )
    return match.group(1).strip()[:34] if match else '(see PROFILE)'


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--port', type=int, default=2480)
    parser.add_argument('--user', default='root')
    parser.add_argument('--password', default='playwithdata')
    parser.add_argument('--edges', type=int, default=20000, help='RELATES_TO edges')
    parser.add_argument('--vertices', type=int, default=2000, help=':Entity vertices')
    parser.add_argument('--database', default='relationship_index_repro')
    parser.add_argument('--runs', type=int, default=9, help='timed executions per variant')
    parser.add_argument('--keep', action='store_true', help='keep the database afterwards')
    args = parser.parse_args()

    server = Server(args.host, args.port, args.user, args.password)
    db = args.database
    print(f'ArcadeDB {server.version()}')

    server.admin(f'drop database {db}', tolerate_errors=True)
    server.admin(f'create database {db}')
    server.cypher(db, 'CREATE INDEX entity_uuid IF NOT EXISTS FOR (n:Entity) ON (n.uuid)')
    server.cypher(
        db, 'CREATE INDEX relates_uuid IF NOT EXISTS FOR ()-[e:RELATES_TO]-() ON (e.uuid)'
    )
    server.cypher(db, 'CREATE INDEX mentions_uuid IF NOT EXISTS FOR ()-[e:MENTIONS]-() ON (e.uuid)')
    server.cypher(
        db,
        'UNWIND range(1, $n) AS i CREATE (:Entity {uuid: "v" + toString(i)})',
        {'n': args.vertices},
    )
    for start in range(0, args.edges, 2000):
        server.cypher(
            db,
            'UNWIND range($a, $b) AS i '
            'MATCH (s:Entity {uuid: "v" + toString(1 + i % $v)}), '
            '(t:Entity {uuid: "v" + toString(1 + (i * 7 + 1) % $v)}) '
            'CREATE (s)-[:RELATES_TO {uuid: "e" + toString(i), fact: "f"}]->(t)',
            {'a': start + 1, 'b': min(start + 2000, args.edges), 'v': args.vertices},
        )
    server.cypher(
        db,
        'MATCH (s:Entity {uuid: "v1"}), (t:Entity {uuid: "v2"}) '
        'CREATE (s)-[:MENTIONS {uuid: "m1"}]->(t)',
    )
    print(
        f'{args.vertices} :Entity vertices, {args.edges} :RELATES_TO edges, 1 :MENTIONS edge; '
        'indexes on Entity(uuid), RELATES_TO(uuid), MENTIONS(uuid)\n'
    )

    target = f'e{args.edges // 2}'
    one, many = {'u': target}, {'us': [target, f'e{args.edges // 3}']}
    # (label, query, params, expected rows, must use the edge index)
    variants = [
        (
            'A  anonymous ends, single type, WHERE',
            'MATCH (n)-[e:RELATES_TO]->(m) WHERE e.uuid = $u RETURN e.uuid',
            one,
            1,
            True,
        ),
        (
            'B  same, WHERE e.uuid IN $us',
            'MATCH (n)-[e:RELATES_TO]->(m) WHERE e.uuid IN $us RETURN e.uuid',
            many,
            2,
            True,
        ),
        (
            'C  inline map on the relationship',
            'MATCH (n)-[e:RELATES_TO {uuid: $u}]->(m) RETURN e.uuid',
            one,
            1,
            True,
        ),
        (
            'D  labelled endpoints, WHERE',
            'MATCH (n:Entity)-[e:RELATES_TO]->(m:Entity) WHERE e.uuid = $u RETURN e.uuid',
            one,
            1,
            True,
        ),
        (
            'E  labelled endpoints + inline map',
            'MATCH (n:Entity)-[e:RELATES_TO {uuid: $u}]->(m:Entity) RETURN e.uuid',
            one,
            1,
            True,
        ),
        (
            'F  one labelled endpoint, WHERE',
            'MATCH (n:Entity)-[e:RELATES_TO]->(m) WHERE e.uuid = $u RETURN e.uuid',
            one,
            1,
            True,
        ),
        (
            'G  type union, WHERE',
            'MATCH (n)-[e:MENTIONS|RELATES_TO]->(m) WHERE e.uuid = $u RETURN e.uuid',
            one,
            1,
            True,
        ),
        (
            'H  type union + inline map (delete shape)',
            'MATCH (n)-[e:MENTIONS|RELATES_TO {uuid: $u}]->(m) RETURN e.uuid',
            one,
            1,
            True,
        ),
    ]

    bug = False
    outcomes: list[tuple[str, bool]] = []
    header = f'{"variant":44s} {"access path":34s} {"server ms":>9s}'
    print(header + '\n' + '-' * len(header))
    for label, query, params, rows, must_seek in variants:
        server.cypher(db, query, params)  # warm-up, not timed
        timings = []
        for _ in range(args.runs):
            response = server.cypher(db, 'PROFILE ' + query, params)
            assert len(response['result']) == rows, f'{label}: expected {rows} row(s)'
            plan = response.get('explain', '')
            elapsed = re.search(r'Execution Time: ([\d.]+) ms', plan)
            assert elapsed, f'{label}: PROFILE output has no execution time'
            timings.append(float(elapsed.group(1)))
        seeks = 'MATCH EDGE BY INDEX' in plan
        verdict = '' if seeks else '   <-- no edge index'
        bug |= must_seek and not seeks
        outcomes.append((label, must_seek and not seeks))
        print(f'{label:44s} {access_path(plan):34s} {statistics.median(timings):9.2f}{verdict}')

    if not args.keep:
        server.admin(f'drop database {db}', tolerate_errors=True)

    scanned = [label[0] for label, scan in outcomes if scan]
    print(
        f'\nPRESENT: variants {", ".join(scanned)} scan instead of using the relationship index; '
        'the others seek it.'
        if bug
        else '\nOK: every variant is answered by the relationship index.'
    )
    return 1 if bug else 0


if __name__ == '__main__':
    sys.exit(main())
