# FalkorDB vs ArcadeDB benchmark (2026-11-18)

## Setup

- ArcadeDB: 26.12.1-SNAPSHOT (build 9bad325768141aeb592622cf42d667f0d0d8f2e0/1795043306233/main), image arcadedata/arcadedb@sha256:54592d8ad5090e6d6745814e8b49fd3dbd603ff85cfd819c0f5d488bd8be3a80
- FalkorDB image: unknown
- ArcadeDB ARCADEDB_OPTS_MEMORY: -Xms512m -Xmx2560m
- container limits: ArcadeDB 5 GB / 4 CPU, FalkorDB unknown, TigerGraph 8 GB / 4 CPU (docker), one engine running at a time
- ArcadeDB JAVA_OPTS: -Darcadedb.server.rootPassword=playwithdata -Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin
- shared Docker VM: 63 GiB RAM / 8 CPUs, also running unrelated containers throughout
- host: Linux-7.0.0-1017-azure-x86_64-with-glibc2.39, unknown
- graphiti PR #1310 head (fetched): 15963f0616f29ea0bd5865033b47f1c22f1ff455
- this branch: 35cddd0ba2640f148c487a673ce86f1af41f2106
- python: 3.14.5
- embeddings: 64 clusters (centroid + noise), queries are a stored vector + noise
- FalkorDB TIMEOUT: raised from the 1000 ms default to 0 so slow exact scans are measured, not aborted
- ArcadeDB driver write_concurrency: None (no gate): the columns measure the engine under 8 concurrent writers; the driver default of 1 writer per database is measured in arcadedb_concurrent_writes.py
- TigerGraph: tigergraph/community:4.2.6 (amd64), native on this x86_64 host
- TigerGraph driver: one graph for every group, REST++ upserts (no transactions), installed GSQL queries; fulltext is a token scan, edge similarity an exact scan, node similarity HNSW (eventually consistent)
- dim=1024, reads: 200 queries after 20 warmup (50 for exact full scans), write concurrency 8, batch 500, limit 10, min_score 0.6 (the library default) for similarity
- latencies are `p50 / p95` in ms; ANN recall is recall@10 against the exact scan of the same data
- container memory is the peak `docker stats` reading: for ArcadeDB (a JVM) it mostly reflects heap and page-cache sizing, not data size. Disk is the database directory (ArcadeDB pre-sizes 16 bucket files per type) or, for FalkorDB, the growth of the RDB after SAVE.

| metric | N nodes | FalkorDB | ArcadeDB exact | ArcadeDB ANN | ArcadeDB ANN scalar | TigerGraph |
|---|---|---|---|---|---|---|
| bulk write nodes/s | 1000 | n/a | 1045 | 996 | n/a | n/a |
| bulk write nodes/s | 10000 | n/a | 1168 | 1147 | n/a | n/a |
| bulk write nodes/s | 50000 | n/a | 1186 | 1192 | n/a | n/a |
| bulk write edges/s | 1000 | n/a | 366 | 332 | n/a | n/a |
| bulk write edges/s | 10000 | n/a | 374 | 439 | n/a | n/a |
| bulk write edges/s | 50000 | n/a | 544 | 543 | n/a | n/a |
| rows after load == N nodes / 2N edges | 1000 | n/a | ok | ok | n/a | n/a |
| rows after load == N nodes / 2N edges | 10000 | n/a | ok | ok | n/a | n/a |
| rows after load == N nodes / 2N edges | 50000 | n/a | ok | ok | n/a | n/a |
| save node c=1 (ms) | 1000 | n/a | 3.1 / 4.9 | 2.5 / 3.3 | n/a | n/a |
| save node c=1 (ms) | 10000 | n/a | 2.4 / 3.1 | 2.5 / 2.8 | n/a | n/a |
| save node c=1 (ms) | 50000 | n/a | 2.4 / 8.2 | 2.3 / 3.1 | n/a | n/a |
| save edge c=1 (ms) | 1000 | n/a | 2.7 / 3.4 | 2.4 / 2.8 | n/a | n/a |
| save edge c=1 (ms) | 10000 | n/a | 2.6 / 3.0 | 2.6 / 3.9 | n/a | n/a |
| save edge c=1 (ms) | 50000 | n/a | 2.6 / 3.2 | 2.6 / 3.2 | n/a | n/a |
| save node c=8 (ms) | 1000 | n/a | 12.5 / 23.2 | 11.3 / 15.2 | n/a | n/a |
| save node c=8 (ms) | 10000 | n/a | 10.9 / 12.9 | 11.6 / 13.5 | n/a | n/a |
| save node c=8 (ms) | 50000 | n/a | 11.2 / 13.9 | 11.0 / 13.1 | n/a | n/a |
| save edge c=8 (ms) | 1000 | n/a | 11.6 / 14.4 | 11.5 / 13.4 | n/a | n/a |
| save edge c=8 (ms) | 10000 | n/a | 12.1 / 14.5 | 11.4 / 13.7 | n/a | n/a |
| save edge c=8 (ms) | 50000 | n/a | 11.5 / 13.8 | 11.8 / 15.2 | n/a | n/a |
| node fulltext (ms) | 1000 | n/a | 4.5 / 7.2 | 3.8 / 5.7 | n/a | n/a |
| node fulltext (ms) | 10000 | n/a | 7.0 / 8.0 | 6.5 / 7.1 | n/a | n/a |
| node fulltext (ms) | 50000 | n/a | 11.4 / 13.0 | 11.1 / 13.7 | n/a | n/a |
| edge fulltext (ms) | 1000 | n/a | 5.8 / 7.8 | 5.5 / 6.5 | n/a | n/a |
| edge fulltext (ms) | 10000 | n/a | 8.5 / 9.8 | 8.2 / 9.1 | n/a | n/a |
| edge fulltext (ms) | 50000 | n/a | 18.8 / 21.6 | 18.5 / 20.4 | n/a | n/a |
| node similarity (ms) | 1000 | n/a | 18.9 / 20.8 | 8.9 / 11.5 | n/a | n/a |
| node similarity (ms) | 10000 | n/a | 109.4 / 114.5 | 27.4 / 32.7 | n/a | n/a |
| node similarity (ms) | 50000 | n/a | 688.9 / 705.7 | 60.2 / 67.0 | n/a | n/a |
| edge similarity, exact (ms) | 1000 | n/a | 31.4 / 35.1 | 30.7 / 33.5 | n/a | n/a |
| edge similarity, exact (ms) | 10000 | n/a | 221.8 / 232.7 | 221.0 / 229.5 | n/a | n/a |
| edge similarity, exact (ms) | 50000 | n/a | 1456.5 / 1483.9 | 1452.8 / 1468.6 | n/a | n/a |
| node BFS depth 2 (ms) | 1000 | n/a | 10.4 / 14.4 | 7.9 / 8.3 | n/a | n/a |
| node BFS depth 2 (ms) | 10000 | n/a | 49.8 / 53.9 | 30.7 / 34.2 | n/a | n/a |
| node BFS depth 2 (ms) | 50000 | n/a | 468.4 / 527.0 | 144.1 / 155.5 | n/a | n/a |
| hybrid search RRF (ms) | 1000 | n/a | 27.5 / 34.0 | 27.2 / 32.7 | n/a | n/a |
| hybrid search RRF (ms) | 10000 | n/a | 217.3 / 226.6 | 217.5 / 226.0 | n/a | n/a |
| hybrid search RRF (ms) | 50000 | n/a | 1449.2 / 1472.8 | 1413.1 / 1444.3 | n/a | n/a |
| ANN recall@10 | 1000 | n/a | n/a | 1.000 | n/a | n/a |
| ANN recall@10 | 10000 | n/a | n/a | 1.000 | n/a | n/a |
| ANN recall@10 | 50000 | n/a | n/a | 1.000 | n/a | n/a |
| vector index catch-up after the bulk load | 1000 | n/a | n/a | n/a | n/a | n/a |
| vector index catch-up after the bulk load | 10000 | n/a | n/a | n/a | n/a | n/a |
| vector index catch-up after the bulk load | 50000 | n/a | n/a | n/a | n/a | n/a |
| container memory / disk | 1000 | n/a | 2673 MiB peak / 224 MB disk | 2806 MiB peak / 207 MB disk | n/a | n/a |
| container memory / disk | 10000 | n/a | 2842 MiB peak / 656 MB disk | 2914 MiB peak / 479 MB disk | n/a | n/a |
| container memory / disk | 50000 | n/a | 3048 MiB peak / 2229 MB disk | 2977 MiB peak / 1849 MB disk | n/a | n/a |

## ArcadeDB engine-fused search vs Graphiti Python RRF (informational, ungrouped)

| N nodes | Graphiti Python RRF p50 / p95 | engine `/vector/{db}/hybrid` p50 / p95 |
|---|---|---|
| 1000 | 10.6 / 12.6 | 6.1 / 7.5 |
| 10000 | 13.0 / 13.6 | 11.1 / 13.1 |
| 50000 | 23.6 / 27.3 | 21.5 / 23.1 |
