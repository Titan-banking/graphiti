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

Reproduce the whole-record serialization that reads the record once per property (#11738).

A row that carries a whole record is serialized by looping over its property names and reading
each one, and reading one walks the record header from its first byte until it meets the name.
A record with P properties therefore costs P header walks, so the time per row grows with P
squared.

    python repro_arcadedb_wide_row_serialization.py --properties 100 400 1600

The transport is gRPC by default (ArcadeDB's own data plane); ``--transport bolt`` runs the
same statements over Bolt, and ``--transport http`` over the HTTP API. The rows are built over
the control plane either way, which is where a database is created.
"""

import argparse
import contextlib
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from benchmarks.arcadedb_transport import create  # noqa: E402

DB = 'widerows'
N = 1000


def timed(transport: Any, command: str) -> float:
    """The microseconds per row that the statement costs."""
    samples = []
    for _ in range(7):
        t0 = time.perf_counter()
        transport.command(DB, 'sql', command)
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples[2:]) * 1e6 / N


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument('--transport', choices=['grpc', 'bolt', 'http'], default='grpc')
    p.add_argument('--grpc', default='127.0.0.1:5000')
    p.add_argument('--bolt', default='bolt://127.0.0.1:7687')
    p.add_argument('--http', default='http://127.0.0.1:2480')
    p.add_argument('--user', default='root')
    p.add_argument('--password', default='playwithdata')
    p.add_argument('--properties', type=int, nargs='+', default=[100, 400, 1600])
    args = p.parse_args()

    transport = create(
        args.transport,
        grpc=args.grpc,
        bolt=args.bolt,
        http=args.http,
        user=args.user,
        password=args.password,
    )
    transport.open()
    try:
        print(f'{"P":>6} {"SELECT FROM W":>28} {"toJSON() in the query":>26} {"ratio":>7}')
        for properties in args.properties:
            control = create('http', http=args.http, user=args.user, password=args.password)
            control.open()
            try:
                with contextlib.suppress(Exception):
                    control.drop_database(DB)
                control.create_database(DB)
                control.command(DB, 'sql', 'CREATE DOCUMENT TYPE W')
            finally:
                control.close()

            # N records, each with P properties: one statement per record, all the properties at once.
            columns = ', '.join(f'f{i:02d} = {i}' for i in range(properties))
            for _ in range(N):
                transport.command(DB, 'sql', f'INSERT INTO W SET {columns}')

            whole = timed(transport, 'SELECT FROM W')
            inside = timed(transport, 'SELECT @this.toJSON() AS j FROM W')
            print(
                f'{properties:>6} {whole:>18.1f} us/row {inside:>18.1f} us/row {whole / inside:>6.2f}x',
                flush=True,
            )
    finally:
        transport.close()


if __name__ == '__main__':
    main()
