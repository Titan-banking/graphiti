#!/usr/bin/env python3
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

Measure the Postgres wire path of an ArcadeDB server: how long a row of P columns takes over the
Postgres protocol, and the same row read inside the query for comparison.

    python repro_arcadedb_postgres_wide_row.py --pg 15445 --password playwithdata --rows 1000

The server must have the Postgres protocol plugin enabled and the driver jar on the classpath.
"""

import argparse
import contextlib
import json
import os
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER = os.environ.get(
    'POSTGRES_DRIVER_JAR',
    os.path.expanduser('~/.m2/repository/org/postgresql/postgresql/42.7.14/postgresql-42.7.14.jar'),
)


def http(port: int, path: str, body: dict, password: str) -> bytes:
    import base64

    req = urllib.request.Request(
        f'http://127.0.0.1:{port}{path}',
        data=json.dumps(body).encode(),
        method='POST',
        headers={
            'Authorization': 'Basic ' + base64.b64encode(f'root:{password}'.encode()).decode(),
            'Content-Type': 'application/json',
        },
    )
    with urllib.request.urlopen(req, timeout=1800) as resp:
        return resp.read()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    # Uncommon ports on purpose: 2480 and 5432 are what a local ArcadeDB or Postgres listens on,
    # and this script must not reach one that is already running.
    p.add_argument('--http', type=int, default=12485)
    p.add_argument('--pg', type=int, default=15445)
    p.add_argument('--password', default='playwithdata')
    p.add_argument('--rows', type=int, default=1000)
    p.add_argument('--properties', type=int, nargs='+', default=[400, 1600])
    args = p.parse_args()

    if not os.path.exists(DRIVER):
        print(f'PostgreSQL JDBC driver not found at {DRIVER}', file=sys.stderr)
        sys.exit(1)

    for properties in args.properties:
        db = f'pgwide{properties}'
        with contextlib.suppress(Exception):
            http(args.http, '/api/v1/server', {'command': f'drop database {db}'}, args.password)
        http(args.http, '/api/v1/server', {'command': f'create database {db}'}, args.password)
        http(
            args.http,
            f'/api/v1/command/{db}',
            {'language': 'sql', 'command': 'CREATE VERTEX TYPE W'},
            args.password,
        )
        columns = ', '.join(f'f{i:02d} = {i}' for i in range(properties))
        statement = f'INSERT INTO W SET {columns}'
        for _ in range(args.rows):
            http(
                args.http,
                f'/api/v1/command/{db}',
                {'language': 'sql', 'command': statement},
                args.password,
            )

        # the measurement runs in a JVM that has the JDBC driver
        driver = os.path.basename(DRIVER)
        out = subprocess.run(
            [
                'docker',
                'run',
                '--rm',
                '--network',
                'host',
                '-v',
                f'{HERE}/pg:/w',
                '-v',
                f'{os.path.dirname(DRIVER)}:/m2',
                'eclipse-temurin:21',
                'sh',
                '-c',
                'cd /w && javac -cp /m2/'
                + driver
                + ' PostgresWideRow.java && java -cp "/w:/m2/'
                + driver
                + '" PostgresWideRow '
                + str(args.pg)
                + ' '
                + args.password
                + ' '
                + str(args.rows)
                + ' '
                + str(properties),
            ],
            capture_output=True,
            text=True,
        )
        if out.returncode != 0:
            print(out.stderr[-2000:], file=sys.stderr)
            sys.exit(1)
        print(out.stdout.strip(), flush=True)


if __name__ == '__main__':
    main()
