#!/usr/bin/env python3
"""
Reproduction: an OpenCypher MATCH whose property value is a field of a parameter
(`{uuid: $d.u}`) is planned as a NodeByLabelScan, not a NodeIndexSeek, although an index
on the property exists. A flat parameter (`{uuid: $u}`) uses the index.

Needs only Python 3.9+ (standard library) and a running ArcadeDB with its HTTP API:

    docker run --rm -p 2480:2480 \
      -e JAVA_OPTS="-Darcadedb.server.rootPassword=playwithdata" \
      arcadedata/arcadedb:latest

    python3 arcadedb_nested_param_repro.py                       # defaults: localhost:2480, N=20000
    python3 arcadedb_nested_param_repro.py --port 2480 --nodes 50000

Exit status 1 when a variant is planned as a full label scan, 0 otherwise.
"""

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


def root_operator(plan_text: str) -> str:
    """First physical operator of the PROFILE plan, e.g. 'NodeIndexSeek(n:Entity) [index=...'."""
    for line in plan_text.splitlines():
        match = re.match(r'\s*\+ (Node\w+\(.*)', line)
        if match:
            return match.group(1)[:90]
    return '(plan has no Node* operator: different execution path)'


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--port', type=int, default=2480)
    parser.add_argument('--user', default='root')
    parser.add_argument('--password', default='playwithdata')
    parser.add_argument('--nodes', type=int, default=20000)
    parser.add_argument('--database', default='nested_param_repro')
    parser.add_argument('--runs', type=int, default=15, help='timed executions per variant')
    parser.add_argument('--keep', action='store_true', help='keep the database afterwards')
    args = parser.parse_args()

    server = Server(args.host, args.port, args.user, args.password)
    db = args.database
    print(f'ArcadeDB {server.version()}')

    server.admin(f'drop database {db}', tolerate_errors=True)
    server.admin(f'create database {db}')
    server.cypher(db, 'CREATE INDEX entity_uuid IF NOT EXISTS FOR (n:Entity) ON (n.uuid)')
    for start in range(0, args.nodes, 5000):
        server.cypher(
            db,
            'UNWIND range($a, $b) AS i CREATE (:Entity {uuid: "u" + toString(i), name: "n"})',
            {'a': start + 1, 'b': min(start + 5000, args.nodes)},
        )
    print(f'{args.nodes} :Entity vertices, unique-key index entity_uuid on Entity(uuid)\n')

    target = f'u{args.nodes // 2}'
    nested, flat = {'d': {'u': target}}, {'u': target}
    # (label, query, params, must not be a full label scan; None = informational)
    variants = [
        (
            'A  flat   inline map   {uuid: $u}',
            'MATCH (n:Entity {uuid: $u}) RETURN n.name',
            flat,
            True,
        ),
        (
            'B  nested inline map   {uuid: $d.u}',
            'MATCH (n:Entity {uuid: $d.u}) RETURN n.name',
            nested,
            True,
        ),
        (
            'C  flat   WHERE        n.uuid = $u',
            'MATCH (n:Entity) WHERE n.uuid = $u RETURN n.name',
            flat,
            True,
        ),
        (
            'D  nested WHERE        n.uuid = $d.u',
            'MATCH (n:Entity) WHERE n.uuid = $d.u RETURN n.name',
            nested,
            True,
        ),
        (
            'E  UNWIND [$d] AS e, {uuid: e.u}',
            'UNWIND [$d] AS e MATCH (n:Entity {uuid: e.u}) RETURN n.name',
            nested,
            None,
        ),
    ]

    bug = False
    header = f'{"variant":42s} {"plan (PROFILE)":92s} {"server ms":>9s}'
    print(header + '\n' + '-' * len(header))
    for label, query, params, should_seek in variants:
        server.cypher(db, query, params)  # warm-up, not timed
        timings = []
        for _ in range(args.runs):
            response = server.cypher(db, 'PROFILE ' + query, params)
            assert len(response['result']) == 1, f'{label}: expected exactly one row'
            plan = response.get('explain', '')
            # server-side time from the PROFILE header, free of HTTP round-trip noise
            timings.append(float(re.search(r'Execution Time: ([\d.]+) ms', plan).group(1)))
        operator = root_operator(plan)
        scans = operator.startswith('NodeByLabelScan')
        verdict = '   <-- full label scan' if scans and should_seek is not None else ''
        bug |= scans and should_seek is not None
        print(f'{label:42s} {operator:92s} {statistics.median(timings):9.2f}{verdict}')

    if not args.keep:
        server.admin(f'drop database {db}', tolerate_errors=True)

    print(
        '\nBUG PRESENT: a field of a parameter is not used as an index seek key (full label scan).'
        if bug
        else '\nOK: no variant is planned as a full label scan.'
    )
    return 1 if bug else 0


if __name__ == '__main__':
    sys.exit(main())
