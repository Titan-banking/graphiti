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

Measure what a projection of a record costs over each transport.

The rows are built over the control plane (the HTTP API, which is where a database is
created), and the reads are measured over the transport named: gRPC by default, Bolt or
HTTP on request. The three carry the same statement and answer the same rows, so the
numbers are comparable.

    python repro_arcadedb_projection_reads.py --properties 100 400 1600

What it shows: a record that carries many properties is expensive to read whole, and the
cost of reading it is paid per projected property when the projection names few of them.
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

DB = 'projectionreads'
N = 1000


def build(transport: Any, http: str, properties: int) -> None:
    """The rows the projection is measured on: a record with ``properties`` of them."""
    control = create('http', http=http, user=transport.user, password=transport.password)
    control.open()
    try:
        with contextlib.suppress(Exception):
            control.drop_database(DB)
        # The drop is asynchronous: wait until the database is gone before creating it again.
        for _ in range(60):
            if DB not in control.list_databases():
                break
            time.sleep(0.5)
        control.create_database(DB)
        # A VERTEX TYPE, so the openCypher label W matches it (a document type has no label).
        control.command(DB, 'sql', 'CREATE VERTEX TYPE W')
        columns = ', '.join(f'f{i:02d} = {i}' for i in range(properties))
        for _ in range(N):
            control.command(DB, 'sql', f'INSERT INTO W SET {columns}')
    finally:
        control.close()


def measure(transport: Any, query: str) -> float:
    """The microseconds per row that the query costs, over the transport."""
    samples = []
    for _ in range(7):
        t0 = time.perf_counter()
        rows = transport.query(DB, 'cypher', query)
        assert len(rows) == N, len(rows)
        samples.append((time.perf_counter() - t0) * 1e6 / N)
    return statistics.median(samples[2:])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--transport', choices=['grpc', 'bolt', 'http'], default='grpc')
    p.add_argument('--grpc', default='127.0.0.1:5000')
    p.add_argument('--bolt', default='bolt://localhost:7687')
    p.add_argument('--http', default='http://localhost:2480')
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
        for properties in args.properties:
            build(transport, args.http, properties)

            whole = measure(transport, 'MATCH (w:W) RETURN w')
            projected = measure(transport, 'MATCH (w:W) RETURN w.f00, w.f01, w.f02')
            # EVERY property projected: the header is walked once per projected name
            every = ', '.join(f'w.f{i:02d}' for i in range(properties))
            allprops = measure(transport, f'MATCH (w:W) RETURN {every}')

            print(
                f'P={properties:>5}  whole record over {args.transport} {whole:>9.1f} us/row   '
                f'3 projected {projected:>8.1f} us/row   all {properties} projected {allprops:>9.1f} us/row',
                flush=True,
            )
    finally:
        transport.close()


if __name__ == '__main__':
    main()
