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
Reproduction: edge-insert throughput FALLS as writers are added, with a trivial payload.

Each writer repeatedly sends one statement that creates `--batch` relationships between random
existing vertices (no properties, no embeddings, no indexes besides the vertex key), as one
auto-committed transaction over the HTTP API. A request that fails with a conflict (HTTP 503) is
retried immediately, so throughput is the engine's, not a client back-off's.

    edges/s = committed edges / wall time, for 1, 2, 4 and 8 writer threads.

Needs only Python 3.9+ (standard library) and a running ArcadeDB:

    docker run --rm -p 2480:2480 \
      -e JAVA_OPTS="-Darcadedb.server.rootPassword=playwithdata" \
      arcadedata/arcadedb:26.11.1

    python3 arcadedb_concurrent_edge_insert_repro.py                 # localhost:2480
    python3 arcadedb_concurrent_edge_insert_repro.py --vertices 100000 --batch 100

Exit status 1 when 8 writers are slower than 1 writer, 0 otherwise.
"""

from __future__ import annotations

import argparse
import base64
import collections
import json
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request


class Server:
    def __init__(self, host: str, port: int, user: str, password: str):
        self.base = f'http://{host}:{port}'
        token = base64.b64encode(f'{user}:{password}'.encode()).decode()
        self.headers = {'Authorization': f'Basic {token}', 'Content-Type': 'application/json'}

    def post(self, path: str, body: dict) -> tuple[int, str]:
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(), headers=self.headers, method='POST'
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    def cypher(self, db: str, command: str, params: dict | None = None) -> tuple[int, str]:
        body: dict = {'language': 'opencypher', 'command': command}
        if params:
            body['params'] = params
        return self.post(f'/api/v1/command/{db}', body)

    def version(self) -> str:
        request = urllib.request.Request(self.base + '/api/v1/server', headers=self.headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read()).get('version', 'unknown')


INSERT = 'UNWIND $pairs AS p MATCH (a:N {id: p[0]}), (b:N {id: p[1]}) CREATE (a)-[:R]->(b)'


def run(server: Server, db: str, args, writers: int, disjoint: bool = False) -> dict:
    server.post('/api/v1/server', {'command': f'drop database {db}'})
    server.post('/api/v1/server', {'command': f'create database {db}'})
    server.cypher(db, 'CREATE INDEX FOR (n:N) ON (n.id)')
    for start in range(0, args.vertices, 5000):
        server.cypher(
            db,
            'UNWIND range($a, $b) AS i CREATE (:N {id: i})',
            {'a': start, 'b': min(start + 4999, args.vertices - 1)},
        )

    lock = threading.Lock()
    remaining = [args.batches]
    committed = [0]
    conflicts: collections.Counter = collections.Counter()
    failed: collections.Counter = collections.Counter()

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        while True:
            with lock:
                if remaining[0] <= 0:
                    return
                remaining[0] -= 1
            if disjoint:
                # vertices congruent to this writer's index modulo the number of writers
                span = args.vertices // writers
                pairs = [
                    [rng.randrange(span) * writers + seed, rng.randrange(span) * writers + seed]
                    for _ in range(args.batch)
                ]
            else:
                pairs = [
                    [rng.randrange(args.vertices), rng.randrange(args.vertices)]
                    for _ in range(args.batch)
                ]
            while True:
                status, body = server.cypher(db, INSERT, {'pairs': pairs})
                if status == 200:
                    with lock:
                        committed[0] += args.batch
                    break
                if status == 503:
                    # {"exception":"...ConcurrentModificationException","detail":"Slot rebase not
                    # possible on page PageId(db/1/7) slot 12 (concurrent change to the same record)..."}
                    try:
                        detail = json.loads(body).get('detail', body)
                    except ValueError:
                        detail = body
                    with lock:
                        conflicts[re.sub(r'\d+', 'N', detail)[:90]] += 1
                    continue
                with lock:
                    failed[f'HTTP {status} {body[:80]}'] += 1
                break

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(writers)]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall = time.perf_counter() - started

    status, body = server.cypher(db, 'MATCH ()-[r:R]->() RETURN count(r) AS c')
    stored = json.loads(body)['result'][0]['c'] if status == 200 else -1
    server.post('/api/v1/server', {'command': f'drop database {db}'})
    return {
        'edges_per_s': committed[0] / wall,
        'conflicts': sum(conflicts.values()),
        'where': conflicts.most_common(3),
        'failed': dict(failed),
        'committed': committed[0],
        'stored': stored,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--port', type=int, default=2480)
    parser.add_argument('--user', default='root')
    parser.add_argument('--password', default='playwithdata')
    parser.add_argument('--vertices', type=int, default=50000)
    parser.add_argument('--batch', type=int, default=500, help='relationships per transaction')
    parser.add_argument('--batches', type=int, default=80, help='transactions per scenario')
    parser.add_argument('--writers', type=int, nargs='+', default=[1, 2, 4, 8])
    parser.add_argument('--database', default='edge_insert_repro')
    args = parser.parse_args()

    server = Server(args.host, args.port, args.user, args.password)
    print(f'ArcadeDB {server.version()}')
    print(
        f'{args.vertices} vertices, {args.batches} transactions of {args.batch} random '
        'relationships (no properties), retried immediately on a conflict\n'
    )
    header = f'{"writers":>7} {"edges/s":>9} {"conflicts":>10} {"stored":>8}  most common conflict'
    print(header + '\n' + '-' * 90)
    rates = {}
    for writers in args.writers:
        r = run(server, args.database, args, writers)
        rates[writers] = r['edges_per_s']
        flag = '' if r['stored'] == r['committed'] else f'   stored != committed ({r["committed"]})'
        where = r['where'][0][0][:48] if r['where'] else ''
        print(
            f'{writers:7d} {r["edges_per_s"]:9.0f} {r["conflicts"]:10d} {r["stored"]:8d}  '
            f'{where}{flag}{" " + str(r["failed"]) if r["failed"] else ""}'
        )

    first, last = min(rates), max(rates)
    bug = rates[last] < rates[first]

    # Control: the same engine, the same transactions, but every writer owns its vertices, so
    # no two concurrent transactions can touch the same record.
    c = run(server, args.database, args, last, disjoint=True)
    print(
        f'{last:7d} {c["edges_per_s"]:9.0f} {c["conflicts"]:10d} {c["stored"]:8d}  '
        f'<- control: {last} writers on DISJOINT vertices'
    )

    print(
        f'\nPRESENT: {last} writers on shared vertices ({rates[last]:.0f} edges/s) are slower than '
        f'{first} ({rates[first]:.0f} edges/s), while {last} writers on disjoint vertices reach '
        f'{c["edges_per_s"]:.0f} edges/s: the loss is the cost of conflicting transactions.'
        if bug
        else f'\nOK: {last} writers are not slower than {first}.'
    )
    return 1 if bug else 0


if __name__ == '__main__':
    sys.exit(main())
